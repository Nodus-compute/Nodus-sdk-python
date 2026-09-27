"""GPU workspaces: a saved project, a rented machine with VS Code, JupyterLab and SSH, and background jobs."""
from __future__ import annotations

import asyncio
import math
import os
import re
import time
import uuid
import warnings
from pathlib import Path
from typing import Any

from .errors import APIError, NodusError, NotFoundError, ValidationError, WorkspaceNotReadyError


_RESEARCH_WORKSPACE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}", re.ASCII)
_BASE = "/v1/research-workspaces"
_TOOLS = ("editor", "notebook", "ssh")
_EDITOR_TOOL = {"": "editor", "vscode": "editor", "jupyter": "notebook", "ssh": "ssh"}
_GPU_COUNTS = (1, 2, 4, 8)
_CPU_COUNTS = (2, 4, 8, 16, 32)
_TEN_GB_POLICIES = {"included-10gb-v1", "r2-standard-10gb-account-v1"}
_CONFIGURATION_FIELDS = {
    "name", "environment", "runtime_id", "editor", "repository", "ref", "gpu", "gpu_count", "gpu_memory_gb",
    "gpu_form_factor", "compute_class", "vcpus", "host_memory_gb", "disk_gb", "budget_usd", "max_hours", "size_gb",
    "ssh_authorized_key",
}
# The console's GPU catalog: memory per model as it publishes it, so the same name means the same request.
_GPU_MEMORY_GB = {
    "H100": 80, "A100": 80, "H200": 141, "B200": 180, "L40S": 48, "L4": 24, "A10": 24, "RTXA6000": 48,
    "RTX3090": 24, "RTX4090": 24, "RTX5090": 32,
}
_AMD = {"MI300X", "MI350X"}
_PUBLIC_KEY_TYPES = ("ssh-ed25519 ", "ssh-rsa ", "ssh-dss ", "ecdsa-sha2-", "sk-ssh-ed25519@openssh.com ", "sk-ecdsa-sha2-")
_OPTIONAL_CONFIGURATION_FIELDS = {"repository", "ref", "gpu_form_factor", "ssh_authorized_key", "runtime_id", "disk_gb"}


def _path(workspace_id: str) -> str:
    if type(workspace_id) is not str or _RESEARCH_WORKSPACE_ID.fullmatch(workspace_id) is None:
        raise ValidationError("Use a workspace ID returned by Nodus")
    return f"{_BASE}/{workspace_id}"


def _key(value: str) -> str:
    from . import _valid_idempotency_key
    return _valid_idempotency_key(value)


def _fresh_key() -> str:
    return f"nodus-{uuid.uuid4()}"


_GPU_SHORTHAND = re.compile(r"^(?P<model>.*?)(?:[-_ ]?(?P<memory>\d{1,4})\s*GB?)?(?::(?P<count>\d+))?$", re.IGNORECASE)


def _parse_gpu(gpu: str) -> tuple[str, float | None, int | None]:
    """Split "A100-80GB:2" into the model, the memory it names and the count it names."""
    match = _GPU_SHORTHAND.fullmatch(gpu.strip())
    model = (match.group("model") if match else gpu).strip()
    if not match or not model:
        raise ValidationError("gpu must name a model, such as \"H100\", \"A100-40GB\" or \"H100:2\"")
    memory = float(match.group("memory")) if match.group("memory") else None
    count = int(match.group("count")) if match.group("count") else None
    if count is not None and count not in _GPU_COUNTS:
        raise ValidationError("gpu_count must be one of 1, 2, 4, 8")
    return model, memory, count


def _gpu_fields(gpu: str, gpu_count: int | None, gpu_memory_gb: float | None, *,
                require_memory: bool, allow_default_count: bool = False) -> dict[str, Any]:
    """The gpu, gpu_count and gpu_memory_gb the server needs, from a name and explicit overrides."""
    model, named_memory, named_count = _parse_gpu(_text(gpu, "gpu", limit=64))
    if gpu_count is not None:
        _count(gpu_count, "gpu_count", _GPU_COUNTS)
    if (named_count is not None and gpu_count is not None and gpu_count != named_count
            and not (allow_default_count and gpu_count == 1)):
        raise ValidationError(f"gpu_count {gpu_count} disagrees with the count in {gpu!r}")
    if named_memory is not None and gpu_memory_gb is not None and gpu_memory_gb != named_memory:
        raise ValidationError(f"gpu_memory_gb {gpu_memory_gb} disagrees with the memory in {gpu!r}")
    fields: dict[str, Any] = {"gpu": model}
    count = named_count if named_count is not None else gpu_count
    if count is not None:
        fields["gpu_count"] = _count(count, "gpu_count", _GPU_COUNTS)
    memory = named_memory if named_memory is not None else gpu_memory_gb
    if memory is None and require_memory:
        memory = _GPU_MEMORY_GB.get(_compact_gpu(model))
    if memory is None and require_memory:
        raise ValidationError(f"Pass gpu_memory_gb or name it, as in \"{model}-80GB\": "
                              f"{model!r} is not in the console's GPU catalog")
    if memory is not None:
        fields["gpu_memory_gb"] = _finite(memory, "gpu_memory_gb", minimum=0)
    return fields


def _compact_gpu(gpu: str) -> str:
    compact = gpu.strip().upper()
    for prefix in ("NVIDIA ", "AMD ", "INSTINCT "):
        compact = compact.removeprefix(prefix).strip()
    compact = compact.replace(" ", "").replace("-", "").replace("_", "")
    return {"A6000": "RTXA6000", "MI350": "MI350X"}.get(compact, compact)


def _finite(value: Any, field: str, *, minimum: float, allow_equal: bool = False) -> float:
    bound = "at least" if allow_equal else "greater than"
    try:
        finite = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (value < minimum if allow_equal else value <= minimum):
        raise ValidationError(f"{field} must be a finite number {bound} {minimum}")
    return value


def _optional_amount(value: Any) -> float | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
    except OverflowError:
        pass
    return None


def _wait_bounds(poll_seconds: Any, timeout_seconds: Any) -> None:
    _finite(poll_seconds, "poll_seconds", minimum=0.1, allow_equal=True)
    _finite(timeout_seconds, "timeout_seconds", minimum=0, allow_equal=True)


def _uncertain(error: NodusError) -> bool:
    """A failure that leaves it unknown whether the request was accepted."""
    from .errors import APIConnectionError, APITimeoutError
    if isinstance(error, (APIConnectionError, APITimeoutError)):
        return True
    return error.status_code is None or error.status_code >= 500


def _ssh_key(value: Any) -> str:
    """Public keys only, one per line, so a private key file never leaves the process."""
    text = _text(value, "ssh_key", limit=8192)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or any(not line.startswith(_PUBLIC_KEY_TYPES) for line in lines):
        raise ValidationError("ssh_key must hold OpenSSH public keys, one per line, such as \"ssh-ed25519 AAAA...\"")
    return text


def _choice(value: Any, field: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValidationError(f"{field} must be one of {', '.join(choices)}")
    return value


def _validated_change(field: str, value: Any) -> Any:
    """The same rules create applies, for one configuration field changed later."""
    if value is None:
        if field in _OPTIONAL_CONFIGURATION_FIELDS:
            return None
        raise ValidationError(f"{field} cannot be cleared. Set a value or leave the field unchanged")
    if field == "budget_usd":
        return _finite(value, field, minimum=0, allow_equal=True)
    if field in ("gpu_memory_gb", "size_gb", "host_memory_gb"):
        return _finite(value, field, minimum=0)
    if field == "gpu_count":
        return _count(value, field, _GPU_COUNTS)
    if field == "vcpus":
        return _count(value, field, _CPU_COUNTS)
    if field in ("max_hours", "disk_gb"):
        low, high = (1, 168) if field == "max_hours" else (80, 2048)
        if isinstance(value, bool) or type(value) is not int or not low <= value <= high:
            raise ValidationError(f"{field} must be a whole number from {low} to {high}")
        return value
    if field == "editor":
        return _choice(value, field, ("vscode", "jupyter", "ssh"))
    if field == "compute_class":
        return _choice(value, field, ("accelerator", "vm"))
    if field == "gpu_form_factor":
        return _choice(value, field, ("pcie", "sxm", "nvl"))
    if field == "ssh_authorized_key":
        return _ssh_key(value)
    return _text(value, field, limit=128 if field == "name" else 512)


def _count(value: Any, field: str, choices: tuple[int, ...]) -> int:
    if isinstance(value, bool) or type(value) is not int or value not in choices:
        raise ValidationError(f"{field} must be one of {', '.join(str(c) for c in choices)}")
    return value


def _text(value: Any, field: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValidationError(f"{field} must be non-empty text of at most {limit} characters")
    return value


def _default_size_gb(capabilities: Any) -> float:
    """The project storage the deployment allows, in the unit its policy prices."""
    if not isinstance(capabilities, dict):
        raise APIError("Workspace capabilities response is invalid")
    if capabilities.get("storage_policy_version") in _TEN_GB_POLICIES:
        return 10
    limit = capabilities.get("storage_limit_bytes")
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or limit <= 0:
        raise ValidationError("Pass size_gb: the deployment did not report a storage limit")
    return limit / 1024 ** 3


def _configuration(name: str, *, gpu: str | None, gpu_count: int, gpu_memory_gb: float | None,
                   environment: str | None, editor: str, max_hours: int | None, size_gb: float | None,
                   budget_usd: float | None, ssh_key: str | None, cpus: int | None, memory_gb: float | None,
                   disk_gb: int | None, repository: str | None, ref: str | None, runtime_id: str | None,
                   form_factor: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {"name": _text(name, "name", limit=128)}
    if environment is not None:
        _text(environment, "environment", limit=64)
    if form_factor is not None:
        _choice(form_factor, "form_factor", ("pcie", "sxm", "nvl"))
    if cpus is not None:
        if gpu is not None or gpu_count != 1 or gpu_memory_gb is not None or form_factor is not None:
            raise ValidationError("A CPU workspace takes cpus and memory_gb, not gpu, gpu_count, gpu_memory_gb or form_factor")
        body["environment"] = environment or "pytorch-cpu"
        body["editor"] = editor
        body["compute_class"] = "vm"
        body["vcpus"] = _count(cpus, "cpus", _CPU_COUNTS)
        if memory_gb is None:
            raise ValidationError("memory_gb is required for a CPU workspace")
        body["host_memory_gb"] = _finite(memory_gb, "memory_gb", minimum=0)
    else:
        if gpu is None:
            raise ValidationError("Choose a gpu such as \"H100\", or cpus for a CPU-only workspace")
        fields = _gpu_fields(gpu, gpu_count, gpu_memory_gb, require_memory=True, allow_default_count=True)
        body["environment"] = environment or ("pytorch-rocm" if _compact_gpu(fields["gpu"]) in _AMD else "pytorch-cuda")
        body["editor"] = editor
        body.update(fields)
        if form_factor is not None:
            body["gpu_form_factor"] = form_factor
    _choice(editor, "editor", ("vscode", "jupyter", "ssh"))
    if editor == "ssh" and not ssh_key:
        raise ValidationError("ssh_key is required for an SSH-only workspace")
    if ssh_key is not None:
        body["ssh_authorized_key"] = _ssh_key(ssh_key)
    if max_hours is None:
        raise ValidationError("max_hours is required: the session stops itself after this many hours")
    if isinstance(max_hours, bool) or type(max_hours) is not int or not 1 <= max_hours <= 168:
        raise ValidationError("max_hours must be a whole number from 1 to 168")
    body["max_hours"] = max_hours
    if size_gb is not None:
        body["size_gb"] = _finite(size_gb, "size_gb", minimum=0)
    if budget_usd is not None:
        body["budget_usd"] = _finite(budget_usd, "budget_usd", minimum=0, allow_equal=True)
    if disk_gb is not None:
        if isinstance(disk_gb, bool) or type(disk_gb) is not int or not 80 <= disk_gb <= 2048:
            raise ValidationError("disk_gb must be a whole number from 80 to 2048")
        body["disk_gb"] = disk_gb
    if repository is not None:
        body["repository"] = _text(repository, "repository", limit=512)
    if ref is not None:
        body["ref"] = _text(ref, "ref", limit=256)
    if runtime_id is not None:
        body["runtime_id"] = _text(runtime_id, "runtime_id", limit=256)
    return body


def _job(command: str, *, budget_usd: float, gpu: str | None, gpu_count: int | None,
         gpu_memory_gb: float | None) -> dict[str, Any]:
    body: dict[str, Any] = {"command": _text(command, "command", limit=8192),
                            "budget_usd": _finite(budget_usd, "budget_usd", minimum=0)}
    if gpu is not None:
        body.update(_gpu_fields(gpu, gpu_count, gpu_memory_gb, require_memory=False))
        return body
    if gpu_count is not None:
        body["gpu_count"] = _count(gpu_count, "gpu_count", _GPU_COUNTS)
    if gpu_memory_gb is not None:
        body["gpu_memory_gb"] = _finite(gpu_memory_gb, "gpu_memory_gb", minimum=0)
    return body


def _tool(tool: str) -> str:
    if tool not in _TOOLS:
        raise ValidationError("tool must be editor, notebook or ssh")
    return tool


def _schedule(ready_by: str, stop_at: str | None) -> dict[str, Any]:
    body = {"ready_by": _text(ready_by, "ready_by", limit=64)}
    if stop_at is not None:
        body["stop_at"] = _text(stop_at, "stop_at", limit=64)
    return body


def _rows(response: Any, field: str) -> list[dict[str, Any]]:
    rows = response.get(field) if isinstance(response, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise APIError(f"Workspace {field} response is invalid")
    return rows


def _page(response: Any) -> tuple[list[dict[str, Any]], str]:
    rows = _rows(response, "workspaces")
    cursor = response.get("next_cursor", "")
    if not isinstance(cursor, str) or (cursor and not rows):
        raise APIError("Workspace list cursor is invalid")
    return rows, cursor


class _WorkspaceState:
    """What one workspace view says. Every attribute reflects the last absorbed answer."""

    id: str
    name: str
    state: str
    status_message: str
    connections: dict[str, bool]
    configuration: dict[str, Any]
    configuration_revision: str
    storage_revision: int
    session: dict[str, Any] | None
    meter: dict[str, Any] | None
    storage: dict[str, Any]
    pending_upload: dict[str, Any] | None
    wake_plan: dict[str, Any] | None
    raw: dict[str, Any]

    def _init_state(self, workspace_id: str) -> None:
        self.id = workspace_id
        self.name = ""
        self.state = ""
        self.status_message = ""
        self.connections = {}
        self.configuration = {}
        self.configuration_revision = ""
        self.storage_revision = 0
        self.session = None
        self.meter = None
        self.storage = {}
        self.pending_upload = None
        self.wake_plan = None
        self.raw = {}

    def _absorb(self, view: Any) -> None:
        if not isinstance(view, dict) or not isinstance(view.get("id"), str) or not isinstance(view.get("state"), str) \
                or _RESEARCH_WORKSPACE_ID.fullmatch(view["id"]) is None:
            raise APIError("Workspace response is invalid", body=view)
        self.raw = view
        self.id = view["id"]
        self.name = view.get("name") if isinstance(view.get("name"), str) else ""
        self.state = view["state"]
        self.status_message = view.get("status_message") if isinstance(view.get("status_message"), str) else ""
        connections = view.get("connections")
        self.connections = {tool: connections.get(tool) is True for tool in _TOOLS} if isinstance(connections, dict) else {}
        self.configuration = view.get("configuration") if isinstance(view.get("configuration"), dict) else {}
        revision = view.get("configuration_revision")
        self.configuration_revision = revision if isinstance(revision, str) else ""
        storage_revision = view.get("storage_revision")
        self.storage_revision = storage_revision if type(storage_revision) is int and storage_revision >= 0 else 0
        self.session = view.get("session") if isinstance(view.get("session"), dict) else None
        self.meter = view.get("meter") if isinstance(view.get("meter"), dict) else None
        self.storage = view.get("storage") if isinstance(view.get("storage"), dict) else {}
        self.pending_upload = view.get("pending_upload") if isinstance(view.get("pending_upload"), dict) else None
        self.wake_plan = view.get("wake_plan") if isinstance(view.get("wake_plan"), dict) else None

    @property
    def session_id(self) -> str | None:
        """The running compute session, or None while stopped."""
        value = self.session.get("id") if self.session else None
        return value if isinstance(value, str) and value else None

    @property
    def tool(self) -> str:
        """The connection this workspace was configured for: editor, notebook or ssh."""
        editor = self.configuration.get("editor")
        return _EDITOR_TOOL.get(editor if isinstance(editor, str) else "", "editor")

    @property
    def ready(self) -> bool:
        """True once the configured tool accepts connections."""
        return self.connections.get(self.tool, False)

    @property
    def charge_state(self) -> str:
        """The session meter's aggregate charge state, or an empty string when absent."""
        value = self.meter.get("charge_state") if self.meter else None
        return value if isinstance(value, str) else ""

    @property
    def final_charge_usd(self) -> float | None:
        """The fixed compute charge from the server, excluding ongoing retained files."""
        value = self.meter.get("final_charge_usd") if self.meter else None
        return _optional_amount(value)

    @property
    def cost_usd(self) -> float | None:
        """The server's fixed compute charge when final, otherwise its current estimate."""
        if self.charge_state == "final":
            return self.final_charge_usd
        value = self.meter.get("total_now_usd") if self.meter else None
        return _optional_amount(value)

    def _stopped_early(self) -> str | None:
        if self.state in ("stopped", "failed", "stopping"):
            return self.status_message or f"Compute is {self.state}."
        return None

    @staticmethod
    def _with_key(error: NodusError, key: str) -> NodusError:
        """Carry the request key on an uncertain failure so a retry is the same submission."""
        if not _uncertain(error):
            return error
        if isinstance(error.body, dict):
            error.body = {**error.body, "idempotency_key": key}
        elif error.body is None:
            error.body = {"idempotency_key": key}
        if key not in error.message:
            error.message = f"{error.message}\nRetry with idempotency_key={key!r} so the retry is the same request."
            error.args = (error.message,)
        return error

    def _still_running(self) -> str | None:
        if self.state == "stopped":
            return None
        if self.state == "failed":
            return self.status_message or "The compute session failed."
        return f"The workspace is still {self.state}."

    def _start_pending(self) -> str:
        return f"The workspace is still starting ({self.state}). Compute keeps running. " \
               "Call wait_until_ready again or stop() to release it."

    def _configure_body(self, changes: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(changes) - _CONFIGURATION_FIELDS)
        if unknown:
            raise ValidationError("unknown configuration field: " + ", ".join(unknown))
        checked = {field: _validated_change(field, value) for field, value in changes.items()}
        if not self.configuration_revision:
            raise APIError("The workspace view carried no configuration revision")
        merged = {**self.configuration, **checked}
        return {"configuration_revision": self.configuration_revision,
                "configuration": {field: value for field, value in merged.items() if value is not None}}

    def _replace_revision(self) -> int | None:
        return self.storage_revision or None

    def _upload_blocked(self) -> str | None:
        if self.state != "stopped":
            return f"Stop compute before uploading files. The workspace is {self.state}."
        if self.pending_upload is not None:
            return "An earlier upload is still being verified. Wait for it to finish."
        return None


class Workspace(_WorkspaceState):
    """A handle on one GPU workspace.

    Mutable: ``refresh()``, ``start()``, ``stop()`` and the waits update this
    instance in place and return it.
    """

    def __init__(self, client: Any, workspace_id: str = ""):
        self._client = client
        self._init_state(_path(workspace_id).rsplit("/", 1)[1] if workspace_id else "")

    def _path(self, suffix: str = "") -> str:
        return _path(self.id) + suffix

    def refresh(self) -> "Workspace":
        """Re-read state, connections, session and storage."""
        self._absorb(self._client._request("GET", self._path()))
        return self

    def start(self, *, idempotency_key: str | None = None) -> "Workspace":
        """Rent compute and restore saved files. Returns before the tools are ready."""
        key = _key(idempotency_key or _fresh_key())
        try:
            self._absorb(self._client._request("POST", self._path("/start"), json={}, idempotency_key=key))
        except NodusError as error:
            raise self._with_key(error, key) from None
        return self

    def wait_until_ready(self, *, poll_seconds: float = 5.0, timeout_seconds: float = 900.0) -> "Workspace":
        """Poll until the configured tool accepts connections. A timeout leaves compute running."""
        _wait_bounds(poll_seconds, timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        while True:
            self.refresh()
            if self.ready:
                return self
            reason = self._stopped_early()
            if reason:
                raise WorkspaceNotReadyError(reason, body=self.raw)
            if time.monotonic() >= deadline:
                raise WorkspaceNotReadyError(self._start_pending(), body=self.raw)
            time.sleep(poll_seconds)

    def stop(self, *, idempotency_key: str | None = None) -> "Workspace":
        """Save project files and release compute. Already stopped is not an error."""
        self.refresh()
        if self.session_id is None:
            return self
        key = _key(idempotency_key or _fresh_key())
        try:
            self._absorb(self._client._request("POST", self._path("/stop"), json={"session_id": self.session_id},
                                               idempotency_key=key))
        except NodusError as error:
            raise self._with_key(error, key) from None
        return self

    def wait_until_stopped(self, *, poll_seconds: float = 5.0, timeout_seconds: float = 900.0) -> "Workspace":
        """Poll until compute has stopped. Read status_message to learn whether the final save succeeded."""
        _wait_bounds(poll_seconds, timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        while True:
            self.refresh()
            pending = self._still_running()
            if pending is None:
                return self
            if self.state == "failed" or time.monotonic() >= deadline:
                raise WorkspaceNotReadyError(pending, body=self.raw)
            time.sleep(poll_seconds)

    def connect(self, tool: str = "editor") -> dict[str, Any]:
        """Open a connection: a browser ``url`` for editor or notebook, SSH details for ssh."""
        return self._client._request("POST", self._path("/connections"), json={"tool": _tool(tool)})

    def ssh(self) -> dict[str, Any]:
        """SSH details: ``command``, ``ssh_config``, ``vscode_url`` and the host to reach."""
        return self.connect("ssh")

    def run(self, command: str, *, budget_usd: float, gpu: str | None = None, gpu_count: int | None = None,
            gpu_memory_gb: float | None = None, idempotency_key: str | None = None):
        """Run a command against the saved project on separate GPU compute, as a workload."""
        from . import Workload
        body = _job(command, budget_usd=budget_usd, gpu=gpu, gpu_count=gpu_count, gpu_memory_gb=gpu_memory_gb)
        key = _key(idempotency_key or _fresh_key())
        try:
            result = self._client._request("POST", self._path("/workloads"), json=body, idempotency_key=key)
            workload = Workload(self._client)
            workload._absorb(result or {})
            if not workload.id:
                raise APIError("Workspace run returned no workload id", body=result)
        except NodusError as error:
            raise self._with_key(error, key) from None
        return workload

    def workloads(self) -> list[dict[str, Any]]:
        """Workloads submitted from this workspace."""
        return _rows(self._client._request("GET", self._path("/workloads")), "workloads")

    def sessions(self) -> list[dict[str, Any]]:
        """Past and current compute sessions."""
        return _rows(self._client._request("GET", self._path("/sessions")), "sessions")

    def upload(self, directory: str | os.PathLike[str], *, idempotency_key: str | None = None,
               poll_seconds: float = 2.0, timeout_seconds: float = 600.0) -> dict[str, Any]:
        """Replace the saved project with a local folder while stopped, then wait for verification."""
        from ._workspace_files import upload_files
        _wait_bounds(poll_seconds, timeout_seconds)
        self.refresh()
        blocked = self._upload_blocked()
        if blocked:
            raise WorkspaceNotReadyError(blocked, body=self.raw)
        result = upload_files(self._client, self.id, directory, idempotency_key=idempotency_key or _fresh_key(),
                              replace_revision=self._replace_revision())
        deadline = time.monotonic() + timeout_seconds
        while True:
            self.refresh()
            if self.pending_upload is None:
                return result
            if time.monotonic() >= deadline:
                raise WorkspaceNotReadyError("The upload is still being verified.", body=self.pending_upload)
            time.sleep(poll_seconds)

    def download(self, destination: str | os.PathLike[str], *, overwrite: bool = False) -> Path:
        """Save the current project files to a local tar archive."""
        from . import _workspace_files
        self.refresh()
        return _workspace_files.export_files(self._client, self.id, destination,
                                             storage_revision=self.storage_revision, overwrite=overwrite)

    def delete_files(self) -> dict[str, Any]:
        """Delete the saved project files while stopped."""
        self.refresh()
        return self._client._request("DELETE", self._path("/files"),
                                     json={"storage_revision": self.storage_revision}, max_retries=0)

    def configure(self, **changes: Any) -> "Workspace":
        """Change saved configuration fields, such as gpu_count=2, for the next session."""
        if not self.configuration_revision:
            self.refresh()
        self._absorb(self._client._request("PATCH", self._path(), json=self._configure_body(changes)))
        return self

    def schedule(self, *, ready_by: str, stop_at: str | None = None,
                 idempotency_key: str | None = None) -> dict[str, Any]:
        """Have compute ready by an RFC 3339 time, and optionally stop at another."""
        return self._client._request("POST", self._path("/schedule"), json=_schedule(ready_by, stop_at),
                                     idempotency_key=idempotency_key or _fresh_key())

    def unschedule(self, *, idempotency_key: str | None = None) -> "Workspace":
        """Cancel a schedule. Running compute is not affected."""
        self._absorb(self._client._request("DELETE", self._path("/schedule"),
                                           idempotency_key=idempotency_key or _fresh_key()))
        return self


class AsyncWorkspace(_WorkspaceState):
    """Asynchronous counterpart of :class:`Workspace`."""

    def __init__(self, client: Any, workspace_id: str = ""):
        self._client = client
        self._init_state(_path(workspace_id).rsplit("/", 1)[1] if workspace_id else "")

    def _path(self, suffix: str = "") -> str:
        return _path(self.id) + suffix

    async def refresh(self) -> "AsyncWorkspace":
        self._absorb(await self._client._request("GET", self._path()))
        return self

    async def start(self, *, idempotency_key: str | None = None) -> "AsyncWorkspace":
        key = _key(idempotency_key or _fresh_key())
        try:
            self._absorb(await self._client._request("POST", self._path("/start"), json={}, idempotency_key=key))
        except NodusError as error:
            raise self._with_key(error, key) from None
        return self

    async def wait_until_ready(self, *, poll_seconds: float = 5.0, timeout_seconds: float = 900.0) -> "AsyncWorkspace":
        _wait_bounds(poll_seconds, timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        while True:
            await self.refresh()
            if self.ready:
                return self
            reason = self._stopped_early()
            if reason:
                raise WorkspaceNotReadyError(reason, body=self.raw)
            if time.monotonic() >= deadline:
                raise WorkspaceNotReadyError(self._start_pending(), body=self.raw)
            await asyncio.sleep(poll_seconds)

    async def stop(self, *, idempotency_key: str | None = None) -> "AsyncWorkspace":
        await self.refresh()
        if self.session_id is None:
            return self
        key = _key(idempotency_key or _fresh_key())
        try:
            self._absorb(await self._client._request("POST", self._path("/stop"), json={"session_id": self.session_id},
                                                     idempotency_key=key))
        except NodusError as error:
            raise self._with_key(error, key) from None
        return self

    async def wait_until_stopped(self, *, poll_seconds: float = 5.0, timeout_seconds: float = 900.0) -> "AsyncWorkspace":
        _wait_bounds(poll_seconds, timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        while True:
            await self.refresh()
            pending = self._still_running()
            if pending is None:
                return self
            if self.state == "failed" or time.monotonic() >= deadline:
                raise WorkspaceNotReadyError(pending, body=self.raw)
            await asyncio.sleep(poll_seconds)

    async def connect(self, tool: str = "editor") -> dict[str, Any]:
        return await self._client._request("POST", self._path("/connections"), json={"tool": _tool(tool)})

    async def ssh(self) -> dict[str, Any]:
        return await self.connect("ssh")

    async def run(self, command: str, *, budget_usd: float, gpu: str | None = None, gpu_count: int | None = None,
                  gpu_memory_gb: float | None = None, idempotency_key: str | None = None):
        from . import AsyncWorkload
        body = _job(command, budget_usd=budget_usd, gpu=gpu, gpu_count=gpu_count, gpu_memory_gb=gpu_memory_gb)
        key = _key(idempotency_key or _fresh_key())
        try:
            result = await self._client._request("POST", self._path("/workloads"), json=body, idempotency_key=key)
            workload = AsyncWorkload(self._client)
            workload._absorb(result or {})
            if not workload.id:
                raise APIError("Workspace run returned no workload id", body=result)
        except NodusError as error:
            raise self._with_key(error, key) from None
        return workload

    async def workloads(self) -> list[dict[str, Any]]:
        return _rows(await self._client._request("GET", self._path("/workloads")), "workloads")

    async def sessions(self) -> list[dict[str, Any]]:
        return _rows(await self._client._request("GET", self._path("/sessions")), "sessions")

    async def upload(self, directory: str | os.PathLike[str], *, idempotency_key: str | None = None,
                     poll_seconds: float = 2.0, timeout_seconds: float = 600.0) -> dict[str, Any]:
        from ._workspace_files import upload_files_async
        _wait_bounds(poll_seconds, timeout_seconds)
        await self.refresh()
        blocked = self._upload_blocked()
        if blocked:
            raise WorkspaceNotReadyError(blocked, body=self.raw)
        result = await upload_files_async(self._client, self.id, directory,
                                          idempotency_key=idempotency_key or _fresh_key(),
                                          replace_revision=self._replace_revision())
        deadline = time.monotonic() + timeout_seconds
        while True:
            await self.refresh()
            if self.pending_upload is None:
                return result
            if time.monotonic() >= deadline:
                raise WorkspaceNotReadyError("The upload is still being verified.", body=self.pending_upload)
            await asyncio.sleep(poll_seconds)

    async def download(self, destination: str | os.PathLike[str], *, overwrite: bool = False) -> Path:
        from . import _workspace_files
        await self.refresh()
        return await _workspace_files.export_files_async(self._client, self.id, destination,
                                                         storage_revision=self.storage_revision, overwrite=overwrite)

    async def delete_files(self) -> dict[str, Any]:
        await self.refresh()
        return await self._client._request("DELETE", self._path("/files"),
                                           json={"storage_revision": self.storage_revision}, max_retries=0)

    async def configure(self, **changes: Any) -> "AsyncWorkspace":
        if not self.configuration_revision:
            await self.refresh()
        self._absorb(await self._client._request("PATCH", self._path(), json=self._configure_body(changes)))
        return self

    async def schedule(self, *, ready_by: str, stop_at: str | None = None,
                       idempotency_key: str | None = None) -> dict[str, Any]:
        return await self._client._request("POST", self._path("/schedule"), json=_schedule(ready_by, stop_at),
                                           idempotency_key=idempotency_key or _fresh_key())

    async def unschedule(self, *, idempotency_key: str | None = None) -> "AsyncWorkspace":
        self._absorb(await self._client._request("DELETE", self._path("/schedule"),
                                                 idempotency_key=idempotency_key or _fresh_key()))
        return self


def _legacy_volume(kwargs: dict[str, Any]) -> bool:
    """A create with a size and no compute names a sandbox volume."""
    return kwargs.get("gpu") is None and kwargs.get("cpus") is None and kwargs.get("size_gb") is not None \
        and kwargs.get("gpu_count") == 1 and kwargs.get("editor") == "vscode" \
        and all(value is None for field, value in kwargs.items() if field not in ("size_gb", "gpu_count", "editor"))


def _by_name(rows: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    matches = [row for row in rows if row.get("name") == name]
    return matches[0] if len(matches) == 1 else None


class Workspaces:
    """Create and find GPU workspaces. Each call returns a :class:`Workspace` handle."""

    def __init__(self, client: Any):
        self._client = client

    def capabilities(self) -> dict[str, Any]:
        """What this deployment offers: environments, GPU counts, editors and storage limits."""
        return self._client._request("GET", _BASE + "/capabilities")

    def storage(self) -> dict[str, Any]:
        """The account's saved-file usage, allowance and charges."""
        return self._client._request("GET", _BASE + "/storage")

    def create(self, name: str, *, gpu: str | None = None, gpu_count: int = 1, gpu_memory_gb: float | None = None,
               environment: str | None = None, editor: str = "vscode", max_hours: int | None = None,
               size_gb: float | None = None, budget_usd: float | None = None, ssh_key: str | None = None,
               cpus: int | None = None, memory_gb: float | None = None, disk_gb: int | None = None,
               repository: str | None = None, ref: str | None = None, runtime_id: str | None = None,
               form_factor: str | None = None) -> Any:
        """Save a workspace configuration. Nothing is rented until ``start()``."""
        options = dict(gpu=gpu, gpu_count=gpu_count, gpu_memory_gb=gpu_memory_gb, environment=environment,
                       editor=editor, max_hours=max_hours, size_gb=size_gb, budget_usd=budget_usd, ssh_key=ssh_key,
                       cpus=cpus, memory_gb=memory_gb, disk_gb=disk_gb, repository=repository, ref=ref,
                       runtime_id=runtime_id, form_factor=form_factor)
        if _legacy_volume(options):
            warnings.warn("client.workspaces.create(name, size_gb=...) creates a sandbox volume. "
                          "Use client.volumes.create for volumes and pass gpu= for a GPU workspace.",
                          FutureWarning, stacklevel=2)
            return self._client.volumes.create(name, size_gb=size_gb)
        body = _configuration(name, **options)
        if "size_gb" not in body:
            body["size_gb"] = _default_size_gb(self.capabilities())
        workspace = Workspace(self._client)
        workspace._absorb(self._client._request("POST", _BASE, json=body))
        return workspace

    def delete_files(self, workspace_id: str, *, storage_revision: int) -> dict[str, Any]:
        """Delete stopped saved files once, guarded by the observed revision."""
        from ._workspace_files import revision
        return self._client._request("DELETE", _path(workspace_id) + "/files",
                                     json={"storage_revision": revision(storage_revision)}, max_retries=0)

    def export_files(self, workspace_id: str, destination: str | os.PathLike[str], *,
                     storage_revision: int, overwrite: bool = False) -> Path:
        """Stream one observed revision of saved files to a verified local tar archive."""
        from . import _workspace_files
        return _workspace_files.export_files(self._client, workspace_id, destination,
                                             storage_revision=storage_revision, overwrite=overwrite)

    def upload_files(self, workspace_id: str, directory: str | os.PathLike[str], *,
                     idempotency_key: str, replace_revision: int | None = None) -> dict[str, Any]:
        """Upload a stopped project once and return its pending verification status."""
        from ._workspace_files import upload_files
        return upload_files(self._client, workspace_id, directory,
                            idempotency_key=idempotency_key, replace_revision=replace_revision)

    def get(self, reference: str) -> Workspace:
        """A workspace by ID, or by its name when the name is unique."""
        _text(reference, "reference", limit=256)
        workspace = Workspace(self._client)
        if _RESEARCH_WORKSPACE_ID.fullmatch(reference):
            workspace._init_state(reference)
            try:
                return workspace.refresh()
            except NotFoundError as missing:
                found = _by_name([ws.raw for ws in self.list()], reference)
                if found is None:
                    raise missing
                workspace._absorb(found)
                return workspace
        found = _by_name([ws.raw for ws in self.list()], reference)
        if found is None:
            raise NotFoundError(f"No workspace is named {reference!r}", status_code=404)
        workspace._absorb(found)
        return workspace

    def list(self) -> list[Workspace]:
        """Every workspace the account can see."""
        result: list[Workspace] = []
        cursor, seen = "", set()
        while True:
            params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
            rows, cursor = _page(self._client._request("GET", _BASE, params=params))
            for row in rows:
                workspace = Workspace(self._client)
                workspace._absorb(row)
                result.append(workspace)
            if not cursor:
                return result
            if cursor in seen:
                raise APIError("Workspace pagination repeated a cursor")
            seen.add(cursor)


class AsyncWorkspaces:
    def __init__(self, client: Any):
        self._client = client

    async def capabilities(self) -> dict[str, Any]:
        return await self._client._request("GET", _BASE + "/capabilities")

    async def storage(self) -> dict[str, Any]:
        return await self._client._request("GET", _BASE + "/storage")

    async def create(self, name: str, *, gpu: str | None = None, gpu_count: int = 1,
                     gpu_memory_gb: float | None = None, environment: str | None = None, editor: str = "vscode",
                     max_hours: int | None = None, size_gb: float | None = None, budget_usd: float | None = None,
                     ssh_key: str | None = None, cpus: int | None = None, memory_gb: float | None = None,
                     disk_gb: int | None = None, repository: str | None = None, ref: str | None = None,
                     runtime_id: str | None = None, form_factor: str | None = None) -> Any:
        options = dict(gpu=gpu, gpu_count=gpu_count, gpu_memory_gb=gpu_memory_gb, environment=environment,
                       editor=editor, max_hours=max_hours, size_gb=size_gb, budget_usd=budget_usd, ssh_key=ssh_key,
                       cpus=cpus, memory_gb=memory_gb, disk_gb=disk_gb, repository=repository, ref=ref,
                       runtime_id=runtime_id, form_factor=form_factor)
        if _legacy_volume(options):
            warnings.warn("client.workspaces.create(name, size_gb=...) creates a sandbox volume. "
                          "Use client.volumes.create for volumes and pass gpu= for a GPU workspace.",
                          FutureWarning, stacklevel=2)
            return await self._client.volumes.create(name, size_gb=size_gb)
        body = _configuration(name, **options)
        if "size_gb" not in body:
            body["size_gb"] = _default_size_gb(await self.capabilities())
        workspace = AsyncWorkspace(self._client)
        workspace._absorb(await self._client._request("POST", _BASE, json=body))
        return workspace

    async def delete_files(self, workspace_id: str, *, storage_revision: int) -> dict[str, Any]:
        from ._workspace_files import revision
        return await self._client._request("DELETE", _path(workspace_id) + "/files",
                                           json={"storage_revision": revision(storage_revision)}, max_retries=0)

    async def export_files(self, workspace_id: str, destination: str | os.PathLike[str], *,
                           storage_revision: int, overwrite: bool = False) -> Path:
        from . import _workspace_files
        return await _workspace_files.export_files_async(self._client, workspace_id, destination,
                                                         storage_revision=storage_revision, overwrite=overwrite)

    async def upload_files(self, workspace_id: str, directory: str | os.PathLike[str], *,
                           idempotency_key: str, replace_revision: int | None = None) -> dict[str, Any]:
        from ._workspace_files import upload_files_async
        return await upload_files_async(self._client, workspace_id, directory,
                                        idempotency_key=idempotency_key, replace_revision=replace_revision)

    async def get(self, reference: str) -> AsyncWorkspace:
        _text(reference, "reference", limit=256)
        workspace = AsyncWorkspace(self._client)
        if _RESEARCH_WORKSPACE_ID.fullmatch(reference):
            workspace._init_state(reference)
            try:
                return await workspace.refresh()
            except NotFoundError as missing:
                found = _by_name([ws.raw for ws in await self.list()], reference)
                if found is None:
                    raise missing
                workspace._absorb(found)
                return workspace
        found = _by_name([ws.raw for ws in await self.list()], reference)
        if found is None:
            raise NotFoundError(f"No workspace is named {reference!r}", status_code=404)
        workspace._absorb(found)
        return workspace

    async def list(self) -> list[AsyncWorkspace]:
        result: list[AsyncWorkspace] = []
        cursor, seen = "", set()
        while True:
            params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
            rows, cursor = _page(await self._client._request("GET", _BASE, params=params))
            for row in rows:
                workspace = AsyncWorkspace(self._client)
                workspace._absorb(row)
                result.append(workspace)
            if not cursor:
                return result
            if cursor in seen:
                raise APIError("Workspace pagination repeated a cursor")
            seen.add(cursor)

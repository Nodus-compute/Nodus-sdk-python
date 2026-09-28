"""The ``nodus`` command.

Run a workload file and inspect its progress with short, explicit commands.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
import warnings
import webbrowser
from typing import Any

from . import Client, SandboxExec, __version__, _is_header_safe, _redact, _resolve_base_url, _current_hosted_url, config, login
from ._terminal import clean, compute_label, format_cost, show_table, show_workload, status_label, workload_cost
from ._brief import STATUS_FILTERS
from .errors import ValidationError, NodusError, NotFoundError, AuthenticationError, APIError, APIConnectionError, APITimeoutError, asset_id_from_error
from .types import _num
from ._workload_file import load_workload_file, write_workload_file
from ._project_cli import add_source_arguments, source_options
from ._client_identity import acting_as

# Nearly everything printed here was written somewhere else, and a terminal
# acts on whatever escapes it is handed. The rule between the two cleaners:
# _safe is only for values that may legitimately span lines, and every value
# that is one line by definition goes through _safe_line. A newline in a
# one-line value is not formatting -- it forges a whole row of output that
# reads exactly like the tool's own. The --json dumps are the one exception:
# json.dumps escapes every control character itself.
#
# C0 and C1 controls, minus tab and newline. This keeps them because it also
# cleans a workload's own log, which is nothing but lines.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# The same, sparing nothing: an id, a status, a SKU, a code, an address.
_CONTROL_LINE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _positive_seconds(value: str) -> float:
    """Reject invalid observation settings before creating a workload."""
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a finite positive number of seconds") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("expected a finite positive number of seconds")
    return seconds


def _positive_cost(value: str) -> float:
    """Reject invalid spending limits before creating a sandbox."""
    try:
        cost = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a finite positive USD amount") from exc
    if not math.isfinite(cost) or cost <= 0:
        raise argparse.ArgumentTypeError("expected a finite positive USD amount")
    return cost


def _nonnegative_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a nonnegative integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("expected a nonnegative integer")
    return number


def _page_limit(value: str) -> int:
    number = _nonnegative_integer(value)
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("expected an integer from 1 to 100")
    return number


def _positive_integer(value: str) -> int:
    number = _nonnegative_integer(value)
    if number == 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def _safe(text: Any) -> str:
    """Many-line text from elsewhere, with what a terminal acts on removed."""
    return clean(text)


def _safe_line(text: Any) -> str:
    """A one-line value from elsewhere, with tab and newline gone too."""
    return clean(text, line=True)


def _fmt_workload(wl: Any) -> str:
    # Terminal metered costs remain explicitly pending until their lifetime total is final.
    route = _safe_line(wl.route.sku) if wl.route else "-"
    status = _safe_line(getattr(wl.status, "value", wl.status))
    return f"{_safe_line(wl.id)}  {status:<13} {route:<28} {workload_cost(wl)}"


@contextmanager
def _wait_activity(workload_id: str):
    if not sys.stderr.isatty():
        yield
        return
    stopped = threading.Event()
    started = time.monotonic()
    label = _safe_line(workload_id)

    def render():
        frame = 0
        while True:
            elapsed = int(time.monotonic() - started)
            marker = "|/-\\"[frame % 4]
            print(f"\r{marker} Waiting for {label}  {elapsed}s elapsed", end="", file=sys.stderr, flush=True)
            if stopped.wait(0.2):
                return
            frame += 1

    worker = threading.Thread(target=render, name="nodus-wait", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()
        print(file=sys.stderr)


@contextmanager
def _cancel_on_interrupt(client: Client, workload_id: str):
    try:
        yield
    except KeyboardInterrupt as interrupt:
        display_id = _safe_line(workload_id)
        print(f"\nRequesting cancellation for {display_id}...", file=sys.stderr)
        try:
            prior = getattr(interrupt, "_nodus_cancellation", None)
            if prior is not None and prior[0] == workload_id:
                if prior[1] is not None:
                    raise prior[1]
            else:
                client.cancel(workload_id)
        except (NodusError, KeyboardInterrupt) as exc:
            reason = "Interrupted again" if isinstance(exc, KeyboardInterrupt) else _safe_line(exc)
            print(
                f"Cancellation not confirmed for {display_id}: {reason}. "
                f"Run nodus cancel {display_id} against the same API deployment.",
                file=sys.stderr,
            )
        else:
            print(
                f"Cancellation requested for {display_id}. "
                "Nodus is stopping the workload and releasing its resources.",
                file=sys.stderr,
            )
        raise


def _cmd_init(args: argparse.Namespace) -> int:
    path = write_workload_file(args.file)
    filename = str(path)
    if filename == "nodus.toml":
        print(f"Created {_safe_line(path)}. Edit it, then run nodus run.")
    elif re.fullmatch(r"[\w .:/-]+", filename) and not filename.startswith("-"):
        print(f'Created {_safe_line(path)}. Edit it, then run:\nnodus run "{filename}"')
    else:
        print(f"Created {_safe_line(path)}. Edit it, then pass its filename to nodus run using your shell's quoting rules.")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    settings = load_workload_file(args.file)
    submission_key = settings.get("idempotency_key") or str(uuid.uuid4())
    settings["idempotency_key"] = submission_key
    with Client(base_url=args.base_url) as client:
        try:
            wl = client.run(**settings)
        except (KeyboardInterrupt, NodusError) as exc:
            if isinstance(exc, KeyboardInterrupt) or exc.status_code is None or exc.status_code >= 500:
                print(
                    "Submission outcome unknown. A workload may still be running. "
                    "Before retrying, set the following top-level value in the same workload file: "
                    f"idempotency_key = {json.dumps(submission_key)}. "
                    "Retry with nodus submit, then cancel the returned workload if needed.",
                    file=sys.stderr,
                )
            raise
        print(_safe_line(wl.id), flush=True)
        if args.cmd == "submit":
            return 0
        seen_links: set[str] = set()
        def show_live_links(current):
            for link in current.links:
                if link.url not in seen_links:
                    print(f"wandb: {link.url}", flush=True)
                    seen_links.add(link.url)
        with _cancel_on_interrupt(client, wl.id):
            wl.wait(poll_seconds=args.poll, timeout_seconds=args.timeout,
                    progress=False if args.plain else None, on_update=show_live_links)
        show_workload(wl, plain=args.plain)
        return 0 if wl.succeeded else 1


def _cmd_upload(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        raise ValueError("Upload a file or archive that exists on this computer.")
    with Client(base_url=args.base_url) as client:
        asset = client.assets.upload(path)
    print(_safe_line(asset.id))
    return 0


def _cmd_secret(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        if args.secret_cmd == "set":
            if args.from_file:
                with Path(args.from_file).open("r", encoding="utf-8", newline="") as source:
                    value = source.read(4097)
            else:
                if sys.stdin.isatty():
                    raise ValidationError("Pipe the secret on stdin or use --from-file")
                value = sys.stdin.read(4097)
            metadata = client.secrets.put(args.name, value)
            print(f"Stored {_safe_line(metadata['name'])} version {_safe_line(metadata['version'])}")
        elif args.secret_cmd == "ls":
            show_table(["Name", "Version", "Created"],
                       [[_safe_line(item.get("name", "")), _safe_line(item.get("version", "")),
                         _safe_line(item.get("created_at", ""))] for item in client.secrets.list()],
                       empty="No secrets.", plain=args.plain)
        else:
            client.secrets.delete(args.name)
            print(f"Revoked {_safe_line(args.name)}")
    return 0


def _cmd_connection(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        if args.connection_cmd == "add":
            result = client.connections.create(args.name, args.kind, secret=args.secret,
                scope=args.scope, region=args.region, live=args.live, branch=args.branch,
                entity=args.entity, project=args.project)
            print(f"Created {_safe_line(result['name'])} ({_safe_line(result['id'])})")
        elif args.connection_cmd == "ls":
            show_table(["ID", "Name", "Kind", "Region", "Live", "Verified"],
                [[_safe_line(c.get(k, "")) for k in ("id", "name", "kind", "region", "live_mode", "verified_at")]
                 for c in client.connections.list()], empty="No connections.", plain=args.plain)
        elif args.connection_cmd == "verify":
            result = client.connections.verify(args.connection)
            print(f"Verified {_safe_line(result['name'])} at {_safe_line(result['verified_at'])}")
        else:
            client.connections.delete(args.connection)
            print(f"Deleted {_safe_line(args.connection)}")
    return 0


def _query_recovery(exc: BaseException) -> str | None:
    asset_id = asset_id_from_error(exc)
    if asset_id is not None:
        return f"Query export {asset_id} was admitted. Inspect it with nodus asset get {asset_id} before repeating the import."
    return None


def _cmd_asset(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        if args.asset_cmd == "get":
            asset = client.assets.get(args.asset_id)
            for label, value in (("Asset", asset.id), ("Status", asset.state),
                                 ("Name", asset.name), ("Stored bytes", asset.stored_bytes)):
                print(f"{label}: {_safe_line(value)}")
            if asset.error:
                print(f"Error: {_safe_line(asset.error)}")
            if isinstance(asset.export, dict):
                for label, key in (("Format", "format"), ("Rows", "row_count"), ("Export bytes", "bytes")):
                    if key in asset.export:
                        print(f"{label}: {_safe_line(asset.export[key])}")
        else:
            asset = client.assets.import_query(args.connection, args.sql, format=args.format,
                                               branch=args.branch, reuse=args.reuse)
            print(_safe_line(asset.id))
    return 0


def _cmd_assets(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        show_table(["Asset", "Status", "Name"],
                   [[asset.id, asset.state, asset.name] for asset in client.assets.list()],
                   empty="No assets yet. Use nodus upload FILE to add one.", plain=args.plain)
    return 0


def _resolve_sandbox(client, reference):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", reference):
        raise ValidationError("Use a sandbox ID or a name of up to 128 letters, digits, dots, underscores or dashes.")
    missing = None
    if reference.startswith("sb_") and "." not in reference:
        try:
            box = client.sandboxes.from_id(reference)
        except NotFoundError as error:
            if error.code != "not_found" or re.fullmatch(r"sb_[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", reference):
                raise
            missing = error
        else:
            return box
    cursor = None
    seen = set()
    matches = {}
    while True:
        rows, next_cursor = client.sandboxes.list_page(name=reference, cursor=cursor)
        for box in rows:
            if box.envelope.get("name") != reference or box.is_terminal:
                continue
            matches[box.id] = box
        if len(matches) > 1:
            raise ValidationError("More than one active sandbox matches that name. Use an exact ID from nodus sandbox ls.")
        if next_cursor is None:
            break
        if next_cursor in seen:
            raise ValidationError("Sandbox pagination did not advance. Retry the command.")
        seen.add(next_cursor)
        cursor = next_cursor
    if not matches:
        if missing:
            raise missing
        raise ValidationError("No active sandbox with that name was found. Check nodus sandbox ls and use its ID.")
    return next(iter(matches.values()))


@contextmanager
def _sandbox_mutation(request_key, *, sandbox_id=None, noun="sandbox"):
    key = request_key or f"{noun}-cli-{uuid.uuid4()}"
    try:
        yield key
    except (KeyboardInterrupt, NodusError) as error:
        uncertain = isinstance(error, (KeyboardInterrupt, APIConnectionError, APITimeoutError))
        if isinstance(error, NodusError):
            uncertain = uncertain or bool(error.status_code and error.status_code >= 500)
            uncertain = uncertain or (isinstance(error, APIError) and error.status_code is None)
        if uncertain:
            identity = f" Use {noun} ID {_safe_line(sandbox_id)} instead of its name." if sandbox_id else ""
            print(
                "Request outcome unknown. The operation may have been accepted."
                + identity + " Retry the unchanged operation with --idempotency-key="
                + shlex.quote(key) + " before the positional arguments. Do not submit it with a new key.",
                file=sys.stderr,
            )
        raise


def _cmd_freeze(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        method = {"freeze": client.freeze, "freeze-status": client.freeze_status, "resume": client.resume}[args.cmd]
        result = method(args.workload_id)
        print(_safe_line(f"{result.workload_id}: {result.state}. Retained checkpoint: {result.retained_bytes} bytes."))
        print("Retained storage is not separately metered. No storage charge is reported.")
        print("Resume restarts the command with saved files. Your program must load its checkpoint.")
    return 0


def _cmd_pools(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        if args.pools_cmd == "create":
            print(_safe_line(client.pools.create(args.name).id))
        elif args.pools_cmd == "token":
            print(client.pools.enrollment_token(args.pool_id, mode=args.mode, host_id=args.host_id).token)
        elif args.pools_cmd in ("route", "route-settings"):
            settings = {name: getattr(args, name) for name in ("wait_policy", "wait_alpha", "waiting_budget_pct", "burst_approval", "burst_threshold_micros", "burst_timeout_behaviour")}
            if args.pools_cmd == "route":
                pool = client.pools.set_route(args.pool_id, args.setting == "on",
                    accepted_rate_version=args.accept_rate_version, accepted_rate_micros=args.accept_rate_micros, **settings)
                print(_safe_line(f"{pool.id}: Route {'enabled' if pool.route_enabled else 'disabled'}"))
            else:
                pool = client.pools.update_route_settings(args.pool_id, **settings)
                print(_safe_line(f"{pool.id}: Route settings updated"))
        elif args.pools_cmd == "predict":
            pool = client.pools.set_predict(args.pool_id, args.setting == "on",
                accepted_rate_version=args.accept_rate_version, accepted_monthly_micros=args.accept_monthly_micros)
            print(_safe_line(f"{pool.id}: Predict {'enabled' if pool.predict_enabled else 'disabled'}"))
        elif args.pools_cmd == "forecast":
            forecast = client.pools.forecast(args.pool_id, horizon=args.horizon)
            if args.json:
                print(json.dumps(forecast.raw, indent=2))
            else:
                print(_safe_line(f"Refresh: {forecast.refresh_status}. Subscription monthly rate: {forecast.subscription.monthly_micros} USD micros."))
                if forecast.snapshot is None:
                    print("No forecast cached.")
                else:
                    snapshot = forecast.snapshot
                    calibration = snapshot.calibration
                    print(_safe_line(f"Model: {snapshot.forecast.model}. Generated: {snapshot.generated_at}. Owned devices: {snapshot.owned_devices}."))
                    print("Issued hourly coverage: " + ("Not available" if calibration.hourly_coverage is None else f"{calibration.hourly_coverage:.1%}"))
                    show_table(["UTC hour", "p10 device-hours", "p50 device-hours", "p90 device-hours"],
                        [[point.hour, str(point.p10), str(point.p50), str(point.p90)] for point in snapshot.forecast.points],
                        empty="Insufficient measured history for a forecast.", plain=args.plain)
        elif args.pools_cmd in ("action-policies", "action-policy", "act-kill-switch"):
            if args.pools_cmd == "action-policy":
                settings = client.pools.set_action_policy(args.pool_id, kind=args.kind, level=args.level,
                    window_cron=args.window_cron, parallelism_cap=args.parallelism_cap)
            elif args.pools_cmd == "act-kill-switch":
                settings = client.pools.set_act_kill_switch(args.pool_id, args.setting == "on")
            else:
                settings = client.pools.action_policies(args.pool_id)
            print(_safe_line(f"Act kill switch: {'on' if settings.kill_switch else 'off'}"))
            show_table(["Kind", "Level", "UTC window", "Parallelism", "Shadow qualified"],
                [[p.kind, p.level, p.window_cron, str(p.parallelism_cap), str(p.shadow_qualified)] for p in settings.policies], empty="No action policies returned.", plain=args.plain)
        elif args.pools_cmd == "start-shadow":
            run = client.pools.start_shadow(args.pool_id, kind=args.kind, level=args.level,
                window_cron=args.window_cron, parallelism_cap=args.parallelism_cap)
            print(_safe_line(f"{run.id}: {run.state}. Shadow observations do not execute actions."))
        elif args.pools_cmd == "shadows":
            page = client.pools.shadow_runs(args.pool_id, limit=args.limit, cursor=args.cursor, kind=args.kind)
            if args.json:
                print(json.dumps(page.raw, indent=2))
            else:
                show_table(["Run", "Kind", "Trusted hours", "Elapsed gaps", "Would have acted", "Qualified"],
                    [[r.id, r.kind, f"{r.trusted_hours}/168", str(r.gap_hours), str(r.would_have_acted), str(r.qualifies_auto)] for r in page.runs],
                    empty="No shadow runs. Automatic actions are not qualified.", plain=args.plain)
                if page.next_cursor:
                    print(_safe_line("Next cursor: " + page.next_cursor))
        elif args.pools_cmd == "action-proposals":
            page = client.pools.action_proposals(args.pool_id, limit=args.limit, cursor=args.cursor, kind=args.kind)
            if args.json:
                print(json.dumps(page.raw, indent=2))
            else:
                show_table(["Action", "Kind", "State", "Observed outcome"],
                    [[p.id, p.kind, p.state, p.outcome.result if p.outcome else "Not available"] for p in page.proposals],
                    empty="No Act proposals.", plain=args.plain)
                if page.next_cursor:
                    print(_safe_line("Next cursor: " + page.next_cursor))
        elif args.pools_cmd in ("approve-action", "reject-action"):
            method = client.pools.approve_action_proposal if args.pools_cmd == "approve-action" else client.pools.reject_action_proposal
            proposal = method(args.pool_id, args.proposal_id)
            print(_safe_line(f"{proposal.id}: {proposal.state}. Approval alone does not confirm application or savings."))
        elif args.pools_cmd == "proposals":
            page = client.pools.proposals(args.pool_id, limit=args.limit, cursor=args.cursor, state=args.state)
            if args.json:
                print(json.dumps(page.raw, indent=2))
            else:
                show_table(["Proposal", "Workload", "Proposed cost (USD micros)", "State", "Reason"],
                    [[item.id, item.workload_id, str(item.expected_cost_micros), item.state, item.reason] for item in page.proposals],
                    empty="No burst proposals.", plain=args.plain)
                if page.next_cursor:
                    print(_safe_line("Next cursor: " + page.next_cursor))
        elif args.pools_cmd in ("approve", "reject"):
            method = client.pools.approve_proposal if args.pools_cmd == "approve" else client.pools.reject_proposal
            proposal = method(args.pool_id, args.proposal_id)
            print(_safe_line(f"{proposal.id}: {proposal.state}. Proposed cost: {proposal.expected_cost_micros} USD micros."))
            print("Approval records intent and does not itself rent capacity. Only applied records an observed winning execution.")
        elif args.pools_cmd == "recommendations":
            advice = client.pools.recommendations(args.pool_id, limit=args.limit, cursor=args.cursor, state=args.state)
            if args.json:
                print(json.dumps(advice.raw, indent=2))
            else:
                print(_safe_line(f"Refresh: {advice.refresh_status}. Savings below are estimates, not measured outcomes."))
                show_table(["Recommendation", "Kind", "Risk", "Estimated savings (USD micros)", "State"],
                    [[item.id, item.kind, item.recommendation["risk"], ("Not available" if item.recommendation["expected_savings_micros"] is None else str(item.recommendation["expected_savings_micros"])), item.state]
                     for item in advice.recommendations], empty="No recommendations available.", plain=args.plain)
                if advice.next_cursor:
                    print(_safe_line("Next cursor: " + advice.next_cursor))
        elif args.pools_cmd == "mark-done":
            outcome = client.pools.recommendation_done(args.pool_id, args.recommendation_id, args.outcome,
                reported_saving_micros=args.reported_saving_micros)
            print(_safe_line("Customer-reported outcome: " + outcome.outcome))
            print("Customer-reported saving (USD micros): " + ("Not provided" if outcome.reported_saving_micros is None else str(outcome.reported_saving_micros)))
        elif args.pools_cmd == "utilization":
            ledger = client.pools.utilization(args.pool_id, from_=args.from_, to=args.to, bucket=args.bucket)
            if args.json:
                print(json.dumps(ledger.raw, indent=2))
            else:
                print(_safe_line(f"{ledger.from_} to {ledger.to} ({ledger.bucket}, UTC)"))
                rows = []
                for label, metrics in [("Pool", ledger.summary), *[(host.name, host.summary) for host in ledger.hosts]]:
                    percentages = ["Unknown" if value is None else f"{value:g}%" for value in
                                   (metrics.allocation_pct, metrics.busy_pct, metrics.busy_of_allocated_pct)]
                    rows.append([label, metrics.data_status, *percentages])
                show_table(["Scope", "Data", "Allocated", "Busy", "Busy / allocated"], rows, empty="No utilization data.", plain=args.plain)
                show_table(["Metric", "Seconds"],
                           [[name.replace("_seconds", "").replace("_", " ").capitalize(),
                             str(value) if value is not None else ("Not available" if name in
                             ("fragmentation_seconds", "queued_seconds", "burst_seconds") else "Unknown")]
                            for name in ledger.summary.__dataclass_fields__ if name.endswith("_seconds")
                            for value in [getattr(ledger.summary, name)]], empty="No utilization data.", plain=args.plain)
                cost = ledger.summary.burst_cost_micros
                print("Settled burst cost (USD micros): " + ("Not available" if cost is None else str(cost)))
                print("Queued and fragmentation values use workload-seconds. Burst uses device-seconds.")
        else:
            hosts = client.pools.hosts(args.pool_id)
            show_table(["Host", "Name", "Health", "Mode", "Devices"],
                       [[host.id, host.name, host.state, host.agent_mode, str(len(host.devices))]
                        for host in hosts], empty="No enrolled hosts yet.", plain=args.plain)
    return 0


def _cmd_agent(args: argparse.Namespace) -> int:
    from ._agent_cli import run
    return run(args, Client)


def _cmd_sandbox(args: argparse.Namespace) -> int:
    source = source_options(args) if args.sandbox_cmd == "new" else None
    with Client(base_url=args.base_url) as client:
        if args.sandbox_cmd == "new":
            template = args.template
            if template is None and args.image is None and args.github_repo is not None:
                template = "nodus:agent-tools-v1"
            with _sandbox_mutation(args.idempotency_key) as key:
                sandbox = client.sandboxes.create(
                    image=args.image,
                    name=args.name,
                    budget=args.budget,
                    template=template,
                    idempotency_key=key,
                    **source,
                )
            print(_safe_line(sandbox.id))
            return 0
        if args.sandbox_cmd == "ls":
            sandboxes = client.sandboxes.list(limit=args.limit)
            if args.json:
                print(json.dumps([
                    {
                        "id": sandbox.id,
                        "name": sandbox.envelope.get("name", ""),
                        "state": getattr(sandbox.state, "value", sandbox.state),
                        "cost_usd": sandbox.cost_usd,
                        "charge_state": sandbox.charge_state,
                        "final_charge_usd": sandbox.final_charge_usd,
                        "url": sandbox.url,
                    }
                    for sandbox in sandboxes
                ], indent=2, default=str))
            else:
                show_table(
                    ["Sandbox", "Name", "Status", "Cost"],
                    [[sandbox.id, sandbox.envelope.get("name", ""), sandbox.state, _sandbox_cost(sandbox)] for sandbox in sandboxes],
                    empty="No sandboxes yet. Use nodus sandbox new to create one.",
                    plain=args.plain,
                )
            return 0
        if args.sandbox_cmd == "exec":
            sandbox = _resolve_sandbox(client, args.sandbox_id)
            command: str | list[str] = args.command[0] if len(args.command) == 1 else args.command
            with _sandbox_mutation(args.idempotency_key, sandbox_id=sandbox.id) as key:
                process = sandbox.exec(command, cwd=args.cwd, idempotency_key=key)
            try:
                for frame in process.iter_output():
                    print(_safe(frame.text), end="", file=sys.stderr if frame.stream == "stderr" else sys.stdout)
                process.wait()
            except (KeyboardInterrupt, NodusError) as error:
                if not isinstance(error, KeyboardInterrupt):
                    try:
                        process.refresh()
                    except NodusError:
                        pass
                if process.is_terminal and not process.succeeded:
                    _sandbox_exec_failure(process)
                print(_safe_line(
                    f"Execution {process.id} was accepted in sandbox {sandbox.id}. "
                    f"Resume output with nodus sandbox logs {shlex.quote(_safe_line(sandbox.id))} {shlex.quote(_safe_line(process.id))} --follow. "
                    "Do not rerun exec to resume observation."
                ), file=sys.stderr)
                raise
            if not process.succeeded:
                _sandbox_exec_failure(process)
            return 0 if process.succeeded else 1
        if args.sandbox_cmd == "logs":
            sandbox = _resolve_sandbox(client, args.sandbox_id)
            process = SandboxExec(client, sandbox.id, args.exec_id)
            for frame in process.iter_output(follow=args.follow):
                print(_safe(frame.text), end="", file=sys.stderr if frame.stream == "stderr" else sys.stdout)
            return 0
        sandbox = _resolve_sandbox(client, args.sandbox_id)
        if args.sandbox_cmd == "detail":
            print(json.dumps({"id": sandbox.id, "state": sandbox.state, "url": sandbox.url,
                              "cost_usd": sandbox.cost_usd, "envelope": sandbox.envelope,
                              "charge_state": sandbox.charge_state, "final_charge_usd": sandbox.final_charge_usd,
                              "failure": sandbox.failure, "startup": sandbox.startup}, indent=2, default=str))
            return 0
        if args.sandbox_cmd == "files":
            files = sandbox.files
            if args.files_cmd == "ls":
                print(json.dumps(files.list(args.path), indent=2))
            elif args.files_cmd == "read":
                print(_safe(files.read(args.path).decode("utf-8", "replace")), end="")
            elif args.files_cmd == "download":
                files.download(args.path, args.destination)
                print(_safe_line(args.destination))
            elif args.files_cmd == "upload":
                with _sandbox_mutation(args.idempotency_key, sandbox_id=sandbox.id) as key:
                    files.upload(args.source, args.path, idempotency_key=key)
            elif args.files_cmd == "write":
                data = Path(args.from_file).read_bytes() if args.from_file else args.text
                with _sandbox_mutation(args.idempotency_key, sandbox_id=sandbox.id) as key:
                    files.write(args.path, data, expected_sha256=args.expected_sha256, idempotency_key=key)
            return 0
        if args.sandbox_cmd in ("sleep", "wake"):
            with _sandbox_mutation(args.idempotency_key, sandbox_id=sandbox.id) as key:
                getattr(sandbox, args.sandbox_cmd)(idempotency_key=key)
            print(_safe_line(sandbox.id))
            return 0
        if args.sandbox_cmd == "cost":
            sandbox.refresh()
            print(_sandbox_cost(sandbox))
            return 0
        with _sandbox_mutation(args.idempotency_key, sandbox_id=sandbox.id) as key:
            sandbox.terminate(idempotency_key=key)
        print(_safe_line(sandbox.id))
        return 0


def _workspace_line(workspace) -> str:
    if workspace.charge_state == "estimated" and workspace.state in ("stopped", "terminated", "failed"):
        cost = "Finalizing cost"
    elif workspace.charge_state == "final":
        cost = format_cost(workspace.final_charge_usd) if workspace.final_charge_usd is not None else "Not available"
    else:
        cost = format_cost(workspace.cost_usd) if workspace.cost_usd is not None else "-"
    tool = workspace.tool if workspace.ready else "-"
    return _safe_line(f"{workspace.id}  {workspace.name}  {workspace.state}  {tool}  {cost}")


def _cmd_workspace(args: argparse.Namespace) -> int:
    if args.workspace_cmd == "delete" and args.idempotency_key is not None:
        from . import _valid_idempotency_key
        _valid_idempotency_key(args.idempotency_key)
    with Client(base_url=args.base_url) as client:
        if args.workspace_cmd == "new":
            workspace = client.workspaces.create(
                args.name, gpu=args.gpu, gpu_count=args.gpu_count, gpu_memory_gb=args.gpu_memory_gb,
                environment=args.environment, size_gb=args.size_gb,
                ssh_key=Path(args.ssh_key).read_text().strip() if args.ssh_key else None,
                cpus=args.cpus, memory_gb=args.memory_gb, disk_gb=args.disk_gb)
            print(_safe_line(workspace.id))
            return 0
        if args.workspace_cmd == "ls":
            for workspace in client.workspaces.list():
                print(_workspace_line(workspace))
            return 0
        try:
            workspace = client.workspaces.get(args.workspace_id)
        except NotFoundError:
            if args.workspace_cmd != "delete" or not args.idempotency_key:
                raise
            from ._workspaces import Workspace
            workspace = Workspace(client, args.workspace_id)
        if args.workspace_cmd == "delete":
            if not args.yes:
                expected = workspace.name or workspace.id
                prompt = _safe_line(
                    f"Delete workspace {workspace.name or workspace.id!r} ({workspace.id}) and its saved files? "
                    "This cannot be undone. Past sessions and charges stay in Billing. "
                    f"Type {expected!r} to confirm: ")
                try:
                    confirmed = input(prompt) == expected
                except EOFError:
                    raise ValidationError("Deletion needs confirmation. Use --yes to confirm without a prompt.") from None
                if not confirmed:
                    print("Cancelled. Workspace was not deleted.")
                    return 0
            with _sandbox_mutation(args.idempotency_key, sandbox_id=workspace.id, noun="workspace") as key:
                workspace.delete(idempotency_key=key)
            print(_safe_line(f"{workspace.id} deleted"))
            return 0
        if args.workspace_cmd == "get":
            print(json.dumps(workspace.raw, indent=2, default=str))
            return 0
        if args.workspace_cmd == "start":
            with _sandbox_mutation(args.idempotency_key, sandbox_id=workspace.id, noun="workspace") as key:
                workspace.start(idempotency_key=key)
            if args.wait:
                workspace.wait_until_ready(poll_seconds=args.poll_seconds, timeout_seconds=args.timeout)
            print(_workspace_line(workspace))
            return 0
        if args.workspace_cmd == "stop":
            with _sandbox_mutation(args.idempotency_key, sandbox_id=workspace.id, noun="workspace") as key:
                workspace.stop(idempotency_key=key)
            if args.wait:
                workspace.wait_until_stopped(poll_seconds=args.poll_seconds, timeout_seconds=args.timeout)
            print(_safe_line(f"{workspace.id} {workspace.state}"))
            return 0
        if args.workspace_cmd == "connect":
            connection = workspace.connect(args.tool)
            print(_safe_line(connection.get("url") or json.dumps(connection)))
            return 0
        if args.workspace_cmd == "ssh":
            connection = workspace.ssh()
            print(_safe_line(connection.get("command", "")))
            print(_safe(connection.get("ssh_config", "")))
            print(_safe_line(connection.get("vscode_url", "")))
            return 0
        if args.workspace_cmd == "run":
            with _sandbox_mutation(args.idempotency_key, sandbox_id=workspace.id, noun="workspace") as key:
                workload = workspace.run(args.command, budget_usd=args.budget, gpu=args.gpu, gpu_count=args.gpu_count,
                                         gpu_memory_gb=args.gpu_memory_gb, idempotency_key=key)
            print(_safe_line(workload.id))
            return 0
        if args.workspace_cmd == "jobs":
            print(json.dumps(workspace.workloads(), indent=2, default=str))
            return 0
        if args.workspace_cmd == "upload":
            upload = workspace.upload(args.directory, idempotency_key=args.idempotency_key)
            print(_safe_line(f"{workspace.id} {upload.get('state', '')}"))
            return 0
        saved = workspace.download(args.destination, overwrite=args.overwrite)
        print(_safe_line(str(saved)))
        return 0


_AGENT_CLIENTS = {"claude-code": "Claude", "codex": "Codex", "cursor": "Cursor", "mcp": "MCP client"}


def _launched_by(item: dict) -> str:
    who = item.get("launched_by") if isinstance(item.get("launched_by"), dict) else {}
    agent = _AGENT_CLIENTS.get(who.get("client"))
    if agent:
        return agent
    if who.get("api_key_name"):
        return f"API key {who['api_key_name']}"
    return who.get("user_name") or "-"


def _compute_row(item: dict) -> list[str]:
    group = item.get("group") if isinstance(item.get("group"), dict) else None
    name = str(item.get("name") or item.get("id") or "")
    if group and group.get("size"):
        name += f" ({group['size']} runs)"
    count, gpu = item.get("gpu_count"), item.get("gpu")
    gpu_text = "-" if not gpu else (f"{count}x {gpu}" if isinstance(count, int) and count > 1 else str(gpu))
    kind = {"instance": "Instance", "training": "Training", "workspace": "Workspace"}.get(item.get("type"), str(item.get("type") or "-"))
    return [name, kind, gpu_text, str(item.get("status_text") or item.get("state") or "-"), _launched_by(item)]


def _cmd_ps(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        try:
            items = [item for state in (("running", "history") if args.all else ("running",))
                     for item in client.compute.iterate(state=state, include_workspaces=True)]
        except NotFoundError:
            print("Error: This Nodus server does not list running compute yet. "
                  "Use nodus workspace ls and nodus list instead.", file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps(items, indent=2, default=str))
        return 0
    show_table(["NAME", "TYPE", "GPU", "STATUS", "LAUNCHED BY"], [_compute_row(item) for item in items],
               empty="Nothing is running." if not args.all else "No compute yet.", plain=True)
    return 0


def _read_public_key(path: str | None) -> str | None:
    return Path(path).expanduser().read_text() if path else None


def _reference(machine) -> str:
    """The name to type after nodus ssh: the ID when the name would read as a flag."""
    return shlex.quote(machine.name if machine.name and not machine.name.startswith("-") else machine.id)


def _cmd_launch(args: argparse.Namespace) -> int:
    from ._compute import _timed_out, local_public_key
    from .errors import WorkspaceNotReadyError
    from ._compute import team_has_keys
    keyless = args.ssh_key is None and local_public_key() is None
    with Client(base_url=args.base_url) as client:
        with _sandbox_mutation(args.idempotency_key, noun="instance") as key:
            machine = client.launch(args.gpu, gpu_count=args.gpus, disk_gb=args.disk, environment=args.env,
                                    ssh_key=_read_public_key(args.ssh_key), name=args.name,
                                    keep_files=args.keep_files, wait=False, idempotency_key=key)
        if keyless and not team_has_keys(client):
            print(_safe_line(f"{machine.id} {machine.state}."))
            print(_GENERATE_HINT)
            print(_safe_line(f"Then connect with: nodus ssh {_reference(machine)}"))
            return 0
        if not args.wait:
            print(_safe_line(f"{machine.id} {machine.state}. Connect when ready with: nodus ssh {machine.id}"))
            return 0
        try:
            try:
                machine._wait_until("ssh", args.poll_seconds, args.timeout)
            except WorkspaceNotReadyError as error:
                raise _timed_out(machine, error) from None
            connection = machine.ssh()
        except (NodusError, KeyboardInterrupt):
            print(_safe_line(f"{machine.name or machine.id} ({machine.id}) was launched and may still be running. "
                             f"Check it with nodus ssh {machine.id} or release it with nodus stop {machine.id}."),
                  file=sys.stderr)
            raise
    print(_safe_line(f"{machine.name} ({machine.id}) is ready. It runs until you stop it: nodus stop {machine.id}"))
    print(_safe_line(f"Connect: nodus ssh {_reference(machine)}"))
    print(_safe_line(f"Or run: {connection.get('command', '')}"))
    return 0


_ADD_KEY_HINT = "Add your key with: nodus ssh-key add"
_REFUSED_HINT = "If the connection is refused, add your key with: nodus ssh-key add"
_GENERATE_HINT = "No SSH key found. Create and add one with: nodus ssh-key add --generate"


def _exec_ssh(argv: list[str]) -> int:
    """Hand the terminal to ssh: replace this process on POSIX, wait for it on Windows."""
    if os.name == "nt":
        return subprocess.call(argv)
    os.execvp("ssh", argv)
    return 0


def _local_key_admitted(client, machine) -> bool:
    """Whether the default local key is the machine's own key or a saved team key. Unknown reads as no."""
    try:
        return _key_admitted(client, machine)
    except Exception:
        return False


def _key_admitted(client, machine) -> bool:
    from ._compute import local_public_key
    from ._ssh_keys import listed
    local = local_public_key()
    if local is None:
        return False
    local = local.strip()
    own = machine.configuration.get("ssh_authorized_key")
    if isinstance(own, str) and listed([{"public_key": line} for line in own.splitlines()], local):
        return True
    try:
        return listed(client.ssh_keys.list(), local)
    except NodusError:
        return False


def _cmd_ssh(args: argparse.Namespace) -> int:
    from ._ssh import UnsupportedTransport, ssh_argv
    with Client(base_url=args.base_url) as client:
        machine = client.workspaces.get(args.workspace_id)
        try:
            connection = machine.ssh()
        except NodusError as error:
            if getattr(error, "code", None) == "workspace_ssh_key_required":
                print(_safe_line(f"Error: {error.message}"), file=sys.stderr)
                print(_ADD_KEY_HINT, file=sys.stderr)
                return 2
            raise
    if args.print:
        print(_safe_line(connection.get("command", "")))
        print(_safe(connection.get("ssh_config", "")))
        return 0
    try:
        argv = ssh_argv(connection)
    except UnsupportedTransport as unsupported:
        print(_safe_line(f"Error: nodus ssh cannot open a {unsupported} connection directly. "
                         "Review its details with nodus ssh --print before using them."), file=sys.stderr)
        return 1
    except ValidationError as refused:
        print(_safe_line(f"Error: {refused} Nothing was run."), file=sys.stderr)
        return 1
    if shutil.which("ssh") is None:
        print(_safe_line(shlex.join(argv)))
        print("Error: ssh was not found. Install an OpenSSH client and run the command above.", file=sys.stderr)
        return 1
    if connection.get("transport") == "tunnel" and shutil.which("cloudflared") is None:
        print(_safe_line(shlex.join(argv)))
        print("Error: this connection needs cloudflared. Install it and run the command above.", file=sys.stderr)
        return 1
    with Client(base_url=args.base_url) as client:
        admitted = _local_key_admitted(client, machine)
    if not admitted:
        print(_REFUSED_HINT, file=sys.stderr)
    return _exec_ssh(argv)


def _generate_key() -> None:
    """Create ~/.ssh/id_ed25519 with ssh-keygen, refusing to replace any existing file."""
    keygen = shutil.which("ssh-keygen")
    if keygen is None:
        raise ValueError("ssh-keygen was not found. Install an OpenSSH client, or pass the path to a public key.")
    directory = Path.home() / ".ssh"
    target = directory / "id_ed25519"
    if any(os.path.lexists(path) for path in (target, target.with_name("id_ed25519.pub"))):
        raise ValueError(f"{target} already exists without a usable public key. Recreate {target}.pub with "
                         f"ssh-keygen -y -f {target}, or pass the path to a public key.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        # With no input, an overwrite prompt from ssh-keygen is answered no.
        subprocess.run([keygen, "-t", "ed25519", "-N", "", "-f", str(target), "-C", "nodus", "-q"], check=True,
                       stdin=subprocess.DEVNULL)
    except subprocess.CalledProcessError as error:
        raise ValueError(f"ssh-keygen could not create {target} (exit {error.returncode}).") from None


def _cmd_ssh_key(args: argparse.Namespace) -> int:
    from ._compute import default_public_key, local_public_key
    with Client(base_url=args.base_url) as client:
        if args.ssh_key_cmd == "add":
            path = Path(args.path).expanduser() if args.path else None
            if path is None and local_public_key() is None:
                if not args.generate:
                    print(f"Error: {_GENERATE_HINT}", file=sys.stderr)
                    return 2
                _generate_key()
            public_key = _read_public_key(args.path) if path else default_public_key()
            name = path.name if path else next(
                name for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub")
                if (Path.home() / ".ssh" / name).is_file())
            saved = client.ssh_keys.add(public_key, name=args.name or name)
            print(_safe_line(f"Added {saved.get('fingerprint', '')}. Running instances accept it within a few seconds."))
            return 0
        if args.ssh_key_cmd == "ls":
            rows = [[key.get("fingerprint", ""), key.get("name") or "-", str(key.get("added_at") or "-")]
                    for key in client.ssh_keys.list()]
            show_table(["FINGERPRINT", "NAME", "ADDED"], rows, empty="No SSH keys saved. Add one with nodus ssh-key add.",
                       plain=True)
            return 0
        client.ssh_keys.remove(args.fingerprint)
    print(_safe_line(f"Removed {args.fingerprint}."))
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        machine = client.workspaces.get(args.workspace_id)
        with _sandbox_mutation(args.idempotency_key, sandbox_id=machine.id, noun="instance") as key:
            machine.stop(idempotency_key=key)
    print(_safe_line(f"{machine.id} {machine.state}"))
    return 0


def _sandbox_cost(sandbox):
    if sandbox.charge_state == "estimated" and sandbox.is_terminal:
        return "Finalizing cost"
    if sandbox.charge_state == "final":
        return format_cost(sandbox.final_charge_usd) if sandbox.final_charge_usd is not None else "Not available"
    return format_cost(sandbox.cost_usd)


def _sandbox_exec_failure(process):
    state = getattr(process.state, "value", process.state)
    detail = process.failure_code or f"exit code {process.exit_code}"
    print(_safe_line(f"Execution {process.id} ended {state}: {detail}."), file=sys.stderr)


def _cmd_outputs(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        if args.reload:
            result = client.reload_output(args.workload_id, args.reload, stage=args.stage)
            print(_safe_line(result.get("state", "")))
        else:
            outputs = client.outputs(args.workload_id)
            if args.json:
                print(json.dumps([o.raw for o in outputs], indent=2))
            else:
                show_table(["Stage", "Output", "Bytes", "Sink", "Rows", "Sink error"],
                           [[o.stage_id, o.name, str(o.bytes), o.sink_state or "-",
                             str(o.sink_rows) if o.sink_rows is not None else "-", o.sink_error]
                            for o in outputs], empty="No outputs available yet.", plain=args.plain)
    return 0


def _cmd_download(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        paths = client.get(args.workload_id).download()
    if not paths:
        print("No outputs available yet.")
        return 1
    for path in paths:
        print(_safe_line(path))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        filters = {"scope": args.status} if args.status in ("mine", "team") else {"status": args.status}
        workloads = client.list(limit=args.limit, **filters)
        if args.json:
            print(json.dumps([wl.raw for wl in workloads], indent=2, default=str))
        else:
            empty = "No runs yet. Start with nodus init, then nodus run."
            if args.status:
                empty = {
                    "active": "No active runs.",
                    "mine": "No runs submitted by you.",
                    "team": "No runs for this team.",
                }.get(args.status, f"No runs with status {_safe_line(args.status)}.")
            show_table(["Run", "Status", "Compute", "Cost"],
                       [[wl.id, status_label(wl.status), compute_label(wl.route), workload_cost(wl)] for wl in workloads],
                       empty=empty, plain=args.plain)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        if args.cmd == "wait":
            with _cancel_on_interrupt(client, args.workload_id):
                wl = client.get(args.workload_id)
                wl.wait(poll_seconds=args.poll, timeout_seconds=args.timeout,
                        progress=False if args.json or args.plain else None)
        else:
            wl = client.get(args.workload_id)
        if args.json:
            # ensure_ascii (the default) escapes every control character, so
            # raw wire text cannot reach the terminal through a dump.
            print(json.dumps(wl.raw, indent=2, default=str))
        else:
            show_workload(wl, plain=args.plain)
        return 0 if wl.succeeded or not wl.is_terminal else 1


def _cmd_events(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        if args.follow:
            print("Sequence  Event")
            with _cancel_on_interrupt(client, args.workload_id):
                for ev in client.stream_events(args.workload_id, poll_seconds=args.poll):
                    print(f"{ev.seq:>5}  {_safe_line(ev.type)}")
        else:
            show_table(["Sequence", "Event"],
                       [[str(ev.seq), ev.type] for ev in client.events(args.workload_id)],
                       empty="No events recorded yet.", plain=args.plain)
    return 0


def _cmd_artifacts(args: argparse.Namespace) -> int:
    # One line per manifest, then one per object it names. The endpoint returns
    # manifests, and a manifest names several objects, so flattening them into a
    # single line per row would have to pick one digest and drop the rest.
    with Client(base_url=args.base_url) as client:
        artifacts = client.artifacts(args.workload_id)
        if not artifacts:
            print("No artifacts available yet.")
        else:
            print("Stage  Attempt  Type  Manifest")
        for art in artifacts:
            mark = "final" if art.final else "checkpoint"
            print(f"{_safe_line(art.stage_id)}  gen{art.generation}/seq{art.sequence}"
                  f"  {mark}  {_safe_line(art.manifest_id)}")
            for name, out in sorted(art.outputs.items()):
                print(f"    output {_safe_line(name)}  {_safe_line(out.sha256[:12])}  {out.bytes}B")
            for f in art.files:
                print(f"    file   {_safe_line(f.uri)}  {_safe_line(f.sha256[:12])}  {f.bytes}B")
    return 0


def _cmd_cancel(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        client.cancel(args.workload_id)
    print(f"Cancellation requested for {_safe_line(args.workload_id)}. Nodus is stopping the run.")
    return 0


def _cmd_ledger(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        led = client.ledger(args.workload_id)
        if args.json:
            print(json.dumps(led.raw, indent=2, default=str))
            return 0
        print("Entry               Direction  Amount")
        for e in led.entries:
            side, amount = ("debit", e.debit_usd) if e.debit_usd else ("credit", e.credit_usd)
            print(f"  {_safe_line(e.entry_type):<18} {side:<7} ${amount:.6f}")
        st = led.settlement
        # Both numbers, always: the charge is what the customer pays, the
        # balance is what closing left — exactly $0.00 when the books are square.
        print(f"  {'charged':<18} {'total':<7} ${led.charged_usd:.6f}")
        print(f"  {'settlement':<18} {_safe_line(st.status):<7} balance ${st.balance_usd:.6f}")
    return 0


_NO_LOG_YET = (
    "No logs have been recorded for this run yet. Try again shortly."
)


def _cmd_logs(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        try:
            out = client.logs(args.workload_id, stage=args.stage, generation=args.generation)
        except NotFoundError:
            # A missing workload also returns 404 on the log route.
            client.get(args.workload_id)
            print(_NO_LOG_YET)
            return 1
    if not out:
        print(_NO_LOG_YET)
        return 1
    lines = _safe(out).splitlines()
    if args.tail and len(lines) > args.tail:
        lines = lines[-args.tail:]
    print("\n".join(lines))
    return 0


def _fmt_route(route: Any) -> list[str]:
    lines = [
        f"{'catalog SKU':<22} {_safe_line(route.sku)}",
        f"{'fit':<22} {compute_label(route)}"
        + (f"  |  {_safe_line(route.region)}" if getattr(route, 'region', '') else ""),
        f"{'node rate':<22} ${route.price_usd_hour:.4f}/h",
        f"{'remaining budget':<22} ${route.remaining_budget_usd:.2f}",
    ]
    return lines


def _cmd_explain(args: argparse.Namespace) -> int:
    """Why this route: what was chosen, and the arithmetic it was chosen on.

    Read back from the control plane that made the decision, so what is printed
    is the routing that happened rather than a re-derivation of it.
    """
    with Client(base_url=args.base_url) as client:
        wl = client.get(args.workload_id)
        if not wl.route:
            print(f"{_safe_line(wl.id)} has no route yet "
                  f"(status {_safe_line(getattr(wl.status, 'value', wl.status))})")
            return 1
        print(f"workload  {_safe_line(wl.id)}")
        print()
        for line in _fmt_route(wl.route):
            print(f"  {line}")
        print()
        print("  Legacy budget fields do not impose a spending limit.")
    return 0


def _open_browser(url: str) -> bool:
    """Best effort, and only for a web address that needed no cleaning.

    The address arrives from the console, and ``webbrowser.open`` hands
    whatever it is given to the platform's handler: a ``file:`` or
    ``javascript:`` URL would be acted on locally. An address carrying a
    control character is not opened in a cleaned-up form either -- cleaning it
    makes it a different address, which is not the one anyone approved. The
    range is :data:`_CONTROL_LINE`'s -- C0, DEL and C1, tab and newline
    included -- because a URL is a one-line value like any other.
    """
    if _CONTROL_LINE.search(url):
        return False
    if not url.lower().startswith(("http://", "https://")):
        return False
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _env_outranks(*names: str) -> list[str]:
    """Which of these are set, and so beat anything in the config file."""
    return [name for name in names if os.environ.get(name, "").strip()]


def _cmd_login(args: argparse.Namespace) -> int:
    base_url = _resolve_base_url(args.base_url)
    if not args.force:
        stored_key, stored_url = config.read_credentials()
        env_key = os.environ.get("NODUS_API_KEY", "").strip()
        same_deployment = _current_hosted_url(stored_url.strip().rstrip("/")) == base_url
        key = env_key or (stored_key if same_deployment else "")
        if key:
            try:
                with Client(api_key=key, base_url=base_url) as client:
                    identity = client._request("GET", "/v1/me")
            except AuthenticationError:
                if env_key:
                    raise AuthenticationError("NODUS_API_KEY was not accepted. Update or unset it before signing in.")
            else:
                email = identity.get("email") if isinstance(identity, dict) else None
                who = f" as {_safe_line(email)}" if email else ""
                print(f"Already signed in{who}. Welcome back!")
                print("Ready to run. Use nodus login --force to sign in again.")
                return 0
    # Before anything is minted: the console issues the key inside the call
    # that releases it, so a file that cannot be written has to fail now.
    config.ensure_writable()
    with login.open_http(base_url) as http:
        device = login.start_device_authorization(http)
        print(f"Your sign-in code is {_safe_line(device.user_code)}")
        print()
        print(f"Enter it at: {_safe_line(device.verification_url)}")
        if not args.no_browser and _open_browser(device.verification_url):
            print("Opened that page in your browser.")
        print()
        print("Waiting for you to approve it...")
        with _wait_activity("browser sign-in"):
            creds = login.poll_for_credentials(http, device, base_url)

    # The same predicate that gates sending a key gates keeping one: a stored
    # key the client cannot put in a header fails every later command, and by
    # then it has only ever been shown redacted. config.save_credentials
    # refuses such a key too; this refusal is the one that can say whose
    # fault it is.
    if not _is_header_safe(creds.api_key):
        which = (
            f"key {_safe_line(creds.key_id)}"
            if creds.key_id
            # No id to revoke by; how it was just minted is the next handle.
            else "the key -- the console lists it as the most recent for this device"
        )
        print(
            "The console sent an API key this client cannot use: a key must "
            "be printable ASCII with no spaces to travel in a request "
            f"header. Nothing was stored. Revoke {which} in the console and "
            "sign in again.",
            file=sys.stderr,
        )
        return 2

    # A caveat about the file belongs in the sentence a person is reading, not
    # in a UserWarning with a source line under it.
    with warnings.catch_warnings(record=True) as caveats:
        warnings.simplefilter("always")
        try:
            path = config.save_credentials(
                creds.api_key,
                creds.base_url,
                key_id=creds.key_id,
                tenant=creds.tenant,
                expires_at=creds.expires_at,
                email=creds.email,
            )
        except BaseException as exc:
            # The key exists on the server whether or not this write worked,
            # and the write can fail for reasons that are not the key's --
            # a full disk, permissions, a foreign entry the dump refuses.
            # Showing it once is the only way it is not lost while still
            # live; repr stays copy-pasteable and cannot act on a terminal.
            print(
                f"Could not write {config.config_path()}: {_safe_line(exc)}. "
                f"Your key, shown in quotes that are not part of it: "
                f"{creds.api_key!r} - it will not be shown again. "
                "Store it, or revoke it in the console.",
                file=sys.stderr,
            )
            if isinstance(exc, Exception):
                return 2
            raise

    # The redaction arm cannot carry a control character once the gate above
    # has passed; wrapped anyway so both arms follow the one rule.
    who = f" as {_safe_line(creds.email)}" if creds.email else ""
    print(f"Signed in{who}. Welcome to Nodus!")
    print(f"Saved your sign-in to {path}. You're ready to run.")
    for caveat in caveats:
        print(f"Note: {_safe_line(caveat.message)}", file=sys.stderr)
    for name in _env_outranks("NODUS_API_KEY", "NODUS_BASE_URL"):
        print(
            f"Note: {name} is set in this environment and outranks the file, "
            "so it is what this client will use, not what was just written.",
            file=sys.stderr,
        )
    return 0


def _cmd_logout(args: argparse.Namespace) -> int:
    path = config.config_path()
    removed = config.clear_api_key()
    if removed is None:
        print(f"No stored key to remove: {path}")
    else:
        named = f" {_safe_line(removed['key_id'])}" if removed.get("key_id") else ""
        print(f"Removed the stored key{named} from {path}")
        print("That key still works until you revoke it in the console:")
        print("deleting the local copy does not revoke it.")
    for name in _env_outranks("NODUS_API_KEY"):
        print(
            f"Note: {name} is set in this environment and outranks the file, "
            "so this client is still signed in with that key. Unset it to "
            "finish logging out.",
            file=sys.stderr,
        )
    return 0


class _CommandHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def _format_action(self, action):
        if isinstance(action, argparse._SubParsersAction):
            descriptions = {choice.dest: choice.help for choice in action._choices_actions}
            groups = (
                ("Setup", ("login", "logout", "init", "mcp")),
                ("Machines", ("launch", "ps", "ssh", "stop", "ssh-key", "workspace")),
                ("Run", ("run", "submit", "sandbox", "agent", "freeze", "freeze-status", "resume")),
                ("Monitor", ("list", "status", "workload", "wait", "logs", "cancel")),
                ("Results", ("download",)),
                ("Advanced", ("upload", "assets", "asset", "secret", "connection", "benchmark", "pools", "events", "artifacts", "ledger", "explain")),
            )
            return "\n".join(
                f"  {title}:\n" + "".join(
                    f"    {name:<10} {descriptions[name]}\n" for name in names
                ) for title, names in groups
            )
        return super()._format_action(action)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nodus", description="Run GPU workloads from a workload file.",
        formatter_class=_CommandHelpFormatter,
        epilog="""Start with nodus init, edit nodus.toml, then nodus run.
Use nodus COMMAND --help for command options.""",
    )
    p.add_argument("--version", action="version", version=f"nodus {__version__}")
    p.add_argument("--base-url", default=None, help="override NODUS_BASE_URL")
    p.add_argument("--plain", action="store_true", help="use plain output without live progress")
    p.add_argument("--debug", action="store_true", help="show technical error details")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND", title="commands")

    # SUPPRESS, not None: a subparser default is copied over the namespace the
    # top-level parser already filled, so `nodus --base-url X login` would lose
    # its address to the subcommand that also offers the flag.
    i = sub.add_parser("login", help="sign in and store an API key")
    i.add_argument("--base-url", default=argparse.SUPPRESS,
                   help="which deployment to sign in to")
    i.add_argument("--no-browser", action="store_true",
                   help="print the address instead of opening it")
    i.add_argument("--force", action="store_true", help="sign in again even when already signed in")

    sub.add_parser("logout", help="delete the stored API key")
    m = sub.add_parser("mcp", help="start the local MCP server for AI clients")
    m.add_argument("--base-url", default=argparse.SUPPRESS, help="custom API origin")

    i = sub.add_parser("init", help="create a starter workload file")
    i.add_argument("file", nargs="?", default="nodus.toml")

    for name, help_text in (
        ("run", "submit a workload file and wait for completion"),
        ("submit", "submit a workload file and return its ID"),
    ):
        r = sub.add_parser(name, help=help_text)
        r.add_argument("file", nargs="?", default="nodus.toml")
        if name == "run":
            r.add_argument("--timeout", type=_positive_seconds, default=None, help="observation timeout in seconds")
            r.add_argument("--poll", type=_positive_seconds, default=2.0)

    l = sub.add_parser("list", help="list workloads")
    l.add_argument("status", nargs="?", default=None, choices=(*STATUS_FILTERS, "mine", "team"))
    l.add_argument("--limit", type=_page_limit, default=50, help="number of runs, from 1 to 100")
    l.add_argument("--json", action="store_true")

    for name, help_text in (
        ("status", "show workload status and cost"),
        ("wait", "wait for completion. Ctrl+C cancels the workload"),
    ):
        g = sub.add_parser(name, help=help_text)
        g.add_argument("workload_id")
        g.add_argument("--json", action="store_true")
        if name == "wait":
            g.add_argument("--timeout", type=_positive_seconds, default=None)
            g.add_argument("--poll", type=_positive_seconds, default=2.0)

    workload = sub.add_parser("workload", help="inspect a workload")
    workload_sub = workload.add_subparsers(dest="workload_cmd", required=True)
    workload_get = workload_sub.add_parser("get", help="show workload status, cost and live links")
    workload_get.add_argument("workload_id")
    workload_get.add_argument("--json", action="store_true")
    workload_get.add_argument("--plain", action="store_true", default=argparse.SUPPRESS)
    workload_get.add_argument("--debug", action="store_true", default=argparse.SUPPRESS)
    workload_get.set_defaults(cmd="status")
    outputs = workload_sub.add_parser("outputs", help="list output downloads and database load state")
    outputs.add_argument("workload_id")
    outputs.add_argument("--json", action="store_true")
    outputs.add_argument("--reload", metavar="NAME", help="retry loading a named output")
    outputs.add_argument("--stage", help="select a stage when names repeat")
    outputs.add_argument("--plain", action="store_true", default=argparse.SUPPRESS)
    outputs.set_defaults(cmd="outputs")

    d = sub.add_parser("download", help="download workload outputs")
    d.add_argument("workload_id")

    u = sub.add_parser("upload", help="upload a data file or archive")
    u.add_argument("file")
    sub.add_parser("assets", help="list uploaded and imported data")
    asset = sub.add_parser("asset", help="import or inspect input assets")
    asset_sub = asset.add_subparsers(dest="asset_cmd", required=True)
    asset_get = asset_sub.add_parser("get", help="inspect an asset by ID, including its export error")
    asset_get.add_argument("asset_id")
    query = asset_sub.add_parser("import-query", help="export a read-only database query")
    query.add_argument("connection", help="connection name or ID")
    query.add_argument("sql", help="SELECT or WITH query")
    query.add_argument("--format", choices=("parquet", "csv"), default="parquet")
    query.add_argument("--branch", help="must match the connection's verified Neon branch")
    query.add_argument("--reuse", action="store_true", help="reuse an eligible export from the last 24 hours")
    secret = sub.add_parser("secret", help="store and manage write-only tenant secrets")
    secret_sub = secret.add_subparsers(dest="secret_cmd", required=True)
    secret_set = secret_sub.add_parser("set", help="store a new version from stdin or a file")
    secret_set.add_argument("name")
    secret_set.add_argument("--from-file", help="read the exact UTF-8 value from a file")
    secret_sub.add_parser("ls", help="list secret names and current versions")
    secret_rm = secret_sub.add_parser("rm", help="revoke a secret for new admissions")
    secret_rm.add_argument("name")

    connection = sub.add_parser("connection", help="manage verified external connections")
    connection_sub = connection.add_subparsers(dest="connection_cmd", required=True)
    connection_add = connection_sub.add_parser("add", help="verify and save a tenant secret reference")
    connection_add.add_argument("--name", required=True)
    connection_add.add_argument("kind", choices=("postgres", "neon", "supabase", "wandb"))
    connection_add.add_argument("--secret", required=True, help="existing tenant secret name or ID")
    connection_add.add_argument("--scope", choices=("read", "write", "readwrite"), default=None)
    connection_add.add_argument("--region")
    connection_add.add_argument("--live", action="store_true", help="admin opt-in for wandb")
    connection_add.add_argument("--branch")
    connection_add.add_argument("--entity")
    connection_add.add_argument("--project")
    connection_sub.add_parser("ls", help="list connection metadata")
    for verb in ("rm", "verify"):
        connection_sub.add_parser(verb).add_argument("connection")

    benchmark = sub.add_parser("benchmark", help="run and inspect a hardware matrix")
    benchmark_sub = benchmark.add_subparsers(dest="benchmark_cmd", required=True)
    benchmark_run = benchmark_sub.add_parser("run", help="submit a benchmark JSON request")
    benchmark_run.add_argument("file")
    benchmark_run.add_argument("--idempotency-key", required=True)
    benchmark_get = benchmark_sub.add_parser("get", help="show a benchmark report")
    benchmark_get.add_argument("benchmark_id")

    pools = sub.add_parser("pools", help="measure customer-owned GPU hosts")
    pools_sub = pools.add_subparsers(dest="pools_cmd", required=True, metavar="COMMAND")
    pools_create = pools_sub.add_parser("create", help="create a pool")
    pools_create.add_argument("name")
    pools_token = pools_sub.add_parser("token", help="print a secret single-use host enrollment token")
    pools_token.add_argument("pool_id")
    pools_token.add_argument("--mode", choices=("observe", "execute"), default="observe")
    pools_token.add_argument("--host-id", default=None, help="existing host ID required for explicit execute reenrollment")
    for command in ("route", "route-settings"):
        route = pools_sub.add_parser(command, help="set Route activation and future placement policy")
        route.add_argument("pool_id")
        if command == "route":
            route.add_argument("setting", choices=("on", "off"))
            route.add_argument("--accept-rate-version", default=None)
            route.add_argument("--accept-rate-micros", type=int, default=None, help="explicit rate per active customer device-hour in USD micros")
        route.add_argument("--wait-policy", choices=("never", "after_wait", "cheaper"), default=None)
        route.add_argument("--wait-alpha", type=float, default=None)
        route.add_argument("--waiting-budget-pct", type=float, default=None)
        route.add_argument("--burst-approval", choices=("auto", "above_threshold", "always"), default=None)
        route.add_argument("--burst-threshold-micros", type=int, default=None)
        route.add_argument("--burst-timeout-behaviour", choices=("keep_waiting", "cancel"), default=None)
    pools_hosts = pools_sub.add_parser("hosts", help="list enrolled hosts and health")
    pools_hosts.add_argument("pool_id")
    pools_predict = pools_sub.add_parser("predict", help="set Predict with explicit rate consent")
    pools_predict.add_argument("pool_id")
    pools_predict.add_argument("setting", choices=("on", "off"))
    pools_predict.add_argument("--accept-rate-version", default=None)
    pools_predict.add_argument("--accept-monthly-micros", type=int, default=None, help="explicitly accept the returned subscription monthly rate in USD micros")
    pools_forecast = pools_sub.add_parser("forecast", help="read cached forecast bands and calibration")
    pools_forecast.add_argument("pool_id")
    pools_forecast.add_argument("--horizon", type=int, choices=(7, 30), default=None)
    pools_forecast.add_argument("--json", action="store_true")
    action_kinds = ("idle_reclaim", "defragment", "drain_window", "wait_tuning")
    action_levels = ("off", "recommend", "approve", "auto")
    policies = pools_sub.add_parser("action-policies", help="read action policy and shadow readiness")
    policies.add_argument("pool_id")
    for command in ("action-policy", "start-shadow"):
        action = pools_sub.add_parser(command, help="save a policy" if command == "action-policy" else "start a future 168-hour shadow cycle")
        action.add_argument("pool_id")
        action.add_argument("kind", choices=action_kinds)
        action.add_argument("level", choices=action_levels)
        action.add_argument("--window-cron", required=True, help="weekly UTC minute hour * * weekday, without steps")
        action.add_argument("--parallelism-cap", type=int, required=True)
    kill = pools_sub.add_parser("act-kill-switch", help="stop new Act authorization while preserving cleanup")
    kill.add_argument("pool_id")
    kill.add_argument("setting", choices=("on", "off"))
    shadows = pools_sub.add_parser("shadows", help="read actual trusted shadow coverage and gaps")
    shadows.add_argument("pool_id")
    shadows.add_argument("--kind", choices=action_kinds, default=None)
    shadows.add_argument("--limit", type=int, default=None)
    shadows.add_argument("--cursor", default=None)
    shadows.add_argument("--json", action="store_true")
    actions = pools_sub.add_parser("action-proposals", help="read Act intent and observed outcomes")
    actions.add_argument("pool_id")
    actions.add_argument("--kind", choices=action_kinds, default=None)
    actions.add_argument("--limit", type=int, default=None)
    actions.add_argument("--cursor", default=None)
    actions.add_argument("--json", action="store_true")
    for command in ("approve-action", "reject-action"):
        decision = pools_sub.add_parser(command, help="decide retained Act evidence without inventing a result")
        decision.add_argument("pool_id")
        decision.add_argument("proposal_id")
    pools_proposals = pools_sub.add_parser("proposals", help="read burst approval intent and retained outcomes")
    pools_proposals.add_argument("pool_id")
    pools_proposals.add_argument("--json", action="store_true")
    pools_proposals.add_argument("--limit", type=int, default=None)
    pools_proposals.add_argument("--cursor", default=None)
    pools_proposals.add_argument("--state", choices=("pending", "approved", "rejected", "expired", "no_op", "applying", "applied"), default=None)
    for action in ("approve", "reject"):
        decision = pools_sub.add_parser(action, help=action + " the retained burst proposal amount")
        decision.add_argument("pool_id")
        decision.add_argument("proposal_id")
    pools_recommendations = pools_sub.add_parser("recommendations", help="read advisory recommendations and evidence")
    pools_recommendations.add_argument("pool_id")
    pools_recommendations.add_argument("--json", action="store_true")
    pools_recommendations.add_argument("--limit", type=int, default=None)
    pools_recommendations.add_argument("--cursor", default=None)
    pools_recommendations.add_argument("--state", choices=("open", "done", "expired"), default=None)
    pools_done = pools_sub.add_parser("mark-done", help="record a manual outcome without executing an action")
    pools_done.add_argument("pool_id")
    pools_done.add_argument("recommendation_id")
    pools_done.add_argument("--outcome", required=True)
    pools_done.add_argument("--reported-saving-micros", type=int, default=None, help="optional customer-reported saving, not the recommendation estimate")
    pools_utilization = pools_sub.add_parser("utilization", help="show measured utilization and unknown coverage")
    pools_utilization.add_argument("pool_id")
    pools_utilization.add_argument("--from", dest="from_", default=None, help="inclusive RFC 3339 start, aligned to a UTC hour")
    pools_utilization.add_argument("--to", default=None, help="exclusive RFC 3339 end, aligned to a UTC hour")
    pools_utilization.add_argument("--bucket", choices=("hour", "day"), default=None)
    pools_utilization.add_argument("--json", action="store_true", help="include host buckets and foreign device IDs")

    from ._agent_cli import add_parser as agent_parser
    agent_parser(sub, _positive_cost, _page_limit)

    launch = sub.add_parser("launch", help="rent a GPU machine and print its SSH command when ready")
    launch.add_argument("--gpu", default="H100", help="GPU model, such as H100 or \"A100-40GB\" (default H100)")
    launch.add_argument("--gpus", type=int, default=None, help="1, 2, 4 or 8 GPUs on one machine (default 1, or the count in --gpu such as H100:2)")
    launch.add_argument("--disk", type=int, default=100, help="local disk in GB, from 80 to 2048")
    launch.add_argument("--env", help="software environment, such as pytorch-cuda")
    launch.add_argument("--ssh-key", help="path to an SSH public key for this machine (default ~/.ssh/id_ed25519.pub)")
    launch.add_argument("--name", help="machine name (default instance- and 8 random characters)")
    launch.add_argument("--keep-files", action="store_true",
                        help="save project files between sessions as a workspace")
    launch.add_argument("--no-wait", dest="wait", action="store_false", help="return once compute is requested")
    launch.add_argument("--timeout", type=float, default=900.0, help="seconds to wait for SSH")
    launch.add_argument("--poll-seconds", type=float, default=5.0)
    launch.add_argument("--idempotency-key", help="reuse the key after an uncertain response")
    ps = sub.add_parser("ps", help="list running instances, workspaces and training")
    ps.add_argument("--all", action="store_true", help="include stopped and finished compute")
    ps.add_argument("--json", action="store_true", help="print the items as JSON")
    ssh = sub.add_parser("ssh", help="open an SSH session on an instance or workspace")
    ssh.add_argument("--print", action="store_true", help="print the SSH command and config entry instead")
    ssh.add_argument("workspace_id", metavar="NAME_OR_ID")
    ssh_key = sub.add_parser("ssh-key", help="save SSH public keys that every machine your team runs admits")
    ssh_key_sub = ssh_key.add_subparsers(dest="ssh_key_cmd", required=True, metavar="COMMAND")
    ssh_key_add = ssh_key_sub.add_parser("add", help="save a public key (default ~/.ssh/id_ed25519.pub)")
    ssh_key_add.add_argument("path", nargs="?", metavar="PATH", help="path to an SSH public key")
    ssh_key_add.add_argument("--name", help="label shown in nodus ssh-key ls")
    ssh_key_add.add_argument("--generate", action="store_true",
                             help="create ~/.ssh/id_ed25519 with ssh-keygen when no default key exists")
    ssh_key_sub.add_parser("ls", help="list saved public keys")
    ssh_key_rm = ssh_key_sub.add_parser("rm", help="remove a saved public key")
    ssh_key_rm.add_argument("fingerprint", metavar="FINGERPRINT")
    stop = sub.add_parser("stop", help="stop an instance or workspace and release its GPU")
    stop.add_argument("--idempotency-key", help="reuse the key after an uncertain response")
    stop.add_argument("workspace_id", metavar="NAME_OR_ID")

    workspace = sub.add_parser("workspace", help="GPU workspaces with VS Code, JupyterLab and SSH")
    workspace_sub = workspace.add_subparsers(dest="workspace_cmd", required=True, metavar="COMMAND")
    workspace_new = workspace_sub.add_parser("new", help="save a workspace configuration without renting compute")
    workspace_new.add_argument("name")
    workspace_new.add_argument("--gpu", help="GPU model, such as H100 or RTX 4090")
    workspace_new.add_argument("--gpu-count", type=int, help="1, 2, 4 or 8 GPUs on one machine")
    workspace_new.add_argument("--gpu-memory-gb", type=float, help="memory per GPU when the model is not known to the SDK")
    workspace_new.add_argument("--cpus", type=int, help="CPU-only workspace with this many vCPUs")
    workspace_new.add_argument("--memory-gb", type=float, help="system RAM for a CPU-only workspace")
    workspace_new.add_argument("--environment", help="software environment, such as pytorch-cuda")
    workspace_new.add_argument("--ssh-key", help="path to an SSH public key to admit")
    workspace_new.add_argument("--size-gb", type=float,
                               help="saved project storage in GB, default the deployment's capacity")
    workspace_new.add_argument("--disk-gb", type=int, help="runtime disk from 80 to 2048 GB")
    workspace_sub.add_parser("ls", help="list workspaces with state, ready tool and session cost")
    for action, help_text in (("get", "print the full workspace view"), ("start", "rent compute and restore saved files"),
                              ("stop", "save files and release compute"), ("connect", "print a browser URL for the editor or notebook"),
                              ("ssh", "print the SSH command, config entry and VS Code link"),
                              ("run", "run a command against the saved project as a workload"),
                              ("jobs", "list workloads submitted from the workspace"),
                              ("delete", "permanently delete a stopped workspace and its saved files"),
                              ("upload", "replace the saved project with a local folder while stopped"),
                              ("download", "save the project files to a local tar archive")):
        operation = workspace_sub.add_parser(action, help=help_text)
        if action in ("start", "stop", "run", "delete"):
            operation.add_argument("--idempotency-key", help="reuse the key after an uncertain response")
        if action == "delete":
            operation.add_argument("--yes", action="store_true", help="confirm permanent deletion without a prompt")
        if action == "upload":
            operation.add_argument("--idempotency-key", help="name this upload attempt. A changed folder or revision needs a new key")
        if action in ("start", "stop"):
            operation.add_argument("--wait", action="store_true", help="poll until the change has completed")
            operation.add_argument("--poll-seconds", type=float, default=5.0)
            operation.add_argument("--timeout", type=float, default=900.0)
        if action == "connect":
            operation.add_argument("--tool", choices=["editor", "notebook"], default="editor")
        if action == "run":
            operation.add_argument("--budget", type=_positive_cost, required=True, help="spending limit for this run in USD")
            operation.add_argument("--gpu")
            operation.add_argument("--gpu-count", type=int)
            operation.add_argument("--gpu-memory-gb", type=float)
        if action == "download":
            operation.add_argument("--overwrite", action="store_true")
        operation.add_argument("workspace_id", metavar="NAME_OR_ID")
        if action == "run":
            operation.add_argument("command")
        if action == "upload":
            operation.add_argument("directory")
        if action == "download":
            operation.add_argument("destination")
    sandbox = sub.add_parser("sandbox", help="create and use agent sandboxes")
    sandbox_sub = sandbox.add_subparsers(dest="sandbox_cmd", required=True, metavar="COMMAND")
    sandbox_new = sandbox_sub.add_parser("new", help="create or reattach to a sandbox")
    sandbox_new.add_argument("image", nargs="?", default=None)
    sandbox_new.add_argument("--name", default=None)
    add_source_arguments(sandbox_new, project_help="upload a local project to the managed environment")
    sandbox_new.add_argument("--setup", help="dependency setup command for the managed environment")
    sandbox_new.add_argument("--template", help="versioned managed environment, such as nodus:agent-tools-v1")
    sandbox_new.add_argument("--network-permission", action="append", help="named managed network permission")
    sandbox_new.add_argument("--budget", type=_positive_cost, default=None, help="deprecated compatibility field")
    sandbox_new.add_argument("--idempotency-key", help="reuse the same key when retrying an uncertain request")
    for action, help_text in (("detail", "inspect a sandbox"), ("sleep", "save project files and release compute"), ("wake", "resume authorized compute")):
        operation = sandbox_sub.add_parser(action, help=help_text)
        operation.add_argument("sandbox_id", metavar="NAME_OR_ID")
        if action != "detail":
            operation.add_argument("--idempotency-key", help="reuse the key after an uncertain response")
    files = sandbox_sub.add_parser("files", help="read, write and transfer project files")
    file_sub = files.add_subparsers(dest="files_cmd", required=True)
    for action in ("ls", "read", "write", "upload", "download"):
        operation = file_sub.add_parser(action)
        operation.add_argument("sandbox_id", metavar="NAME_OR_ID")
        if action == "upload":
            operation.add_argument("source")
        operation.add_argument("path", nargs="?" if action == "ls" else None, default="." if action == "ls" else None)
        if action == "download":
            operation.add_argument("destination")
        if action in ("write", "upload"):
            operation.add_argument("--idempotency-key")
        if action == "write":
            content = operation.add_mutually_exclusive_group(required=True)
            content.add_argument("--text")
            content.add_argument("--from-file")
            operation.add_argument("--expected-sha256", help="required to replace an existing file")
    sandbox_list = sandbox_sub.add_parser("ls", help="list sandboxes")
    sandbox_list.add_argument("--limit", type=_page_limit, default=50)
    sandbox_list.add_argument("--json", action="store_true")
    sandbox_exec = sandbox_sub.add_parser("exec", help="run a command in a sandbox")
    sandbox_exec.add_argument("sandbox_id", metavar="NAME_OR_ID")
    sandbox_exec.add_argument("--cwd", default=None)
    sandbox_exec.add_argument("--idempotency-key", help="reuse the same key when retrying an uncertain request")
    sandbox_exec.add_argument("command", nargs=argparse.REMAINDER)
    sandbox_logs = sandbox_sub.add_parser("logs", help="read command output")
    sandbox_logs.add_argument("sandbox_id", metavar="NAME_OR_ID")
    sandbox_logs.add_argument("exec_id")
    sandbox_logs.add_argument("--follow", action="store_true")
    sandbox_cost = sandbox_sub.add_parser("cost", help="show sandbox cost")
    sandbox_cost.add_argument("sandbox_id", metavar="NAME_OR_ID")
    sandbox_remove = sandbox_sub.add_parser("rm", help="terminate a sandbox")
    sandbox_remove.add_argument("sandbox_id", metavar="NAME_OR_ID")
    sandbox_remove.add_argument("--idempotency-key", help="reuse the same key when retrying an uncertain request")

    e = sub.add_parser("events", help="lifecycle events")
    e.add_argument("workload_id")
    e.add_argument("--follow", action="store_true", help="follow events. Ctrl+C cancels the workload")
    e.add_argument("--poll", type=_positive_seconds, default=2.0)

    a = sub.add_parser("artifacts", help="verified manifests")
    a.add_argument("workload_id")

    for command, help_text in (("freeze", "request a saved-file freeze"), ("freeze-status", "read retained state and cleanup progress"), ("resume", "resume the command with saved checkpoint files")):
        freeze = sub.add_parser(command, help=help_text)
        freeze.add_argument("workload_id")

    c = sub.add_parser("cancel", help="request a safe stop")
    c.add_argument("workload_id")

    d = sub.add_parser("ledger", help="what the run settled")
    d.add_argument("workload_id")
    d.add_argument("--json", action="store_true")

    lg = sub.add_parser("logs", help="what the program printed")
    lg.add_argument("workload_id")
    lg.add_argument("--stage", default=None)
    lg.add_argument("--generation", type=_positive_integer, default=None, help="attempt number, starting at 1")
    lg.add_argument("--tail", type=_nonnegative_integer, default=0, help="last N lines only, or 0 for all lines")

    x = sub.add_parser("explain", help="why this route")
    x.add_argument("workload_id")
    for parser in sub.choices.values():
        parser.add_argument("--plain", action="store_true", default=argparse.SUPPRESS,
                            help="use plain output without live progress")
        parser.add_argument("--debug", action="store_true", default=argparse.SUPPRESS,
                            help="show technical error details")
    return p


def _cmd_mcp(args: argparse.Namespace) -> int:
    try:
        from ._mcp import create_server
    except ModuleNotFoundError as exc:
        if exc.name != "mcp":
            raise
        raise ValueError('Install MCP support with: pip install "nodus-compute[mcp]"') from None
    create_server(base_url=args.base_url).run(transport="stdio")
    return 0


def mcp_main() -> int:
    """Start the MCP server through the nodus-mcp executable."""
    return main(["mcp", *sys.argv[1:]])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)

    handlers = {
        "login": lambda: _cmd_login(args),
        "mcp": lambda: _cmd_mcp(args),
        "logout": lambda: _cmd_logout(args),
        "init": lambda: _cmd_init(args),
        "run": lambda: _cmd_run(args),
        "submit": lambda: _cmd_run(args),
        "download": lambda: _cmd_download(args),
        "outputs": lambda: _cmd_outputs(args),
        "upload": lambda: _cmd_upload(args),
        "assets": lambda: _cmd_assets(args),
        "asset": lambda: _cmd_asset(args),
        "secret": lambda: _cmd_secret(args),
        "connection": lambda: _cmd_connection(args),
        "pools": lambda: _cmd_pools(args),
        "sandbox": lambda: _cmd_sandbox(args),
        "workspace": lambda: _cmd_workspace(args),
        "launch": lambda: _cmd_launch(args),
        "ps": lambda: _cmd_ps(args),
        "ssh": lambda: _cmd_ssh(args),
        "ssh-key": lambda: _cmd_ssh_key(args),
        "stop": lambda: _cmd_stop(args),
        "agent": lambda: _cmd_agent(args),
        "benchmark": lambda: _cmd_benchmark(args),
        "list": lambda: _cmd_list(args),
        "status": lambda: _cmd_status(args),
        "wait": lambda: _cmd_status(args),
        "events": lambda: _cmd_events(args),
        "artifacts": lambda: _cmd_artifacts(args),
        "cancel": lambda: _cmd_cancel(args),
        "freeze": lambda: _cmd_freeze(args),
        "freeze-status": lambda: _cmd_freeze(args),
        "resume": lambda: _cmd_freeze(args),
        "ledger": lambda: _cmd_ledger(args),
        "logs": lambda: _cmd_logs(args),
        "explain": lambda: _cmd_explain(args),
    }
    try:
        with acting_as("cli"):
            return handlers[args.cmd]()
    except (NodusError, ValueError, TypeError, OSError) as exc:
        message = _safe(exc)
        if isinstance(exc, APIConnectionError) and not args.debug:
            message = "Could not connect to Nodus. Check your connection and try again. Your saved sign-in is unchanged."
        elif isinstance(exc, NodusError) and not args.debug and exc.status_code and exc.status_code >= 500:
            message = "Nodus is temporarily unable to complete this request. Please try again shortly."
        elif isinstance(exc, NodusError) and not args.debug and exc.status_code == 404:
            if args.cmd == "login":
                message = "This server does not support sign-in verification. Contact Nodus support. Your saved sign-in is unchanged."
            elif hasattr(args, "workload_id"):
                message = "That run was not found. Check the ID with nodus list and try again."
            elif exc.code == "not_found":
                if args.cmd == "sandbox":
                    message = f"That sandbox or execution was not found. Check nodus {args.cmd} ls and use the exact ID."
                else:
                    message = "That resource was not found. Check its ID and that you are signed in to the correct account."
            else:
                message = "This endpoint is unavailable on the Nodus server. Contact Nodus support."
        elif isinstance(exc, NodusError) and not args.debug and exc.status_code in (401, 403):
            message = "Your sign-in was not accepted. Run nodus login --force to sign in again."
        elif isinstance(exc, NodusError) and not args.debug and exc.status_code == 402:
            if exc.code == "payment_method_required":
                message = "Add a payment method at https://console.nodus-compute.ai/?view=billing before running workloads, including runs using starter credits."
            else:
                message = "This run cannot start within your current spending limit. Review your account limit and available credits in the console."
        if args.cmd in ("secret", "connection") and isinstance(exc, (APIConnectionError, APITimeoutError)):
            message = ("Request to Nodus timed out." if isinstance(exc, APITimeoutError) else "Could not connect to Nodus.")
            message += " Check your connection. Your saved sign-in is unchanged."
            if args.cmd == "connection" and args.connection_cmd == "add":
                message += " Look up the connection by name before retrying."
        elif args.cmd in ("secret", "connection") and isinstance(exc, NodusError) and (exc.status_code is not None or not isinstance(exc, ValidationError)):
            message = ("Secret" if args.cmd == "secret" else "Connection") + " operation failed. Check your credentials, reference, and connection."
        if args.cmd == "asset" and (recovery := _query_recovery(exc)):
            message = recovery
        print(f"Error: {message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt as exc:
        if args.cmd == "asset" and (recovery := _query_recovery(exc)):
            print(recovery, file=sys.stderr)
        return 130




def _cmd_benchmark(args: argparse.Namespace) -> int:
    with Client(base_url=args.base_url) as client:
        if args.benchmark_cmd == "get":
            result = client.get_benchmark(args.benchmark_id)
        else:
            source = Path(args.file)
            if source.stat().st_size > 1_000_000:
                raise ValueError("benchmark request exceeds 1 MB")
            request = json.loads(source.read_text())
            if not isinstance(request, dict) or not {"workload", "matrix"}.issubset(request) or set(request) - {"workload", "matrix", "budget_usd"}:
                raise ValueError("benchmark request requires workload and matrix")
            matrix = request["matrix"]
            if not isinstance(matrix, dict) or set(matrix) != {"gpu_families", "batch_sizes", "regions", "repetitions"}:
                raise ValueError("matrix requires gpu_families, batch_sizes, regions and repetitions")
            result = client.benchmark(workload=request["workload"], budget=request.get("budget_usd"),
                                      idempotency_key=args.idempotency_key, **matrix)
        print(json.dumps(result, indent=2, allow_nan=False))
    return 0

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

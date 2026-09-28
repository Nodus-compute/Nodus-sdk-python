"""Shared safe terminal presentation for the SDK and command line."""

from __future__ import annotations

import math
import re
import sys
import time
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]|\x9b[0-?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(value: Any, *, line: bool = False) -> str:
    text = _CONTROL.sub("", _ANSI.sub("", str(value)))
    return text.replace("\n", " ").replace("\t", " ") if line else text


def console(*, stderr: bool = False, plain: bool = False) -> Console:
    return Console(file=sys.stderr if stderr else sys.stdout, markup=False, highlight=False,
                   force_terminal=False if plain else None, no_color=True if plain else None)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def stage_progress(stage: Any) -> tuple[str, float | None]:
    """Use matching observed units, never time or an unrelated denominator."""
    choices = ((stage.metric_step, stage.metric_total_steps, "Step"),
               (stage.metric_epoch, stage.metric_total_epochs, "Epoch"))
    for completed, total, label in choices:
        if _number(completed) and _number(total) and total > 0 and completed <= total:
            return f"{label} {completed:g}/{total:g}", completed / total
    for completed, _, label in choices:
        if _number(completed):
            return f"{label} {completed:g}", None
    if stage.total_units > 0 and 0 <= stage.completed_units <= stage.total_units:
        return f"Progress {stage.completed_units}/{stage.total_units}", stage.completed_units / stage.total_units
    if status_label(stage.status) in ("Completed", "Failed", "Cancelled"):
        return status_label(stage.status), None
    return "Progress not reported yet", None


def status_label(value: Any) -> str:
    return clean(getattr(value, "value", value), line=True).replace("_", " ").capitalize()


def format_cost(value: float) -> str:
    if 0 < value < 0.000001:
        return "<$0.000001"
    if 0 < value < 0.01:
        return f"${value:.6f}".rstrip("0")
    return f"${value:.2f}"


def workload_cost(workload: Any) -> str:
    meter = getattr(workload, "meter", None)
    status = getattr(workload.status, "value", workload.status)
    if status in ("completed", "failed", "cancelled") and getattr(meter, "charge_state", "") == "estimated":
        return "Finalizing cost"
    return format_cost(workload.cost_now_usd)


def compute_label(route: Any) -> str:
    if route is None:
        return "Not reported"
    resources = route.resources if isinstance(route.resources, dict) else {}
    label = ""
    accelerator = False
    for value in (resources.get("accelerator"), route.fit_class):
        if not isinstance(value, str):
            continue
        candidate = clean(value, line=True).strip()
        if candidate and candidate.lower() not in ("compute", "gpu", "accelerator", "unknown", "none", "not reported"):
            label = candidate
            accelerator = value == resources.get("accelerator")
            break
    if not label:
        return "Not reported"
    count = resources.get("gpu_count")
    multiple = isinstance(count, int) and not isinstance(count, bool) and count in (2, 4, 8)
    if multiple:
        label = f"{count} × {label}"
    memory = resources.get("device_memory_gb")
    if not _number(memory) or memory <= 0:
        memory = route.memory_gb if accelerator or route.compute_class == "accelerator" else 0
    if _number(memory) and memory > 0:
        label += f" ({memory:g} GB{' per GPU' if multiple else ''})"
    return label


def workload_rows(workload: Any) -> list[tuple[str, str]]:
    rows = [("Run", clean(workload.id, line=True)), ("Status", status_label(workload.status)),
            ("Cost", workload_cost(workload))]
    rows.append(("Compute", compute_label(workload.route)))
    if workload.route is not None and _number(workload.route.price_usd_hour) and workload.route.price_usd_hour > 0:
        rows.append(("Node hourly price", f"${workload.route.price_usd_hour:.4f}/hour"))
    for stage in workload.stages:
        label, fraction = stage_progress(stage)
        if fraction is not None:
            label += f"  {fraction:.0%}"
        if stage.last_loss is not None:
            label += f"  loss {stage.last_loss:g}"
        if stage.metric_rate is not None:
            label += f"  {stage.metric_rate:g} steps/s"
        rows.append((clean(stage.id, line=True) or "Progress", label))
    rows.extend(("wandb", link.url) for link in getattr(workload, "links", []))
    reason = workload.raw.get("failure_reason") or workload.raw.get("error")
    if isinstance(reason, str) and reason:
        rows.append(("Details", clean(reason, line=True)))
    return rows


def show_workload(workload: Any, *, plain: bool = False) -> None:
    out = console(plain=plain)
    if plain or not out.is_terminal:
        for name, value in workload_rows(workload):
            out.print(Text(f"{name}: {value}"), soft_wrap=True)
        return
    table = Table.grid(padding=(0, 2))
    for name, value in workload_rows(workload):
        table.add_row(Text(name, style="dim"), Text(value))
    out.print(Panel(table, title="Nodus run", border_style="cyan"))


def show_table(headers: list[str], rows: list[list[str]], *, empty: str, plain: bool = False) -> None:
    out = console(plain=plain)
    if not rows:
        out.print(Text(empty))
    elif plain or not out.is_terminal:
        out.print(Text("  ".join(headers)), soft_wrap=True)
        for row in rows:
            out.print(Text("  ".join(clean(cell, line=True) for cell in row)), soft_wrap=True)
    else:
        table = Table(*headers, border_style="dim", header_style="bold cyan", box=None, padding=(0, 2))
        for row in rows:
            table.add_row(*(Text(clean(cell, line=True)) for cell in row))
        out.print(table)


class RunProgress:
    """Render observation without redirecting the customer's output streams."""

    def __init__(self, workload_id: str, progress: bool | None):
        if progress is not None and not isinstance(progress, bool):
            raise ValueError("progress must be True, False, or None")
        self.console = console(stderr=True)
        self.terminal = self.console.is_terminal and not self.console.is_dumb_terminal
        self.enabled = self.terminal if progress is None else progress
        self.workload_id = workload_id
        self.started = time.monotonic()
        self.workload = None
        self.event_after = 0
        self.log_after = ""
        self.live_available = True
        self.live_empty = False
        self.last_chunk_id = -1
        self.saved_attempt = None
        self.saved_text = ""
        self.truncated = False
        self.live = None
        self.last_summary = ""
        self.notice = ""
        try:
            "⠋".encode(self.console.encoding)
            spinner = "dots"
        except (UnicodeEncodeError, LookupError):
            spinner = "line"
        self.spinner = Spinner(spinner)

    def __enter__(self):
        if self.enabled and self.terminal:
            self.live = Live(console=self.console, get_renderable=self.render,
                             refresh_per_second=1, redirect_stdout=False, redirect_stderr=False)
            self.live.start()
        return self

    def __exit__(self, *exc):
        if self.live is not None:
            self.live.stop()

    def render(self):
        elapsed = int(time.monotonic() - self.started)
        rows = workload_rows(self.workload) if self.workload else [("Run", clean(self.workload_id, line=True)), ("Status", "Connecting")]
        table = Table.grid(padding=(0, 2))
        for label, value in rows:
            if label == "Status" and (self.workload is None or not self.workload.is_terminal):
                self.spinner.update(text=Text(value))
                table.add_row(Text(label, style="dim"), self.spinner)
            else:
                table.add_row(Text(label, style="dim"), Text(value))
        for stage in self.workload.stages if self.workload else []:
            _, fraction = stage_progress(stage)
            if fraction is not None:
                table.add_row(Text(clean(stage.id, line=True), style="dim"),
                              ProgressBar(total=100, completed=fraction * 100, width=28))
        table.add_row(Text("Elapsed", style="dim"), Text(f"{elapsed}s"))
        if self.notice:
            table.add_row(Text("Updates", style="dim"), Text(self.notice))
        return Panel(table, title="Nodus", border_style="cyan")

    def update(self, workload):
        self.workload = workload
        if not self.enabled:
            return
        if self.live is not None:
            self.live.refresh()
        else:
            summary = " | ".join(f"{key}: {value}" for key, value in workload_rows(workload))
            if summary != self.last_summary:
                self.console.print(Text(summary + f" | {int(time.monotonic() - self.started)}s elapsed"), soft_wrap=True)
                self.last_summary = summary

    def requests(self):
        if not self.enabled:
            return []
        base = f"/v1/workloads/{self.workload_id}"
        requests = [("events", base + "/events", {"after": self.event_after})]
        if self.live_available:
            requests.append(("logs", base + "/logs/live", {"after": self.log_after}))
            if self.workload is not None and self.workload.is_terminal:
                requests.append(("saved_logs", base + "/logs", {}))
        else:
            requests.append(("saved_logs", base + "/logs", {}))
        return requests

    def accept(self, kind: str, body: Any):
        if not isinstance(body, dict):
            return
        previous = self.event_after if kind == "events" else self.log_after
        if self.live_available:
            self.notice = ""
        if kind == "events":
            events = body.get("events")
            for event in events if isinstance(events, list) else []:
                if not isinstance(event, dict):
                    continue
                seq = event.get("id")
                if isinstance(seq, int) and seq > self.event_after:
                    self.event_after = seq
                    label = event.get("event_type", "Update")
                    self.console.print(Text("Nodus: " + status_label(label).replace("workload.", "").replace("Workload.", "")))
        else:
            chunks = body.get("chunks")
            self.live_empty = isinstance(chunks, list) and not chunks and self.last_chunk_id < 0
            for chunk in chunks if isinstance(chunks, list) else []:
                if not isinstance(chunk, dict):
                    continue
                ident = chunk.get("id")
                if not isinstance(ident, int) or isinstance(ident, bool) or ident <= self.last_chunk_id:
                    continue
                self.last_chunk_id = ident
                self.console.print(Text(clean(chunk.get("text", ""))), end="", soft_wrap=True)
            cursor = body.get("next_cursor")
            if isinstance(cursor, (str, int)):
                self.log_after = str(cursor)
            if body.get("truncated") and not self.truncated:
                self.console.print(Text("\nOutput was truncated. Use nodus logs to retrieve saved output."))
                self.truncated = True
        rows = body.get("events" if kind == "events" else "chunks")
        current = self.event_after if kind == "events" else self.log_after
        return isinstance(rows, list) and bool(rows) and current != previous

    def accept_saved_logs(self, text: str, stage: str, generation: str):
        attempt = (stage, generation)
        if attempt != self.saved_attempt:
            self.saved_attempt = attempt
            self.saved_text = ""
        if text.startswith(self.saved_text):
            self.console.print(Text(clean(text[len(self.saved_text):])), end="", soft_wrap=True)
            self.saved_text = text

    def unavailable(self, kind: str, status: int | None):
        if kind == "logs":
            self.live_empty = False
        if kind == "saved_logs" and status == 404:
            return
        if kind == "logs" and status in (404, 405, 501):
            self.live_available = False
            self.notice = "Live output unavailable on this deployment. Use nodus logs for saved output."
        else:
            self.notice = "Updates temporarily unavailable. Retrying."

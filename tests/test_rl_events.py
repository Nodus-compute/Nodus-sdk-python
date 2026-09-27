"""Reporting a scored attempt is part of the SDK, not a file to copy.

A workload reports RL progress by writing one `nodus.rl_event` line per
attempt to standard output. These tests hold the SDK to the wire contract the
control plane parses, because a caller who gets it slightly wrong learns so
only after renting a machine.
"""

from __future__ import annotations

import io
import json

import pytest

import nodus


PREFIX = "nodus.rl_event "


def emitted(stdout: io.StringIO) -> list[dict]:
    rows = []
    for line in stdout.getvalue().splitlines():
        assert line.startswith(PREFIX), line
        rows.append(json.loads(line[len(PREFIX) :]))
    return rows


def test_emitter_is_exported_from_the_package() -> None:
    assert hasattr(nodus, "RLEventEmitter")
    assert "RLEventEmitter" in nodus.__all__


def test_completed_attempt_carries_the_parsed_wire_fields() -> None:
    out = io.StringIO()
    events = nodus.RLEventEmitter("wl_fixture", stdout=out)
    events.task_started("evaluation", "task-1")
    events.task_completed("evaluation", "task-1", outcome="passed", reward=1.0, duration_ms=12.5)

    started, completed = emitted(out)
    assert started["phase"] == "evaluation"
    assert started["task_id"] == "task-1"
    assert started["kind"] == "task_started"
    assert "outcome" not in started
    assert completed["kind"] == "task_completed"
    assert completed["outcome"] == "passed"
    assert completed["reward"] == 1.0
    assert completed["duration_ms"] == 12.5
    # The identity is what lets the control plane deduplicate a replayed line.
    assert started["event_id"] != completed["event_id"]
    assert completed["event_id"].startswith("nre_")


def test_the_same_attempt_reported_twice_keeps_one_identity() -> None:
    first, second = io.StringIO(), io.StringIO()
    nodus.RLEventEmitter("wl_fixture", stdout=first).task_completed(
        "evaluation", "task-1", outcome="failed", reward=0.0
    )
    nodus.RLEventEmitter("wl_fixture", stdout=second).task_completed(
        "evaluation", "task-1", outcome="failed", reward=0.0
    )
    assert emitted(first)[0]["event_id"] == emitted(second)[0]["event_id"]


def test_a_different_trial_does_not_reuse_another_trials_identity() -> None:
    first, second = io.StringIO(), io.StringIO()
    nodus.RLEventEmitter("wl_one", stdout=first).task_completed("evaluation", "t", outcome="passed")
    nodus.RLEventEmitter("wl_two", stdout=second).task_completed("evaluation", "t", outcome="passed")
    assert emitted(first)[0]["event_id"] != emitted(second)[0]["event_id"]


@pytest.mark.parametrize(
    "phase,outcome",
    [("baseline", "passed"), ("training", "failed"), ("evaluation", "error")],
)
def test_every_accepted_phase_and_outcome_is_accepted(phase: str, outcome: str) -> None:
    out = io.StringIO()
    nodus.RLEventEmitter("wl_fixture", stdout=out).task_completed(phase, "t", outcome=outcome)
    assert emitted(out)[0]["phase"] == phase


def test_a_phase_the_control_plane_rejects_is_refused_before_output() -> None:
    out = io.StringIO()
    events = nodus.RLEventEmitter("wl_fixture", stdout=out)
    with pytest.raises(nodus.EventValidationError):
        events.task_completed("warmup", "t", outcome="passed")
    assert out.getvalue() == ""


def test_an_outcome_the_control_plane_rejects_is_refused_before_output() -> None:
    out = io.StringIO()
    events = nodus.RLEventEmitter("wl_fixture", stdout=out)
    with pytest.raises(nodus.EventValidationError):
        events.task_completed("evaluation", "t", outcome="skipped")
    assert out.getvalue() == ""


def test_a_reward_that_is_not_a_finite_number_is_refused() -> None:
    out = io.StringIO()
    events = nodus.RLEventEmitter("wl_fixture", stdout=out)
    with pytest.raises(nodus.EventValidationError):
        events.task_completed("evaluation", "t", outcome="passed", reward=float("nan"))
    assert out.getvalue() == ""


def test_one_line_per_event_survives_a_multi_line_message() -> None:
    out = io.StringIO()
    nodus.RLEventEmitter("wl_fixture", stdout=out).task_completed(
        "evaluation", "t", outcome="failed", message="first\nsecond"
    )
    assert len(out.getvalue().splitlines()) == 1
    assert emitted(out)[0]["message"] == "first\nsecond"


def test_trace_text_is_sent_only_when_the_caller_asks_for_it() -> None:
    quiet, loud = io.StringIO(), io.StringIO()
    nodus.RLEventEmitter("wl_fixture", stdout=quiet).task_completed(
        "evaluation", "t", outcome="passed"
    )
    nodus.RLEventEmitter("wl_fixture", stdout=loud).task_completed(
        "evaluation",
        "t",
        outcome="passed",
        raw_trace=nodus.RawTraceFields(input="2 + 2", output="4"),
    )
    assert "input" not in emitted(quiet)[0]
    assert emitted(loud)[0]["input"] == "2 + 2"

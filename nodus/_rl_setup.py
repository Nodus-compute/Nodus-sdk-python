"""Ask for a run to be an RL run, by name rather than through ``extra``.

The control plane validates this metadata and answers 400 when it is wrong.
That answer arrives after a submission, so the same rules are applied here
first, where the caller can still fix them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

# Mirrors rlEnvironmentID and ValidateRLMetadata in the control plane.
_ENVIRONMENT_ID = re.compile(r"^[a-z0-9-]+$")
_MODES = ("train", "evaluate")
_MAX_PLANNED_TASKS = 10000
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RLSetup:
    """What makes a workload an RL run.

    ``environment_id`` is a catalog entry, or ``"custom"`` for an environment
    the caller brings. ``planned_tasks`` is how many held-out tasks the run
    intends to score, which is what the console compares progress against.
    """

    environment_id: str
    mode: str
    model: str
    planned_tasks: int

    def to_payload(self) -> dict[str, Any]:
        """The ``rl`` field of a workload body, validated."""
        if not isinstance(self.environment_id, str) or not _ENVIRONMENT_ID.match(self.environment_id):
            raise ValueError(
                "rl.environment_id must be lowercase letters, digits or hyphens, "
                f'such as "custom", not {self.environment_id!r}'
            )
        if self.mode not in _MODES:
            raise ValueError(f"rl.mode must be one of {_MODES}, not {self.mode!r}")
        if not isinstance(self.model, str) or not self.model or len(self.model.encode("utf-8")) > 256:
            raise ValueError("rl.model must be a name of 1 to 256 bytes")
        if isinstance(self.planned_tasks, bool) or not isinstance(self.planned_tasks, int):
            raise ValueError("rl.planned_tasks must be a whole number")
        if not 1 <= self.planned_tasks <= _MAX_PLANNED_TASKS:
            raise ValueError(
                f"rl.planned_tasks must be between 1 and {_MAX_PLANNED_TASKS}, not {self.planned_tasks}"
            )
        return {
            "schema_version": _SCHEMA_VERSION,
            "environment_id": self.environment_id,
            "mode": self.mode,
            "model": self.model,
            "planned_tasks": self.planned_tasks,
        }


def rl_payload(value: "RLSetup | Mapping[str, Any] | None") -> dict[str, Any] | None:
    """Accept the dataclass or the mapping a caller already had in ``extra``."""
    if value is None:
        return None
    if isinstance(value, RLSetup):
        return value.to_payload()
    if isinstance(value, Mapping):
        return RLSetup(
            environment_id=value.get("environment_id", ""),
            mode=value.get("mode", ""),
            model=value.get("model", ""),
            planned_tasks=value.get("planned_tasks", 0),
        ).to_payload()
    raise TypeError("rl= must be an RLSetup or a mapping of its fields")

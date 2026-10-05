"""Scene data model.

A scene is a deterministic rule: when a trigger fires and every condition
holds, run the actions. AI agents may *author* scenes, but execution is a
plain rule evaluation - the same event always produces the same actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from omnibutler.core.models import RiskLevel

TRIGGER_TYPES = {"state_change", "schedule", "geofence",
                 "session_opened", "session_closed"}
COMPARISON_OPS = {"==", "!=", ">", "<", ">=", "<=", "in", "truthy", "falsy"}
CONDITION_TYPES = {"state", "time_window"}
#: Friendly spellings accepted in scene files for state-condition operators.
OP_ALIASES = {
    "equals": "==", "eq": "==", "not_equals": "!=", "ne": "!=",
    "greater_than": ">", "gt": ">", "less_than": "<", "lt": "<",
    "at_least": ">=", "gte": ">=", "at_most": "<=", "lte": "<=",
}


@dataclass
class SceneTrigger:
    type: str
    # state_change: device / property; schedule: at "HH:MM" or every_minutes;
    # geofence: zone + transition ("enter" / "exit")
    device: str | None = None
    property: str | None = None
    at: str | None = None
    every_minutes: int | None = None
    zone: str | None = None
    transition: str | None = None


@dataclass
class SceneCondition:
    """One AND-ed scene condition.

    ``type == "state"`` (the default, and the only kind before v0.3):
    compare ``device``'s ``property`` against ``value`` with ``op``.

    ``type == "time_window"``: holds while the local time of day is inside
    the ``start``-``end`` window ("HH:MM", start inclusive, end exclusive).
    Windows may cross midnight (start > end, e.g. 22:00-06:00); start == end
    means the whole day.
    """

    device: str | None = None
    property: str | None = None
    op: str = "=="
    value: Any = None
    type: str = "state"
    start: str | None = None  # time_window only, "HH:MM"
    end: str | None = None    # time_window only, "HH:MM"


@dataclass
class SceneAction:
    device: str
    kind: str = "set"  # "set" (property) | "action" (named action)
    property: str | None = None
    value: Any = None
    action: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    risk: RiskLevel | None = None  # per-action override


@dataclass
class Scene:
    name: str
    trigger: SceneTrigger
    actions: list[SceneAction]
    conditions: list[SceneCondition] = field(default_factory=list)
    risk: RiskLevel = RiskLevel.LOW
    enabled: bool = True
    description: str = ""
    source: str | None = None  # file the scene was loaded from

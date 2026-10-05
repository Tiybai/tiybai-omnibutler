"""Scene data model.

A scene is a deterministic rule: when a trigger fires and every condition
holds, run the actions. AI agents may *author* scenes, but execution is a
plain rule evaluation - the same event always produces the same actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from omnibutler.core.models import RiskLevel

TRIGGER_TYPES = {"state_change", "schedule", "geofence"}
COMPARISON_OPS = {"==", "!=", ">", "<", ">=", "<=", "in", "truthy", "falsy"}


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
    device: str
    property: str
    op: str = "=="
    value: Any = None


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

"""The deterministic scene execution engine.

The engine subscribes to (or is handed) events, finds scenes whose trigger
matches, evaluates all conditions (AND semantics), then runs the actions
through the DeviceManager. Safety guardrail: any action whose effective risk
is *high* - from the scene, the action, or the target device - is never
executed; it is parked in the confirmation queue for a human instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from omnibutler.core.confirmations import ConfirmationQueue
from omnibutler.core.events import Event
from omnibutler.core.manager import DeviceManager
from omnibutler.core.models import RiskLevel
from omnibutler.scenes.model import Scene, SceneCondition, SceneTrigger


@dataclass
class ActionOutcome:
    scene: str
    device: str
    description: str
    status: str  # executed | queued | failed
    detail: Any = None


@dataclass
class ExecutionReport:
    event_type: str
    evaluated: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    outcomes: list[ActionOutcome] = field(default_factory=list)

    @property
    def executed(self) -> list[ActionOutcome]:
        return [o for o in self.outcomes if o.status == "executed"]

    @property
    def queued(self) -> list[ActionOutcome]:
        return [o for o in self.outcomes if o.status == "queued"]


def _compare(actual: Any, op: str, expected: Any) -> bool:
    if op == "truthy":
        return bool(actual)
    if op == "falsy":
        return not bool(actual)
    if op == "==":
        return actual == expected
    if op == "!=":
        return actual != expected
    if op == "in":
        return isinstance(expected, (list, tuple, set)) and actual in expected
    if actual is None:
        return False
    try:
        if op == ">":
            return actual > expected
        if op == "<":
            return actual < expected
        if op == ">=":
            return actual >= expected
        if op == "<=":
            return actual <= expected
    except TypeError:
        return False
    return False


class SceneEngine:
    def __init__(
        self,
        manager: DeviceManager,
        confirmations: ConfirmationQueue | None = None,
    ) -> None:
        self.manager = manager
        self.confirmations = confirmations if confirmations is not None else ConfirmationQueue()
        self.scenes: dict[str, Scene] = {}

    def add_scene(self, scene: Scene) -> Scene:
        self.scenes[scene.name] = scene
        return scene

    def add_scenes(self, scenes: list[Scene]) -> None:
        for scene in scenes:
            self.add_scene(scene)

    def list_scenes(self) -> list[Scene]:
        return list(self.scenes.values())

    def set_enabled(self, name: str, enabled: bool) -> Scene:
        scene = self.scenes[name]
        scene.enabled = enabled
        return scene

    def attach(self, bus) -> None:
        for event_type in ("state_change", "schedule", "geofence"):
            bus.subscribe(event_type, self.handle_event)

    # -- matching ---------------------------------------------------------
    def _trigger_matches(self, trigger: SceneTrigger, event: Event) -> bool:
        if trigger.type != event.type:
            return False
        if trigger.type == "geofence":
            return (
                event.get("zone") == trigger.zone
                and event.get("transition") == trigger.transition
            )
        if trigger.type == "schedule":
            if trigger.at:
                return event.get("time") == trigger.at
            if trigger.every_minutes:
                minute = event.get("minute")
                return isinstance(minute, int) and minute % trigger.every_minutes == 0
            return False
        if trigger.type == "state_change":
            if event.get("device") != trigger.device:
                return False
            if trigger.property and event.get("property") != trigger.property:
                return False
            return True
        return False

    def _conditions_hold(self, conditions: list[SceneCondition]) -> bool:
        for condition in conditions:
            device = self.manager.registry.find(condition.device)
            if device is None:
                return False
            actual = device.state.get(condition.property)
            if not _compare(actual, condition.op, condition.value):
                return False
        return True

    # -- execution ----------------------------------------------------------
    def handle_event(self, event: Event) -> ExecutionReport:
        report = ExecutionReport(event_type=event.type)
        for scene in self.scenes.values():
            if not scene.enabled:
                report.skipped[scene.name] = "disabled"
                continue
            if not self._trigger_matches(scene.trigger, event):
                continue
            report.evaluated.append(scene.name)
            if not self._conditions_hold(scene.conditions):
                report.skipped[scene.name] = "conditions not met"
                continue
            for action in scene.actions:
                report.outcomes.append(self._run_action(scene, action, event))
        return report

    def _run_action(self, scene: Scene, action, event: Event) -> ActionOutcome:
        device = self.manager.registry.find(action.device)
        device_risk = device.risk if device is not None else RiskLevel.LOW
        effective_risk = RiskLevel.max_of(
            scene.risk, action.risk or RiskLevel.LOW, device_risk
        )
        if action.kind == "set":
            description = f"set {action.device}.{action.property} = {action.value!r}"
            kind, name, value = "set_property", action.property, action.value
        else:
            description = f"call {action.device}.{action.action}({action.params})"
            kind, name, value = "call_action", action.action, None

        if effective_risk is RiskLevel.HIGH:
            item = self.confirmations.add(
                device_id=action.device, kind=kind, name=name, value=value,
                params=action.params, requested_by=f"scene:{scene.name}",
                scene=scene.name, risk=effective_risk.value,
            )
            return ActionOutcome(scene.name, action.device, description,
                                 "queued", {"confirmation_id": item.id})

        try:
            if kind == "set_property":
                result = self.manager.set_property(
                    action.device, name, value, agent=f"scene:{scene.name}"
                )
            else:
                result = self.manager.call_action(
                    action.device, name, action.params, agent=f"scene:{scene.name}"
                )
            return ActionOutcome(scene.name, action.device, description,
                                 "executed", result)
        except Exception as exc:
            return ActionOutcome(scene.name, action.device, description,
                                 "failed", str(exc))

    def confirm(self, confirmation_id: str, agent: str = "human"):
        """Execute a previously queued high-risk action, if still pending."""
        item = self.confirmations.get(confirmation_id)
        if item is None or item.status != "pending":
            return None
        if item.kind == "set_property":
            result = self.manager.set_property(
                item.device_id, item.name, item.value, agent=agent
            )
        else:
            result = self.manager.call_action(
                item.device_id, item.name, item.params, agent=agent
            )
        self.confirmations.mark(confirmation_id, "confirmed")
        return result

    def reject(self, confirmation_id: str) -> bool:
        item = self.confirmations.get(confirmation_id)
        if item is None or item.status != "pending":
            return False
        self.confirmations.mark(confirmation_id, "rejected")
        return True


def schedule_event_now() -> Event:
    """Build a schedule event for the current local time (used by the CLI)."""
    now = time.localtime()
    return Event(type="schedule", source="clock",
                 data={"time": time.strftime("%H:%M", now), "minute": now.tm_min})

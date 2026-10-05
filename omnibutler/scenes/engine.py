"""The deterministic scene execution engine.

The engine subscribes to (or is handed) events, finds scenes whose trigger
matches, evaluates all conditions (AND semantics), then runs the actions
through the DeviceManager. Safety guardrail: any action whose effective risk
is *high* - from the scene, the action, or the target device - is never
executed; it is parked in the confirmation queue for a human instead.

Conditions come in two kinds (see scenes/model.py): *state* conditions
compare a device property against a threshold, and *time_window* conditions
hold only inside a local time-of-day window (which may cross midnight).
A state condition may add ``for_seconds``: the engine tracks, from the
state-change events it observes, how long each property has held its
current value (monotonic clock), and the condition only holds once the
value has satisfied the comparison continuously for that long. A value
whose hold start was never observed counts as not holding - the engine
would rather fire late than pretend a duration it did not witness.
When a scene is skipped because a condition does not hold, the specific
reason is written to the audit log - the execution report keeps the stable
"conditions not met" marker. The clock is injectable (``clock=`` callable
returning minutes since midnight, "HH:MM", or a time/datetime) so tests and
replays stay deterministic; by default the event's own "time" field is used
when present, else the local wall clock.
"""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, field
from typing import Any, Callable

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


def _hhmm_to_minutes(value: Any) -> int | None:
    """Minutes since midnight for "HH:MM" / time / datetime, else None."""
    if isinstance(value, _dt.datetime):
        return value.hour * 60 + value.minute
    if isinstance(value, _dt.time):
        return value.hour * 60 + value.minute
    if isinstance(value, str):
        parts = value.strip().split(":")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            hour, minute = int(parts[0]), int(parts[1])
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour * 60 + minute
    return None


def _minutes_to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _in_time_window(now: int, start: int, end: int) -> bool:
    """Start inclusive, end exclusive; start > end crosses midnight."""
    if start == end:
        return True  # a degenerate window means the whole day
    if start < end:
        return start <= now < end
    return now >= start or now < end


class SceneEngine:
    def __init__(
        self,
        manager: DeviceManager,
        confirmations: ConfirmationQueue | None = None,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self.manager = manager
        self.confirmations = confirmations if confirmations is not None else ConfirmationQueue()
        self.scenes: dict[str, Scene] = {}
        self.clock = clock
        # (device_id, property) -> (current value, monotonic time it took
        # that value), fed by every state_change event the engine sees.
        # Backs `for_seconds` state conditions; entries are only ever
        # written from observed events, never seeded from device state.
        self._value_since: dict[tuple[str, str], tuple[Any, float]] = {}

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
        for event_type in ("state_change", "schedule", "geofence",
                           "session_opened", "session_closed"):
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
        if trigger.type in ("session_opened", "session_closed"):
            if trigger.device and event.get("device") != trigger.device:
                return False
            return True
        return False

    # -- conditions ---------------------------------------------------------
    def _track_state_change(self, event: Event) -> None:
        """Record when each (device, property) took its current value.

        Only state_change events carrying a value teach the engine
        anything. A re-report of the same value keeps the original
        timestamp (the hold continues); any different value restarts
        that property's clock.
        """
        if event.type != "state_change" or "value" not in event.data:
            return
        device_id = event.get("device")
        property_name = event.get("property")
        if not device_id or not property_name:
            return
        value = event.get("value")
        key = (device_id, property_name)
        current = self._value_since.get(key)
        if current is None or current[0] != value:
            self._value_since[key] = (value, time.monotonic())

    def _now_minutes(self, event: Event | None = None) -> int:
        """Current time of day in minutes: injected clock > event > wall."""
        if self.clock is not None:
            provided = self.clock()
            if isinstance(provided, int) and not isinstance(provided, bool):
                return provided % (24 * 60)
            parsed = _hhmm_to_minutes(provided)
            if parsed is not None:
                return parsed
        if event is not None:
            parsed = _hhmm_to_minutes(event.get("time"))
            if parsed is not None:
                return parsed
        now = time.localtime()
        return now.tm_hour * 60 + now.tm_min

    def _condition_failure(
        self, conditions: list[SceneCondition], event: Event
    ) -> str | None:
        """None when every condition holds, else the first failure's reason."""
        for condition in conditions:
            if condition.type == "time_window":
                now = self._now_minutes(event)
                start = _hhmm_to_minutes(condition.start)
                end = _hhmm_to_minutes(condition.end)
                if start is None or end is None or not _in_time_window(now, start, end):
                    return (
                        f"outside time window {condition.start}-{condition.end} "
                        f"(now {_minutes_to_hhmm(now)})"
                    )
                continue
            device = self.manager.registry.find(condition.device)
            if device is None:
                return f"state condition not met: device {condition.device!r} not found"
            actual = device.state.get(condition.property)
            if not _compare(actual, condition.op, condition.value):
                return (
                    f"state condition not met: {condition.device}."
                    f"{condition.property} is {actual!r} "
                    f"(expected {condition.op} {condition.value!r})"
                )
            if condition.for_seconds is not None:
                failure = self._duration_failure(condition, actual)
                if failure is not None:
                    return failure
        return None

    def _duration_failure(self, condition: SceneCondition, actual: Any) -> str | None:
        """None when the value has held long enough for ``for_seconds``.

        The hold clock comes only from observed state_change events:
        no observation (engine started after the value settled), or a
        tracked value that no longer matches the device state (it moved
        without an event), both count as "duration unknown" and fail -
        late is acceptable, pretending is not.
        """
        required = condition.for_seconds
        entry = self._value_since.get((condition.device, condition.property))
        if entry is None or entry[0] != actual:
            return (
                f"state condition not met: {condition.device}."
                f"{condition.property} must hold {condition.op} "
                f"{condition.value!r} for {required:g}s, but how long it "
                "has held its current value has not been observed"
            )
        held = time.monotonic() - entry[1]
        if held < required:
            return (
                f"state condition not met: {condition.device}."
                f"{condition.property} has held {condition.op} "
                f"{condition.value!r} for {held:.0f}s "
                f"(needs {required:g}s)"
            )
        return None

    def _conditions_hold(self, conditions: list[SceneCondition]) -> bool:
        return self._condition_failure(conditions, Event(type="internal")) is None

    # -- execution ----------------------------------------------------------
    def handle_event(self, event: Event) -> ExecutionReport:
        self._track_state_change(event)
        report = ExecutionReport(event_type=event.type)
        for scene in self.scenes.values():
            if not scene.enabled:
                report.skipped[scene.name] = "disabled"
                continue
            if not self._trigger_matches(scene.trigger, event):
                continue
            report.evaluated.append(scene.name)
            failure = self._condition_failure(scene.conditions, event)
            if failure is not None:
                report.skipped[scene.name] = "conditions not met"
                self.manager.audit.record(
                    agent=f"scene:{scene.name}",
                    device_id=scene.name,
                    action="scene:skipped",
                    params={"event": event.type, "reason": failure},
                    result={"scene": scene.name, "skipped": True},
                    ok=False,
                    error=failure,
                )
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

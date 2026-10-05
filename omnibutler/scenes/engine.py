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

An action list may also contain pauses (``- delay: <seconds>``, see
:class:`SceneDelay`). The actions before a pause run immediately; the
rest are parked as a pending segment with a monotonic deadline - nothing
ever sleeps - and run when :meth:`SceneEngine.process_due` finds them
due (the daemon calls it on every tick; without a daemon they simply
stay pending). A scene has at most one pending segment: scheduling a
new one *replaces* the scene's still-pending segment, so re-triggering
a scene restarts its countdown instead of stacking continuations.
Delayed actions go through the exact same path as immediate ones, so a
high-risk action after a delay is parked in the confirmation queue when
it comes due, exactly as if the delay were not there.
"""

from __future__ import annotations

import datetime as _dt
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from omnibutler.core.confirmations import ConfirmationQueue
from omnibutler.core.events import Event
from omnibutler.core.manager import DeviceManager
from omnibutler.core.models import RiskLevel
from omnibutler.scenes.model import Scene, SceneCondition, SceneDelay, SceneTrigger


@dataclass
class _DelayedSegment:
    """Actions parked behind a ``delay``, waiting for their deadline."""

    due: float  # time.monotonic() deadline
    scene: Scene
    actions: list  # remaining SceneAction | SceneDelay items, in order


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
        # Post-delay action segments waiting for their pause to elapse,
        # run by process_due(). At most one per scene (see _schedule_delayed).
        self._pending_delayed: list[_DelayedSegment] = []
        # Serializes handle_event/process_due: events arrive from
        # several threads (daemon polling, HA push callbacks, the
        # gateway) and _value_since / _pending_delayed are shared
        # mutable state. Re-entrant so a path that funnels back into
        # handle_event while already holding it cannot self-deadlock.
        self._lock = threading.RLock()

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
                matched = event.get("time") == trigger.at
            elif trigger.every_minutes:
                minute = event.get("minute")
                matched = (isinstance(minute, int)
                           and minute % trigger.every_minutes == 0)
            else:
                return False
            if not matched:
                return False
            if trigger.days is not None:
                return self._event_weekday(event) in trigger.days
            return True
        if trigger.type == "state_change":
            return (event.get("device") == trigger.device
                    and (not trigger.property
                         or event.get("property") == trigger.property))
        if trigger.type in ("session_opened", "session_closed"):
            return not trigger.device or event.get("device") == trigger.device
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

    def _event_weekday(self, event: Event) -> int:
        """Weekday (Mon=0 .. Sun=6) of the event, in local time.

        Mirrors :meth:`_now_minutes` precedence: an injected clock that
        yields a date/datetime wins (tests and replays); otherwise the
        event's own ``timestamp`` decides. The timestamp is plain epoch
        seconds - it carries no timezone - so it is read as *local* time,
        the same local time the daemon used for the event's "HH:MM".
        """
        if self.clock is not None:
            provided = self.clock()
            if isinstance(provided, _dt.date):
                return provided.weekday()
        return _dt.datetime.fromtimestamp(event.timestamp).weekday()

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
            # State conditions always name a device and a property (the
            # loader enforces it); time_window conditions continued above.
            assert condition.device is not None and condition.property is not None
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
        # The caller only invokes us when for_seconds is set, and state
        # conditions always name a device and a property (loader-enforced).
        assert required is not None
        assert condition.device is not None and condition.property is not None
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
        with self._lock:
            return self._handle_event(event)

    def _handle_event(self, event: Event) -> ExecutionReport:
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
            self._run_segment(scene, scene.actions, event, report,
                              time.monotonic())
        return report

    # -- delayed actions ----------------------------------------------------
    @property
    def delayed_pending(self) -> int:
        """How many post-delay action segments are waiting to come due."""
        return len(self._pending_delayed)

    def _run_segment(
        self,
        scene: Scene,
        actions: list,
        event: Event,
        report: ExecutionReport,
        now: float,
    ) -> None:
        """Run actions in order until a delay (or the end of the list).

        At a :class:`SceneDelay` the remaining actions are parked as a
        pending segment due ``delay`` seconds after ``now`` and this run
        stops - the caller never waits. Actions already run are not
        undone if a later segment is replaced or never comes due.
        """
        for position, item in enumerate(actions):
            if isinstance(item, SceneDelay):
                remainder = list(actions[position + 1:])
                if remainder:
                    self._schedule_delayed(scene, remainder, item.seconds, now)
                return
            report.outcomes.append(self._run_action(scene, item, event))

    def _schedule_delayed(
        self, scene: Scene, actions: list, seconds: float, now: float
    ) -> None:
        # Retrigger semantics: ONE pending segment per scene. Scheduling
        # a new segment discards the scene's still-pending one - the
        # newest run's timeline supersedes the stale one (coming home
        # again restarts the hallway light's off-timer) instead of
        # stacking duplicate continuations that would fire back to back.
        self._pending_delayed = [
            pending for pending in self._pending_delayed
            if pending.scene.name != scene.name
        ]
        self._pending_delayed.append(
            _DelayedSegment(due=now + seconds, scene=scene, actions=actions)
        )

    def process_due(self, now: float | None = None) -> ExecutionReport:
        with self._lock:
            return self._process_due(now)

    def _process_due(self, now: float | None = None) -> ExecutionReport:
        """Run every delayed segment whose pause has elapsed.

        ``now`` is a monotonic timestamp (defaults to the real monotonic
        clock); tests drive it by patching ``time.monotonic`` or by
        passing values from the same patched clock. Due segments run in
        deadline order through the same action path as immediate ones -
        including the confirmation queue for high-risk actions - and a
        segment that hits a further delay parks its remainder again.
        The returned report has ``event_type == "delay"`` and lists the
        scenes it ran in ``evaluated``.
        """
        if now is None:
            now = time.monotonic()
        report = ExecutionReport(event_type="delay")
        due = sorted(
            (p for p in self._pending_delayed if p.due <= now),
            key=lambda p: p.due,
        )
        if not due:
            return report
        self._pending_delayed = [
            p for p in self._pending_delayed if p.due > now
        ]
        for segment in due:
            report.evaluated.append(segment.scene.name)
            event = Event(type="delay", source=f"scene:{segment.scene.name}")
            self._run_segment(segment.scene, segment.actions, event, report, now)
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
        """Execute a previously queued high-risk action, if still pending.

        The item is *claimed* before anything executes: claiming moves
        it to the terminal "confirmed" status atomically (see
        :meth:`ConfirmationQueue.claim`), so when two approvers race -
        a human at the CLI and one on the approvals page - exactly one
        of them gets here and the action runs at most once. The loser
        sees the same None as for an unknown or already-decided id.

        If execution itself fails, the item stays "confirmed": it must
        never fall back to pending, or a retry could run a high-risk
        action the operator believes already ran (or was told failed).
        The failure is written to the audit log and re-raised, so the
        human sees exactly what went wrong and can queue a fresh action
        deliberately if they still want it.
        """
        item = self.confirmations.get(confirmation_id)
        if item is None or item.status != "pending":
            return None
        if not self.confirmations.claim(confirmation_id):
            return None  # another approver claimed it first
        try:
            if item.kind == "set_property":
                return self.manager.set_property(
                    item.device_id, item.name, item.value, agent=agent
                )
            return self.manager.call_action(
                item.device_id, item.name, item.params, agent=agent
            )
        except Exception as exc:
            self.manager.audit.record(
                agent=agent, device_id=item.device_id,
                action="confirmation:execute_failed",
                params={"confirmation_id": item.id, "kind": item.kind,
                        "name": item.name, "value": item.value},
                ok=False, error=str(exc),
            )
            raise

    def reject(self, confirmation_id: str) -> bool:
        # Same atomic claim as confirm, with the "rejected" outcome:
        # a reject racing an approve can never both win.
        return self.confirmations.claim(confirmation_id, status="rejected")


def schedule_event_now() -> Event:
    """Build a schedule event for the current local time (used by the CLI)."""
    now = time.localtime()
    return Event(type="schedule", source="clock",
                 data={"time": time.strftime("%H:%M", now), "minute": now.tm_min})

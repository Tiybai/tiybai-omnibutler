"""Scene state conditions with ``for_seconds``: a value must hold
continuously for the given duration before the condition counts.

Loader tests pin the validation (state conditions only, positive
numbers); engine tests drive a fake monotonic clock and scripted
state_change events through the mock purifier - no real waiting.
"""

import time

import pytest

from omnibutler.core.events import Event
from omnibutler.scenes.engine import SceneEngine
from omnibutler.scenes.loader import SceneValidationError, parse_scene


@pytest.fixture()
def clock(monkeypatch):
    """A controllable time.monotonic, in seconds."""
    now = [10_000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    return now


def _base_scene(**overrides):
    scene = {
        "name": "demo",
        "trigger": {"type": "geofence", "zone": "home", "transition": "enter"},
        "actions": [{"device": "living_light", "set": {"onoff": True}}],
    }
    scene.update(overrides)
    return scene


def _state_condition(**overrides):
    condition = {"device": "air_purifier", "property": "pm25",
                 "op": ">", "value": 75}
    condition.update(overrides)
    return condition


# -- loader -------------------------------------------------------------------

def test_loader_accepts_for_seconds_on_state_condition():
    scene = parse_scene(_base_scene(
        conditions=[_state_condition(for_seconds=600)]))
    assert scene.conditions[0].for_seconds == 600


def test_loader_for_seconds_defaults_to_none():
    scene = parse_scene(_base_scene(conditions=[_state_condition()]))
    assert scene.conditions[0].for_seconds is None


def test_loader_rejects_for_seconds_on_time_window_flat():
    with pytest.raises(SceneValidationError) as exc:
        parse_scene(_base_scene(conditions=[
            {"type": "time_window", "start": "22:00", "end": "06:00",
             "for_seconds": 60},
        ]))
    assert "for_seconds" in str(exc.value)


def test_loader_rejects_for_seconds_on_time_window_nested():
    with pytest.raises(SceneValidationError) as exc:
        parse_scene(_base_scene(conditions=[
            {"time_window": {"start": "22:00", "end": "06:00",
                             "for_seconds": 60}},
        ]))
    assert "for_seconds" in str(exc.value)


@pytest.mark.parametrize("bad", [-5, 0, "600", True])
def test_loader_rejects_non_positive_or_non_numeric_for_seconds(bad):
    with pytest.raises(SceneValidationError) as exc:
        parse_scene(_base_scene(
            conditions=[_state_condition(for_seconds=bad)]))
    assert "for_seconds" in str(exc.value)


# -- engine -------------------------------------------------------------------

def _engine(manager, trigger=None):
    engine = SceneEngine(manager)
    engine.add_scene(parse_scene({
        "name": "pm25-sustained",
        "trigger": trigger or {"type": "state_change",
                               "device": "air_purifier", "property": "pm25"},
        "conditions": [_state_condition(for_seconds=600)],
        "actions": [{"device": "air_purifier", "set": {"mode": "turbo"}}],
    }))
    return engine


def _report_pm25(engine, manager, value):
    """Deliver a sensor reading the way the manager/bus would."""
    manager.registry.get("air_purifier").state["pm25"] = value
    return engine.handle_event(Event(type="state_change", data={
        "device": "air_purifier", "property": "pm25", "value": value}))


def test_fires_only_after_value_holds_for_the_duration(manager, clock):
    engine = _engine(manager)
    report = _report_pm25(engine, manager, 90)
    assert report.skipped.get("pm25-sustained") == "conditions not met"
    assert manager.get_state("air_purifier")["mode"] == "auto"

    clock[0] += 599  # one second short
    report = _report_pm25(engine, manager, 90)
    assert report.skipped.get("pm25-sustained") == "conditions not met"
    assert manager.get_state("air_purifier")["mode"] == "auto"

    clock[0] += 2  # 601s of holding above 75
    report = _report_pm25(engine, manager, 90)
    assert "pm25-sustained" in report.evaluated
    assert "pm25-sustained" not in report.skipped
    assert manager.get_state("air_purifier")["mode"] == "turbo"


def test_dipping_out_of_the_comparison_restarts_the_clock(manager, clock):
    engine = _engine(manager)
    _report_pm25(engine, manager, 90)          # hold starts
    clock[0] += 500
    _report_pm25(engine, manager, 40)          # dips below the threshold
    clock[0] += 100
    _report_pm25(engine, manager, 90)          # hold restarts here
    clock[0] += 500                            # only 500s into the new hold
    report = _report_pm25(engine, manager, 90)
    assert report.skipped.get("pm25-sustained") == "conditions not met"
    clock[0] += 101
    report = _report_pm25(engine, manager, 90)
    assert "pm25-sustained" not in report.skipped
    assert manager.get_state("air_purifier")["mode"] == "turbo"


def test_value_change_while_satisfying_restarts_the_clock(manager, clock):
    engine = _engine(manager)
    _report_pm25(engine, manager, 90)          # hold starts at 90
    clock[0] += 500
    _report_pm25(engine, manager, 120)         # still > 75, but a new value
    clock[0] += 599
    report = _report_pm25(engine, manager, 120)
    assert report.skipped.get("pm25-sustained") == "conditions not met"
    clock[0] += 2
    report = _report_pm25(engine, manager, 120)
    assert "pm25-sustained" not in report.skipped
    assert manager.get_state("air_purifier")["mode"] == "turbo"


def test_unobserved_hold_never_counts_as_sustained(manager, clock):
    # Trigger is a schedule tick, so the only way the engine can learn
    # about pm25 is a state_change event - and none has happened yet:
    # the purifier's state was set directly, behind the engine's back.
    engine = _engine(manager, trigger={"type": "schedule", "at": "08:00"})
    manager.registry.get("air_purifier").state["pm25"] = 90
    tick = Event(type="schedule", data={"time": "08:00", "minute": 0})

    report = engine.handle_event(tick)
    assert report.skipped.get("pm25-sustained") == "conditions not met"

    clock[0] += 3600  # an hour passes; still nothing observed
    report = engine.handle_event(tick)
    assert report.skipped.get("pm25-sustained") == "conditions not met"
    assert manager.get_state("air_purifier")["mode"] == "auto"

    # Once a reading is actually observed, the duration starts then.
    _report_pm25(engine, manager, 90)
    clock[0] += 601
    report = engine.handle_event(tick)
    assert "pm25-sustained" not in report.skipped
    assert manager.get_state("air_purifier")["mode"] == "turbo"

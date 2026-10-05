import pytest

from omnibutler.core.events import Event
from omnibutler.scenes.loader import SceneValidationError, parse_scene


def _base_scene(**overrides):
    scene = {
        "name": "demo",
        "trigger": {"type": "geofence", "zone": "home", "transition": "enter"},
        "actions": [{"device": "living_light", "set": {"onoff": True}}],
    }
    scene.update(overrides)
    return scene


# -- loader / validation ----------------------------------------------------

def test_bundled_scenes_are_valid(engine):
    names = {s.name for s in engine.list_scenes()}
    assert {"arrive-home", "leave-home-check", "sleep-mode",
            "garage-arrival", "air-quality-guard"} <= names


def test_validation_missing_name():
    with pytest.raises(SceneValidationError):
        parse_scene({"trigger": {"type": "schedule", "at": "07:00"}, "actions": []})


def test_validation_bad_trigger_type():
    with pytest.raises(SceneValidationError) as exc:
        parse_scene(_base_scene(trigger={"type": "telepathy"}))
    assert "trigger.type" in str(exc.value)


def test_validation_geofence_needs_transition():
    with pytest.raises(SceneValidationError) as exc:
        parse_scene(_base_scene(trigger={"type": "geofence", "zone": "home"}))
    assert "transition" in str(exc.value)


def test_validation_action_needs_set_or_action():
    with pytest.raises(SceneValidationError):
        parse_scene(_base_scene(actions=[{"device": "living_light"}]))


def test_validation_bad_condition_op():
    scene = _base_scene(conditions=[{"device": "living_ac", "property": "onoff",
                                     "op": "~=", "value": True}])
    with pytest.raises(SceneValidationError):
        parse_scene(scene)


def test_validation_schedule_format():
    with pytest.raises(SceneValidationError):
        parse_scene(_base_scene(trigger={"type": "schedule", "at": "half past ten"}))


# -- execution ----------------------------------------------------------------

def _geo(zone="home", transition="enter"):
    return Event(type="geofence", data={"zone": zone, "transition": transition})


def test_arrive_home_executes(engine, manager):
    report = engine.handle_event(_geo())
    assert "arrive-home" in report.evaluated
    assert manager.get_state("living_ac")["onoff"] is True
    assert manager.get_state("living_ac")["target_temperature"] == 26
    assert manager.get_state("air_purifier")["onoff"] is True


def test_leave_home_turns_everything_off(engine, manager):
    manager.set_property("living_ac", "onoff", True)
    manager.set_property("living_light", "onoff", True)
    report = engine.handle_event(_geo(transition="exit"))
    assert "leave-home-check" in report.evaluated
    assert manager.get_state("living_ac")["onoff"] is False
    assert manager.get_state("living_light")["onoff"] is False


def test_conditions_block_execution(engine, manager):
    # arrive-home only fires while the living AC is off; turn it on first.
    manager.set_property("living_ac", "onoff", True)
    report = engine.handle_event(_geo())
    assert report.skipped.get("arrive-home") == "conditions not met"


def test_sleep_mode_schedule(engine, manager):
    report = engine.handle_event(Event(type="schedule", data={"time": "22:30", "minute": 30}))
    assert "sleep-mode" in report.evaluated
    assert manager.get_state("bedroom_ac")["target_temperature"] == 27
    assert manager.get_state("bedroom_curtain")["position"] == 0
    assert manager.get_state("air_purifier")["mode"] == "silent"
    # A different time does not fire it.
    report2 = engine.handle_event(Event(type="schedule", data={"time": "08:00", "minute": 0}))
    assert "sleep-mode" not in report2.evaluated


def test_air_quality_guard(engine, manager):
    manager.registry.get("air_purifier").state["pm25"] = 90
    event = Event(type="state_change",
                  data={"device": "air_purifier", "property": "pm25", "value": 90})
    report = engine.handle_event(event)
    assert "air-quality-guard" in report.evaluated
    assert manager.get_state("air_purifier")["mode"] == "turbo"


def test_disabled_scene_does_not_run(engine, manager):
    engine.set_enabled("arrive-home", False)
    report = engine.handle_event(_geo())
    assert report.skipped.get("arrive-home") == "disabled"
    assert manager.get_state("living_ac")["onoff"] is False

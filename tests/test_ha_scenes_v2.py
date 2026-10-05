"""v0.3: HA driver hardening + scene time_window / state conditions.

HA tests never touch the network: ``urllib.request.urlopen`` is replaced
with scripted fakes that record calls and replay queued outcomes.
"""

import json
import urllib.error
import urllib.request

import pytest

from omnibutler.core.errors import DeviceNotFoundError, OmniButlerError
from omnibutler.core.events import Event
from omnibutler.drivers.homeassistant import (
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
    HomeAssistantDriver,
    HomeAssistantError,
    HomeAssistantTimeoutError,
)
from omnibutler.scenes.engine import SceneEngine
from omnibutler.scenes.loader import SceneValidationError, parse_scene

# -- HA fakes -----------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class _ScriptedUrlopen:
    """Pops one outcome per call: exceptions are raised, values returned."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, request, timeout=None):
        self.outcomes_remaining = self.outcomes
        self.calls.append({"url": request.full_url, "timeout": timeout})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)


@pytest.fixture()
def ha_driver():
    return HomeAssistantDriver(base_url="http://ha.local:8123", token="tok")


def _patch(monkeypatch, scripted):
    monkeypatch.setattr(urllib.request, "urlopen", scripted)
    return scripted


# -- HA: retry + error classification ------------------------------------------

def test_ha_retries_once_then_succeeds(monkeypatch, ha_driver):
    scripted = _patch(monkeypatch, _ScriptedUrlopen([
        urllib.error.URLError("connection reset"), [],
    ]))
    assert ha_driver.list_devices() == []
    assert len(scripted.calls) == 2


def test_ha_timeout_is_classified_after_one_retry(monkeypatch, ha_driver):
    scripted = _patch(monkeypatch, _ScriptedUrlopen([
        urllib.error.URLError(TimeoutError("timed out")),
        urllib.error.URLError(TimeoutError("timed out")),
    ]))
    with pytest.raises(HomeAssistantTimeoutError) as exc:
        ha_driver.list_devices()
    assert isinstance(exc.value, HomeAssistantConnectionError)
    assert isinstance(exc.value, OmniButlerError)
    assert len(scripted.calls) == 2


def test_ha_connection_failure_is_classified_after_one_retry(monkeypatch, ha_driver):
    scripted = _patch(monkeypatch, _ScriptedUrlopen([
        urllib.error.URLError(ConnectionRefusedError(111, "refused")),
        urllib.error.URLError(ConnectionRefusedError(111, "refused")),
    ]))
    with pytest.raises(HomeAssistantConnectionError) as exc:
        ha_driver.list_devices()
    assert not isinstance(exc.value, HomeAssistantTimeoutError)
    assert "HA_URL" in str(exc.value)
    assert len(scripted.calls) == 2


def test_ha_401_is_auth_error_and_never_retried(monkeypatch, ha_driver):
    scripted = _patch(monkeypatch, _ScriptedUrlopen([
        urllib.error.HTTPError("http://ha.local:8123/api/states", 401,
                               "Unauthorized", None, None),
    ]))
    with pytest.raises(HomeAssistantAuthError) as exc:
        ha_driver.list_devices()
    assert "401" in str(exc.value) and "HA_TOKEN" in str(exc.value)
    assert len(scripted.calls) == 1


def test_ha_500_is_retried_once_then_plain_error(monkeypatch, ha_driver):
    scripted = _patch(monkeypatch, _ScriptedUrlopen([
        urllib.error.HTTPError("http://ha.local:8123/api/states", 500,
                               "Server Error", None, None),
        urllib.error.HTTPError("http://ha.local:8123/api/states", 500,
                               "Server Error", None, None),
    ]))
    with pytest.raises(HomeAssistantError) as exc:
        ha_driver.list_devices()
    assert not isinstance(exc.value, HomeAssistantAuthError)
    assert "HTTP 500" in str(exc.value)
    assert len(scripted.calls) == 2


def test_ha_timeout_is_configurable(monkeypatch):
    driver = HomeAssistantDriver(base_url="http://ha.local:8123", token="t",
                                 timeout=3)
    assert driver.timeout == 3
    scripted = _patch(monkeypatch, _ScriptedUrlopen([[]]))
    driver.list_devices()
    assert scripted.calls[0]["timeout"] == 3


def test_ha_timeout_from_env(monkeypatch):
    monkeypatch.setenv("HA_TIMEOUT", "7")
    driver = HomeAssistantDriver(base_url="http://ha.local:8123", token="t")
    assert driver.timeout == 7


# -- HA: entity prefix filter ---------------------------------------------------

_STATES = [
    {"entity_id": "climate.living", "state": "cool",
     "attributes": {"friendly_name": "Living AC"}},
    {"entity_id": "light.kitchen", "state": "on",
     "attributes": {"friendly_name": "Kitchen Light"}},
    {"entity_id": "switch.tv", "state": "off",
     "attributes": {"friendly_name": "TV Plug"}},
]


def test_ha_entity_prefix_filter(monkeypatch):
    driver = HomeAssistantDriver(base_url="http://ha.local:8123", token="t",
                                 entity_prefixes=["climate.", "light."])
    _patch(monkeypatch, _ScriptedUrlopen([list(_STATES)]))
    assert [d.id for d in driver.list_devices()] == ["climate.living",
                                                      "light.kitchen"]
    with pytest.raises(DeviceNotFoundError):
        driver.get_state("switch.tv")


def test_ha_entity_prefixes_from_env(monkeypatch):
    monkeypatch.setenv("HA_ENTITY_PREFIXES", "climate., switch.")
    driver = HomeAssistantDriver(base_url="http://ha.local:8123", token="t")
    assert driver.entity_prefixes == ("climate.", "switch.")
    _patch(monkeypatch, _ScriptedUrlopen([list(_STATES)]))
    assert [d.id for d in driver.list_devices()] == ["climate.living",
                                                      "switch.tv"]


def test_ha_no_prefix_filter_keeps_everything(monkeypatch, ha_driver):
    _patch(monkeypatch, _ScriptedUrlopen([list(_STATES)]))
    assert len(ha_driver.list_devices()) == 3


# -- HA: attribute -> state mapping ----------------------------------------------

def test_ha_extended_attribute_mapping(ha_driver):
    climate = ha_driver._device_from_state({
        "entity_id": "climate.bedroom", "state": "cool",
        "attributes": {"friendly_name": "Bedroom AC", "temperature": 25,
                       "current_temperature": 28.5, "hvac_mode": "cool",
                       "humidity": 45, "current_humidity": 52},
    })
    assert climate.state["target_temperature"] == 25
    assert climate.state["current_temperature"] == 28.5
    assert climate.state["humidity"] == 52  # current_humidity wins (later key)

    light = ha_driver._device_from_state({
        "entity_id": "light.desk", "state": "on",
        "attributes": {"friendly_name": "Desk", "brightness": 128,
                       "color_temp": 300},  # legacy mireds
    })
    assert light.state["brightness"] == 50
    assert light.state["color_temp"] == 3333  # 1e6 / 300 mired -> kelvin

    fan = ha_driver._device_from_state({
        "entity_id": "fan.tower", "state": "on",
        "attributes": {"friendly_name": "Tower Fan", "percentage": 60},
    })
    assert fan.state["fan_speed"] == 60


# -- scenes: loader for the new condition kinds ----------------------------------

def _scene_dict(**overrides):
    scene = {
        "name": "night-guard",
        "trigger": {"type": "geofence", "zone": "home", "transition": "enter"},
        "actions": [{"device": "living_light", "set": {"onoff": True}}],
    }
    scene.update(overrides)
    return scene


def test_parse_time_window_flat_and_nested():
    flat = parse_scene(_scene_dict(conditions=[
        {"type": "time_window", "start": "22:00", "end": "06:00"},
    ]))
    assert flat.conditions[0].type == "time_window"
    assert (flat.conditions[0].start, flat.conditions[0].end) == ("22:00", "06:00")

    nested = parse_scene(_scene_dict(conditions=[
        {"time_window": {"start": "08:00", "end": "18:30"}},
    ]))
    assert nested.conditions[0].type == "time_window"
    assert (nested.conditions[0].start, nested.conditions[0].end) == ("08:00", "18:30")


def test_parse_time_window_validation():
    with pytest.raises(SceneValidationError):
        parse_scene(_scene_dict(conditions=[
            {"type": "time_window", "start": "22:00"},  # missing end
        ]))
    with pytest.raises(SceneValidationError):
        parse_scene(_scene_dict(conditions=[
            {"type": "time_window", "start": "tea time", "end": "06:00"},
        ]))
    with pytest.raises(SceneValidationError):
        parse_scene(_scene_dict(conditions=[
            {"type": "time_window", "start": "25:00", "end": "06:00"},
        ]))
    with pytest.raises(SceneValidationError):
        parse_scene(_scene_dict(conditions=[{"type": "moon_phase"}]))


def test_parse_state_condition_op_aliases():
    scene = parse_scene(_scene_dict(conditions=[
        {"device": "living_ac", "property": "current_temperature",
         "op": "greater_than", "value": 30},
        {"device": "air_purifier", "property": "pm25",
         "op": "at_least", "value": 75},
    ]))
    assert scene.conditions[0].type == "state"
    assert scene.conditions[0].op == ">"
    assert scene.conditions[1].op == ">="


# -- scenes: execution ------------------------------------------------------------

def _geo():
    return Event(type="geofence", data={"zone": "home", "transition": "enter"})


def _window_scene(start="22:00", end="06:00"):
    return parse_scene(_scene_dict(conditions=[
        {"type": "time_window", "start": start, "end": end},
    ]))


def _engine_at(manager, minutes, *scenes):
    engine = SceneEngine(manager, clock=lambda: minutes)
    engine.add_scenes(list(scenes))
    return engine


def test_time_window_crossing_midnight(manager):
    inside = _engine_at(manager, 23 * 60 + 30, _window_scene())
    report = inside.handle_event(_geo())
    assert report.skipped == {}
    assert manager.get_state("living_light")["onoff"] is True

    manager.set_property("living_light", "onoff", False)
    outside = _engine_at(manager, 12 * 60, _window_scene())
    report = outside.handle_event(_geo())
    assert report.skipped.get("night-guard") == "conditions not met"
    assert manager.get_state("living_light")["onoff"] is False


def test_time_window_same_day_and_boundaries(manager):
    # start inclusive, end exclusive: 08:00 in, 18:00 out.
    at_start = _engine_at(manager, 8 * 60, _window_scene("08:00", "18:00"))
    assert at_start.handle_event(_geo()).skipped == {}
    at_end = _engine_at(manager, 18 * 60, _window_scene("08:00", "18:00"))
    report = at_end.handle_event(_geo())
    assert report.skipped.get("night-guard") == "conditions not met"


def test_time_window_skip_is_audited_with_reason(manager):
    engine = _engine_at(manager, 12 * 60, _window_scene())
    engine.handle_event(_geo())
    skips = [e for e in manager.audit.read_all()
             if e["action"] == "scene:skipped"]
    assert len(skips) == 1
    assert skips[0]["agent"] == "scene:night-guard"
    assert "outside time window 22:00-06:00" in skips[0]["error"]
    assert "12:00" in skips[0]["error"]


def test_time_window_falls_back_to_event_time(manager):
    # No injected clock: a schedule event carries its own time.
    scene = parse_scene({
        "name": "late-check",
        "trigger": {"type": "schedule", "at": "23:15"},
        "conditions": [{"type": "time_window", "start": "22:00", "end": "06:00"}],
        "actions": [{"device": "living_light", "set": {"onoff": True}}],
    })
    engine = SceneEngine(manager)
    engine.add_scene(scene)
    report = engine.handle_event(
        Event(type="schedule", data={"time": "23:15", "minute": 15}))
    assert report.skipped == {}
    assert manager.get_state("living_light")["onoff"] is True


def test_state_condition_threshold_and_audit_reason(manager):
    scene = parse_scene(_scene_dict(conditions=[
        {"device": "air_purifier", "property": "pm25",
         "op": "greater_than", "value": 75},
    ]))
    engine = _engine_at(manager, 12 * 60, scene)

    manager.registry.get("air_purifier").state["pm25"] = 30
    report = engine.handle_event(_geo())
    assert report.skipped.get("night-guard") == "conditions not met"
    skips = [e for e in manager.audit.read_all()
             if e["action"] == "scene:skipped"]
    assert len(skips) == 1
    assert "air_purifier.pm25 is 30" in skips[0]["error"]
    assert "> 75" in skips[0]["error"]

    manager.registry.get("air_purifier").state["pm25"] = 90
    report = engine.handle_event(_geo())
    assert report.skipped == {}
    assert manager.get_state("living_light")["onoff"] is True


def test_state_condition_equals_and_less_than(manager):
    scene = parse_scene(_scene_dict(conditions=[
        {"device": "living_ac", "property": "onoff",
         "op": "equals", "value": False},
        {"device": "living_ac", "property": "current_temperature",
         "op": "less_than", "value": 30},
    ]))
    engine = _engine_at(manager, 12 * 60, scene)
    # living_ac starts off with current_temperature 29: both hold.
    assert engine.handle_event(_geo()).skipped == {}
    manager.set_property("living_ac", "onoff", True)
    report = engine.handle_event(_geo())
    assert report.skipped.get("night-guard") == "conditions not met"


def test_conditions_and_together(manager):
    scene = parse_scene(_scene_dict(conditions=[
        {"type": "time_window", "start": "22:00", "end": "06:00"},
        {"device": "living_ac", "property": "onoff", "op": "==", "value": True},
    ]))
    # In the window but the state condition fails -> still skipped.
    engine = _engine_at(manager, 23 * 60, scene)
    report = engine.handle_event(_geo())
    assert report.skipped.get("night-guard") == "conditions not met"
    assert manager.get_state("living_light")["onoff"] is False


def test_high_risk_still_queued_when_window_passes(manager):
    scene = parse_scene({
        "name": "night-garage",
        "trigger": {"type": "geofence", "zone": "home", "transition": "enter"},
        "conditions": [{"type": "time_window", "start": "22:00", "end": "06:00"}],
        "actions": [{"device": "garage_door", "action": "open", "risk": "high"}],
    })
    engine = _engine_at(manager, 23 * 60, scene)
    report = engine.handle_event(_geo())
    assert [o.status for o in report.outcomes] == ["queued"]
    assert manager.get_state("garage_door")["open_close"] is False
    assert len(engine.confirmations.pending()) == 1

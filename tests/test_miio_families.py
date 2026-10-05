"""Tests for the miio driver's generic MIoT families: light, fan,
humidifier (v0.7).

The three family tables follow the MIoT specification's standard
service definitions for those device types, so these tests drive the
same independent fake-device oracle as test_miio_driver.py (imported,
not copied, so the wire format has exactly one test-side author) and
assert both the wire addresses (siid/piid) and the value translations.
"""

import pytest

from omnibutler.core.errors import PropertyValidationError
from omnibutler.drivers.miio import _kind_from_model
from test_miio_driver import TOKEN, FakeMiioDevice, _driver_for


# -- family selection ---------------------------------------------------------

@pytest.mark.parametrize("model,family", [
    # humidifier
    ("zhimi.humidifier.cb1", "humidifier"),
    ("deerma.humidifier.jsq1", "humidifier"),
    # fan
    ("dmaker.fan.p5", "fan"),
    ("zhimi.fan.za5", "fan"),
    ("smartmi.fan.3", "fan"),
    # light
    ("yeelink.light.lamp1", "light"),
    ("yeelink.light.strip6", "light"),
    ("philips.light.bulb", "light"),
    ("xiaomi.lamp.test", "light"),
    ("yeelight.650", "light"),
    # regression: the original two families must not be reclassified
    ("zhimi.aircondition.v1", "air_conditioner"),
    ("xiaomi.airconditioner.m4", "air_conditioner"),
    ("xiaomi.aircondition.test", "air_conditioner"),
    ("zhimi.airpurifier.ma4", "air_purifier"),
    ("zhimi.airpurifier.test", "air_purifier"),
    # and unknown models stay unknown
    ("unknown.gadget.v9", ""),
    ("", ""),
])
def test_kind_from_model(model, family):
    assert _kind_from_model(model) == family


# -- light ----------------------------------------------------------------------

@pytest.fixture()
def fake_light():
    device = FakeMiioDevice(
        TOKEN, device_id=11223344,
        store={(2, 1): True, (2, 2): 60, (2, 3): 4000},
        model="yeelink.light.test",
    )
    device.start()
    yield device
    device.stop()


def test_light_state_and_control_roundtrip(fake_light):
    driver = _driver_for(fake_light, "desk_light", "yeelink.light.test")
    devices = driver.list_devices()
    assert set(devices[0].properties) == {"onoff", "brightness", "color_temp"}

    # Brightness and colour temperature pass through unscaled.
    state = driver.get_state("desk_light")
    assert state == {"onoff": True, "brightness": 60, "color_temp": 4000}

    assert driver.set_property("desk_light", "brightness", 80) == {"brightness": 80}
    assert driver.set_property("desk_light", "color_temp", 3000) == {"color_temp": 3000}
    assert driver.call_action("desk_light", "turn_off", {}) == {"onoff": False}

    # Wire values landed on the standard Light service addresses.
    assert fake_light.store[(2, 1)] is False
    assert fake_light.store[(2, 2)] == 80
    assert fake_light.store[(2, 3)] == 3000
    assert driver.get_state("desk_light") == {
        "onoff": False, "brightness": 80, "color_temp": 3000}


# -- fan -------------------------------------------------------------------------

@pytest.fixture()
def fake_fan():
    device = FakeMiioDevice(
        TOKEN, device_id=55667788,
        store={(2, 1): False, (2, 2): 2},
        model="dmaker.fan.test",
    )
    device.start()
    yield device
    device.stop()


def test_fan_speed_levels_roundtrip(fake_fan):
    driver = _driver_for(fake_fan, "tower_fan", "dmaker.fan.test")
    devices = driver.list_devices()
    assert set(devices[0].properties) == {"onoff", "fan_speed"}

    # Wire step index 2 is step "3" -> 75%.
    assert driver.get_state("tower_fan") == {"onoff": False, "fan_speed": 75}

    # Canonical percent -> nearest step on the wire, and back.
    assert driver.set_property("tower_fan", "fan_speed", 50) == {"fan_speed": 50}
    assert fake_fan.store[(2, 2)] == 1  # step "2"
    assert driver.get_state("tower_fan")["fan_speed"] == 50

    assert driver.set_property("tower_fan", "fan_speed", 100) == {"fan_speed": 100}
    assert fake_fan.store[(2, 2)] == 3  # step "4"
    assert driver.get_state("tower_fan")["fan_speed"] == 100

    assert driver.set_property("tower_fan", "fan_speed", 25) == {"fan_speed": 25}
    assert fake_fan.store[(2, 2)] == 0  # step "1"
    assert driver.get_state("tower_fan")["fan_speed"] == 25


# -- humidifier --------------------------------------------------------------------

@pytest.fixture()
def fake_humidifier():
    device = FakeMiioDevice(
        TOKEN, device_id=99001122,
        store={(2, 1): True, (2, 2): 0, (3, 1): 55},
        model="zhimi.humidifier.test",
    )
    device.start()
    yield device
    device.stop()


def test_humidifier_state_control_and_raw_reading(fake_humidifier):
    driver = _driver_for(fake_humidifier, "humidifier", "zhimi.humidifier.test")
    devices = driver.list_devices()
    # humidity_reading is a raw reading, not a surfaced Property.
    assert set(devices[0].properties) == {"onoff", "fan_speed"}

    state = driver.get_state("humidifier")
    assert state == {"onoff": True, "fan_speed": 25, "humidity_reading": 55}

    assert driver.set_property("humidifier", "fan_speed", 75) == {"fan_speed": 75}
    assert fake_humidifier.store[(2, 2)] == 2  # step "3"
    assert driver.get_state("humidifier")["fan_speed"] == 75

    with pytest.raises(PropertyValidationError):
        driver.set_property("humidifier", "humidity_reading", 40)  # read-only

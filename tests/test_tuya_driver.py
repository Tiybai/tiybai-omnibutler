"""Tuya driver tests, run against a fake tinytuya module injected into
sys.modules - no real library, no network, no hardware."""

import json
import sys
import types
from typing import ClassVar

import pytest

from omnibutler.core.errors import (
    DeviceNotFoundError,
    DriverNotConfiguredError,
    PlannedDriverError,
    PropertyValidationError,
)
from omnibutler.drivers.tuya import TuyaDriver

SECRET = "unit-test-local-key-000"


class _FakeDevice:
    switch_dp = "1"
    initial_dps: ClassVar[dict] = {}

    def __init__(self, dev_id, address, local_key=None, version=None):
        self.dev_id = dev_id
        self.address = address
        self.local_key = local_key
        self.version = version
        self.calls: list = []
        self.dps = dict(self.initial_dps)
        type(self).instances[dev_id] = self

    def status(self):
        return {"dps": dict(self.dps)}

    def turn_on(self):
        self.calls.append(("turn_on",))
        self.dps[self.switch_dp] = True

    def turn_off(self):
        self.calls.append(("turn_off",))
        self.dps[self.switch_dp] = False

    def set_value(self, index, value):
        self.calls.append(("set_value", index, value))
        self.dps[str(index)] = value


class FakeOutletDevice(_FakeDevice):
    instances: ClassVar[dict] = {}
    switch_dp = "1"
    initial_dps: ClassVar[dict] = {"1": False, "17": 1250, "19": 0}


class FakeBulbDevice(_FakeDevice):
    instances: ClassVar[dict] = {}
    switch_dp = "20"
    initial_dps: ClassVar[dict] = {"20": False, "21": "white", "22": 505, "23": 500}

    def set_brightness(self, brightness):
        self.calls.append(("set_brightness", brightness))
        self.dps["22"] = brightness

    def set_colourtemp(self, colourtemp):
        self.calls.append(("set_colourtemp", colourtemp))
        self.dps["23"] = colourtemp

    def set_colour(self, red, green, blue):
        self.calls.append(("set_colour", red, green, blue))


@pytest.fixture()
def fake_tinytuya(monkeypatch):
    module = types.ModuleType("tinytuya")
    module.OutletDevice = FakeOutletDevice
    module.BulbDevice = FakeBulbDevice
    FakeOutletDevice.instances = {}
    FakeBulbDevice.instances = {}
    monkeypatch.setitem(sys.modules, "tinytuya", module)
    return module


def _driver() -> TuyaDriver:
    return TuyaDriver(devices=[
        {"id": "plug", "device_id": "bf111plug", "ip": "192.168.1.50",
         "local_key": SECRET, "kind": "outlet", "room": "living"},
        {"id": "bulb", "device_id": "bf222bulb", "ip": "192.168.1.51",
         "local_key": SECRET, "kind": "bulb", "version": 3.4, "room": "bedroom"},
    ])


def test_missing_tinytuya_raises_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "tinytuya", None)  # import now fails
    driver = _driver()  # construction must not need the library
    assert {d.id for d in driver.list_devices()} == {"plug", "bulb"}
    with pytest.raises(PlannedDriverError) as exc:
        driver.set_property("plug", "onoff", True)
    assert "tinytuya" in str(exc.value) and "pip install" in str(exc.value)
    assert SECRET not in str(exc.value)


def test_config_from_env(monkeypatch, fake_tinytuya):
    payload = [{"device_id": "bf999env", "ip": "192.168.1.99",
                "local_key": SECRET, "kind": "bulb", "name": "Env bulb"}]
    monkeypatch.setenv("TUYA_DEVICES_JSON", json.dumps(payload))
    driver = TuyaDriver()
    devices = driver.list_devices()
    assert len(devices) == 1
    assert devices[0].id == "tuya-bf999env"
    assert devices[0].name == "Env bulb"
    assert devices[0].driver == "tuya"


def test_config_missing_field_names_only():
    with pytest.raises(DriverNotConfiguredError) as exc:
        TuyaDriver(devices=[{"device_id": "bf1", "local_key": SECRET}])
    assert "ip" in str(exc.value)
    assert SECRET not in str(exc.value)


def test_local_key_never_surfaces(fake_tinytuya):
    driver = _driver()
    blob = json.dumps([d.to_dict() for d in driver.list_devices()])
    assert SECRET not in blob and SECRET not in repr(driver)


def test_outlet_state_and_switch(fake_tinytuya):
    driver = _driver()
    state = driver.get_state("plug")
    assert state["onoff"] is False
    assert state["power"] == 0.0
    assert state["energy"] == 12.5  # DP17 raw 1250 -> 12.5 kWh

    assert driver.set_property("plug", "onoff", True) == {"onoff": True}
    fake = FakeOutletDevice.instances["bf111plug"]
    assert fake.calls == [("turn_on",)]
    assert fake.local_key == SECRET and fake.address == "192.168.1.50"
    assert driver.get_state("plug")["onoff"] is True

    fake.dps["19"] = 455  # 0.1 W units
    assert driver.get_state("plug")["power"] == 45.5

    driver.call_action("plug", "turn_off", {})
    assert driver.get_state("plug")["onoff"] is False


def test_bulb_brightness_and_color_temp_mapping(fake_tinytuya):
    driver = _driver()
    state = driver.get_state("bulb")
    assert state["brightness"] == 50   # raw 505 -> 50 %
    assert state["color_temp"] == 4600  # raw 500 -> midpoint of 2700-6500 K

    assert driver.set_property("bulb", "brightness", 50) == {"brightness": 50}
    fake = FakeBulbDevice.instances["bf222bulb"]
    assert ("set_brightness", 505) in fake.calls  # 50 % -> raw 505 (10-1000)

    assert driver.set_property("bulb", "color_temp", 4600) == {"color_temp": 4600}
    assert ("set_colourtemp", 500) in fake.calls  # 4600 K -> raw 500

    assert driver.set_property("bulb", "onoff", True) == {"onoff": True}
    assert ("turn_on",) in fake.calls
    assert fake.version == 3.4


def test_bulb_color(fake_tinytuya):
    driver = _driver()
    assert driver.set_property("bulb", "color", "#ff0000") == {"color": "#ff0000"}
    fake = FakeBulbDevice.instances["bf222bulb"]
    assert ("set_colour", 255, 0, 0) in fake.calls
    fake.dps["24"] = f"0000{1000:04x}{1000:04x}"  # red in HSV hex
    assert driver.get_state("bulb")["color"] == "#ff0000"


def test_validation_and_unknown_device(fake_tinytuya):
    driver = _driver()
    with pytest.raises(PropertyValidationError):
        driver.set_property("bulb", "brightness", 101)  # above capability max
    with pytest.raises(PropertyValidationError):
        driver.set_property("plug", "power", 10)  # read-only capability
    with pytest.raises(DeviceNotFoundError):
        driver.get_state("nope")

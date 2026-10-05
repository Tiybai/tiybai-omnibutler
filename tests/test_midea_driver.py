"""Midea driver tests, run against a fake msmart-ng package injected into
sys.modules - no real library, no network, no hardware.

The fake mirrors the msmart-ng surface the driver uses: ``msmart.device.AC``
with async authenticate / refresh / apply, plain state attributes, and the
OperationalMode / FanSpeed / SwingMode enums exposed on the AC class.
"""

import enum
import json
import sys
import types

import pytest

from omnibutler.core.errors import (
    DeviceNotFoundError,
    DriverNotConfiguredError,
    OmniButlerError,
    PlannedDriverError,
    PropertyValidationError,
)
from omnibutler.drivers.midea import MideaDriver

SECRET_TOKEN = "unit-test-token-000"
SECRET_KEY = "unit-test-key-111"


class OperationalMode(enum.Enum):
    AUTO = 1
    COOL = 2
    DRY = 3
    HEAT = 4
    FAN = 5


class FanSpeed(enum.IntEnum):
    SILENT = 20
    LOW = 40
    MEDIUM = 60
    HIGH = 80
    FULL = 100
    AUTO = 102


class SwingMode(enum.Enum):
    OFF = 0
    VERTICAL = 1
    HORIZONTAL = 2
    BOTH = 3


class FakeAC:
    OperationalMode = OperationalMode
    FanSpeed = FanSpeed
    SwingMode = SwingMode

    instances: dict = {}
    fail_auth = False

    def __init__(self, ip=None, port=None, device_id=None):
        self.ip = ip
        self.port = port
        self.device_id = device_id
        self.token = None
        self.key = None
        self.power_state = False
        self.target_temperature = 24.0
        self.indoor_temperature = 27.5
        self.operational_mode = OperationalMode.COOL
        self.fan_speed = FanSpeed.MEDIUM
        self.swing_mode = SwingMode.OFF
        self.refresh_count = 0
        self.apply_count = 0
        type(self).instances[ip] = self

    async def authenticate(self, token, key):
        if type(self).fail_auth:
            # Sloppy library behaviour: the error text carries the secret,
            # which the driver must never let leak into its own errors.
            raise RuntimeError(f"bad credentials token={token} key={key}")
        self.token = token
        self.key = key

    async def refresh(self):
        self.refresh_count += 1

    async def apply(self):
        self.apply_count += 1


@pytest.fixture()
def fake_msmart(monkeypatch):
    package = types.ModuleType("msmart")
    package.__path__ = []
    device_module = types.ModuleType("msmart.device")
    device_module.AC = FakeAC
    package.device = device_module
    FakeAC.instances = {}
    FakeAC.fail_auth = False
    monkeypatch.setitem(sys.modules, "msmart", package)
    monkeypatch.setitem(sys.modules, "msmart.device", device_module)
    return package


def _driver() -> MideaDriver:
    return MideaDriver(devices=[
        {"id": "ac", "ip": "192.168.1.70", "token": SECRET_TOKEN,
         "key": SECRET_KEY, "device_id": 123456789, "room": "bedroom"},
    ])


def test_missing_msmart_raises_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "msmart", None)  # import now fails
    driver = _driver()  # construction must not need the library
    assert [d.id for d in driver.list_devices()] == ["ac"]
    with pytest.raises(PlannedDriverError) as exc:
        driver.get_state("ac")
    assert "msmart-ng" in str(exc.value) and "pip install" in str(exc.value)
    assert SECRET_TOKEN not in str(exc.value)
    assert SECRET_KEY not in str(exc.value)


def test_config_from_env(monkeypatch, fake_msmart):
    payload = [{"ip": "192.168.1.99", "token": SECRET_TOKEN,
                "key": SECRET_KEY, "name": "Env AC"}]
    monkeypatch.setenv("MIDEA_DEVICES_JSON", json.dumps(payload))
    driver = MideaDriver()
    devices = driver.list_devices()
    assert len(devices) == 1
    assert devices[0].name == "Env AC"
    assert devices[0].driver == "midea"
    assert devices[0].id.startswith("midea-")


def test_config_missing_field_names_only():
    with pytest.raises(DriverNotConfiguredError) as exc:
        MideaDriver(devices=[{"ip": "192.168.1.1", "token": SECRET_TOKEN}])
    assert "key" in str(exc.value)
    assert SECRET_TOKEN not in str(exc.value)


def test_credentials_never_surface(fake_msmart):
    driver = _driver()
    driver.get_state("ac")  # authenticate + refresh happened
    fake = FakeAC.instances["192.168.1.70"]
    assert fake.token == SECRET_TOKEN and fake.key == SECRET_KEY
    blob = json.dumps([d.to_dict() for d in driver.list_devices()])
    assert SECRET_TOKEN not in blob and SECRET_KEY not in blob
    assert SECRET_TOKEN not in repr(driver) and SECRET_KEY not in repr(driver)


def test_auth_failure_does_not_leak_credentials(fake_msmart):
    FakeAC.fail_auth = True
    driver = _driver()
    with pytest.raises(OmniButlerError) as exc:
        driver.get_state("ac")
    assert SECRET_TOKEN not in str(exc.value)
    assert SECRET_KEY not in str(exc.value)
    assert "RuntimeError" in str(exc.value)  # type name only


def test_state_mapping(fake_msmart):
    driver = _driver()
    state = driver.get_state("ac")
    assert state == {
        "onoff": False,
        "mode": "cool",
        "target_temperature": 24.0,
        "current_temperature": 27.5,
        "fan_speed": 60,  # FanSpeed.MEDIUM -> 60 %
        "swing": False,
    }
    fake = FakeAC.instances["192.168.1.70"]
    assert fake.port == 6444 and fake.device_id == 123456789
    assert fake.refresh_count >= 1

    # AUTO fan speed has no honest percentage: it must be omitted.
    fake.fan_speed = FanSpeed.AUTO
    assert "fan_speed" not in driver.get_state("ac")


def test_set_property_mappings(fake_msmart):
    driver = _driver()
    fake_holder = {}

    def fake_ac():
        driver.get_state("ac")
        return FakeAC.instances["192.168.1.70"]

    fake = fake_ac()

    assert driver.set_property("ac", "onoff", True) == {"onoff": True}
    assert fake.power_state is True

    assert driver.set_property("ac", "mode", "heat") == {"mode": "heat"}
    assert fake.operational_mode is OperationalMode.HEAT

    assert driver.set_property("ac", "target_temperature", 26) == {
        "target_temperature": 26
    }
    assert fake.target_temperature == 26.0

    driver.set_property("ac", "fan_speed", 30)
    assert fake.fan_speed is FanSpeed.LOW  # 30 % -> LOW band
    driver.set_property("ac", "fan_speed", 85)
    assert fake.fan_speed is FanSpeed.FULL  # 85 % -> FULL band

    assert driver.set_property("ac", "swing", True) == {"swing": True}
    assert fake.swing_mode is SwingMode.VERTICAL
    driver.set_property("ac", "swing", False)
    assert fake.swing_mode is SwingMode.OFF

    assert fake.apply_count >= 6  # every set pushed an apply()


def test_actions(fake_msmart):
    driver = _driver()
    driver.call_action("ac", "turn_on", {})
    fake = FakeAC.instances["192.168.1.70"]
    assert fake.power_state is True
    driver.call_action("ac", "toggle", {})
    assert fake.power_state is False
    with pytest.raises(OmniButlerError):
        driver.call_action("ac", "self_destruct", {})


def test_validation_and_unknown_device(fake_msmart):
    driver = _driver()
    with pytest.raises(PropertyValidationError):
        driver.set_property("ac", "target_temperature", 35)  # profile max 30
    with pytest.raises(PropertyValidationError):
        driver.set_property("ac", "target_temperature", 16)  # profile min 17
    with pytest.raises(PropertyValidationError):
        driver.set_property("ac", "current_temperature", 20)  # read-only
    with pytest.raises(PropertyValidationError):
        driver.set_property("ac", "mode", "turbo")  # not a canonical mode
    with pytest.raises(DeviceNotFoundError):
        driver.get_state("nope")

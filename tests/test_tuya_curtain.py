"""Curtain support in the Tuya local driver and the Tuya cloud driver.

Local tests run against a fake tinytuya module injected into
sys.modules (same pattern as test_tuya_driver.py); cloud tests run
against a fake HTTP callable (same pattern as
test_tuya_cloud_driver.py). No real library, network or hardware.
"""

import json
import sys
import types

import pytest

from omnibutler.cloud_keys import HttpResponse
from omnibutler.core.errors import PropertyValidationError
from omnibutler.drivers.tuya import KIND_PROPERTIES, TuyaDriver
from omnibutler.drivers.tuya_cloud import TuyaCloudDriver

SECRET = "unit-test-local-key-000"

# ---------------------------------------------------------------------------
# Local driver (fake tinytuya)
# ---------------------------------------------------------------------------


class FakeCoverDevice:
    instances: dict = {}
    initial_dps: dict = {"1": "close", "2": 0, "3": 0}

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

    def set_value(self, index, value):
        self.calls.append(("set_value", index, value))
        self.dps[str(index)] = value


class FakeOutletDevice:
    instances: dict = {}

    def __init__(self, *args, **kwargs):  # pragma: no cover - fallback guard
        raise AssertionError("curtain devices must not use OutletDevice "
                             "when CoverDevice is available")


@pytest.fixture()
def fake_tinytuya(monkeypatch):
    module = types.ModuleType("tinytuya")
    module.CoverDevice = FakeCoverDevice
    module.OutletDevice = FakeOutletDevice
    FakeCoverDevice.instances = {}
    monkeypatch.setitem(sys.modules, "tinytuya", module)
    return module


def _driver() -> TuyaDriver:
    return TuyaDriver(devices=[
        {"id": "curtain", "device_id": "bf333curtain", "ip": "192.168.1.52",
         "local_key": SECRET, "kind": "curtain", "room": "bedroom"},
    ])


def _fake(driver: TuyaDriver) -> FakeCoverDevice:
    driver.get_state("curtain")  # forces the connection
    return FakeCoverDevice.instances["bf333curtain"]


def test_curtain_discover_and_properties(fake_tinytuya):
    driver = _driver()
    devices = driver.discover()
    assert [d.id for d in devices] == ["curtain"]
    device = devices[0]
    assert set(device.properties) == {"open_close", "position"}
    assert device.properties == KIND_PROPERTIES["curtain"]
    fake = _fake(driver)
    assert isinstance(fake, FakeCoverDevice)  # CoverDevice, not OutletDevice
    assert fake.local_key == SECRET and fake.address == "192.168.1.52"


def test_curtain_state_percent_state_wins(fake_tinytuya):
    driver = _driver()
    fake = _fake(driver)
    fake.dps = {"1": "open", "2": 40, "3": 65}
    state = driver.get_state("curtain")
    assert state["open_close"] is True
    assert state["position"] == 65  # DP3 percent_state beats DP2


def test_curtain_state_percent_control_fallback(fake_tinytuya):
    driver = _driver()
    fake = _fake(driver)
    fake.dps = {"1": "close", "2": 30}  # no DP3 reported
    state = driver.get_state("curtain")
    assert state["open_close"] is False
    assert state["position"] == 30


def test_curtain_state_stop_infers_from_position(fake_tinytuya):
    driver = _driver()
    fake = _fake(driver)
    fake.dps = {"1": "stop", "3": 45}
    assert driver.get_state("curtain")["open_close"] is True
    fake.dps = {"1": "stop", "3": 0}
    assert driver.get_state("curtain")["open_close"] is False
    fake.dps = {"1": "stop"}  # halted, position unknown: invent nothing
    state = driver.get_state("curtain")
    assert "open_close" not in state and "position" not in state


def test_curtain_set_open_close_and_position(fake_tinytuya):
    driver = _driver()
    fake = _fake(driver)
    fake.calls.clear()

    assert driver.set_property("curtain", "open_close", True) == {
        "open_close": True}
    assert fake.calls[-1] == ("set_value", 1, "open")
    assert driver.set_property("curtain", "open_close", False) == {
        "open_close": False}
    assert fake.calls[-1] == ("set_value", 1, "close")

    driver.set_property("curtain", "position", 55.6)
    call = fake.calls[-1]
    assert call[:2] == ("set_value", 2)
    assert call[2] == 56 and isinstance(call[2], int)  # rounded + clamped int

    driver.set_property("curtain", "position", 100)
    assert fake.calls[-1] == ("set_value", 2, 100)

    with pytest.raises(PropertyValidationError):
        driver.set_property("curtain", "position", 101)  # above the max


def test_curtain_actions_mean_open_close(fake_tinytuya):
    driver = _driver()
    fake = _fake(driver)
    fake.dps = {"1": "close", "2": 0, "3": 0}
    driver.call_action("curtain", "turn_on", {})
    assert fake.dps["1"] == "open"
    driver.call_action("curtain", "toggle", {})
    assert fake.dps["1"] == "close"


# ---------------------------------------------------------------------------
# Cloud driver (fake HTTP)
# ---------------------------------------------------------------------------

ACCESS_ID = "test-access-id"
ACCESS_SECRET = "sekret-value-123"
CURTAIN_ID = "bfcurtain001"

CURTAIN_STATUS = [
    {"code": "control", "value": "stop"},
    {"code": "percent_control", "value": 40},
    {"code": "percent_state", "value": 65},
]


def _ok(result) -> HttpResponse:
    return HttpResponse(200, json.dumps({"success": True, "result": result}))


class FakeCurtainHttp:
    def __init__(self) -> None:
        self.commands: list[dict] = []

    def __call__(self, method, url, *, headers=None, data=None, timeout=None):
        if "/v1.0/token" in url:
            return _ok({"access_token": "tok-test", "expire_time": 7200,
                        "uid": "uid-42"})
        if "/status" in url:
            assert CURTAIN_ID in url
            return _ok(CURTAIN_STATUS)
        if "/commands" in url:
            self.commands.extend(json.loads(data)["commands"])
            return _ok(True)
        if "/devices" in url:
            return _ok([{"id": CURTAIN_ID, "name": "Bedroom curtain",
                         "category": "cl", "product_id": "pid-curtain",
                         "online": True}])
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("TUYA_CLOUD_ACCESS_ID", "TUYA_CLOUD_ACCESS_SECRET",
                "TUYA_CLOUD_UID", "TUYA_CLOUD_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


def test_cloud_curtain_mapping():
    http = FakeCurtainHttp()
    driver = TuyaCloudDriver(ACCESS_ID, ACCESS_SECRET, uid="uid-42",
                             http=http, config={})
    devices = driver.discover()
    assert len(devices) == 1
    device = devices[0]
    assert device.properties == KIND_PROPERTIES["curtain"]
    # percent_state (65) wins over percent_control (40); "stop" at a
    # reported position above fully closed reads as open.
    assert device.state["position"] == 65
    assert device.state["open_close"] is True

    driver.set_property(device.id, "open_close", False)
    assert http.commands[-1] == {"code": "control", "value": "close"}
    driver.set_property(device.id, "position", 55.6)
    assert http.commands[-1] == {"code": "percent_control", "value": 56}

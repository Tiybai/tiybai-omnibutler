"""Tests for the Tuya cloud fallback driver (fake HTTP, no real cloud)."""

from __future__ import annotations

import json
import logging

import pytest

from omnibutler.cloud_keys import HttpResponse
from omnibutler.core.errors import DriverNotConfiguredError, PropertyValidationError
from omnibutler.drivers.tuya import KIND_PROPERTIES
from omnibutler.drivers.tuya_cloud import TuyaCloudDriver, TuyaCloudError

ACCESS_ID = "test-access-id"
ACCESS_SECRET = "sekret-value-123"
TOKEN = "tok-test-999"
UID = "uid-42"

BULB_ID = "bfbulb0001"
PLUG_ID = "bfplug0002"

DEVICE_LIST = [
    {"id": BULB_ID, "name": "Desk lamp", "category": "dj",
     "product_id": "pid-bulb", "online": True},
    {"id": PLUG_ID, "name": "Kettle plug", "category": "cz",
     "product_id": "pid-plug", "online": True},
]

BULB_STATUS = [
    {"code": "switch_led", "value": True},
    {"code": "bright_value", "value": 505},
    {"code": "temp_value", "value": 500},
    {"code": "colour_data", "value": json.dumps({"h": 240, "s": 1000, "v": 1000})},
]

PLUG_STATUS = [
    {"code": "switch", "value": True},
    {"code": "cur_power", "value": 123},
    {"code": "add_ele", "value": 456},
]


def _with_switch(status, on):
    return [{**item, "value": on} if item["code"] in ("switch", "switch_led")
            else item for item in status]


def _ok(result) -> HttpResponse:
    return HttpResponse(200, json.dumps({"success": True, "result": result}))


class FakeHttp:
    """Substring-routed fake of the cloud_keys HTTP contract."""

    def __init__(self, *, expire_time: int = 7200,
                 token_uid: str | None = None) -> None:
        self.calls: list[dict] = []
        self.expire_time = expire_time
        self.token_uid = token_uid
        self.bulb_on = True
        self.plug_on = True
        self.token_response: HttpResponse | None = None
        self.list_response: HttpResponse | None = None
        self.command_response: HttpResponse | None = None

    def count(self, part: str) -> int:
        return sum(1 for call in self.calls if part in call["url"])

    def last_call(self, part: str) -> dict:
        matches = [c for c in self.calls if part in c["url"]]
        assert matches, f"no call containing {part!r}"
        return matches[-1]

    def __call__(self, method, url, *, headers=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url,
                           "headers": headers or {}, "data": data})
        if "/v1.0/token" in url:
            if self.token_response is not None:
                return self.token_response
            result = {"access_token": TOKEN, "expire_time": self.expire_time,
                      "refresh_token": "refresh-1"}
            if self.token_uid:
                result["uid"] = self.token_uid
            return _ok(result)
        if "/status" in url:
            if BULB_ID in url:
                return _ok(_with_switch(BULB_STATUS, self.bulb_on))
            if PLUG_ID in url:
                return _ok(_with_switch(PLUG_STATUS, self.plug_on))
            return _ok([])
        if "/commands" in url:
            if self.command_response is not None:
                return self.command_response
            for command in json.loads(data)["commands"]:
                if command["code"] in ("switch", "switch_led"):
                    if BULB_ID in url:
                        self.bulb_on = bool(command["value"])
                    if PLUG_ID in url:
                        self.plug_on = bool(command["value"])
            return _ok(True)
        if "/devices" in url:
            if self.list_response is not None:
                return self.list_response
            return _ok(DEVICE_LIST)
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("TUYA_CLOUD_ACCESS_ID", "TUYA_CLOUD_ACCESS_SECRET",
                "TUYA_CLOUD_UID", "TUYA_CLOUD_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


def make_driver(http: FakeHttp, **kwargs) -> TuyaCloudDriver:
    kwargs.setdefault("config", {})
    return TuyaCloudDriver(ACCESS_ID, ACCESS_SECRET, uid=UID,
                           http=http, **kwargs)


# -- discovery & modelling ---------------------------------------------------


def test_discover_builds_devices_with_local_consistent_mapping():
    driver = make_driver(FakeHttp())
    devices = driver.discover()
    by_id = {d.id: d for d in devices}
    assert set(by_id) == {f"tuyac-{BULB_ID}", f"tuyac-{PLUG_ID}"}

    bulb = by_id[f"tuyac-{BULB_ID}"]
    assert bulb.driver == "tuya_cloud"
    # Same property set as the local driver's bulb kind.
    assert set(bulb.properties) == set(KIND_PROPERTIES["bulb"])
    # Same conversions as the local driver: raw 505 -> 50%, raw 500
    # across 2700-6500 K -> 4600 K, HSV JSON -> #rrggbb.
    assert bulb.state == {"onoff": True, "brightness": 50,
                          "color_temp": 4600, "color": "#0000ff"}

    plug = by_id[f"tuyac-{PLUG_ID}"]
    assert set(plug.properties) == set(KIND_PROPERTIES["outlet"])
    # cur_power is 0.1 W units, add_ele is 0.01 kWh - as locally.
    assert plug.state == {"onoff": True, "power": 12.3, "energy": 4.56}


def test_uid_falls_back_to_token_uid():
    http = FakeHttp(token_uid="uid-from-token")
    driver = TuyaCloudDriver(ACCESS_ID, ACCESS_SECRET, http=http, config={})
    driver.discover()
    assert http.count("/v1.0/users/uid-from-token/devices") == 1


def test_credentials_from_config_with_env_ref(monkeypatch):
    monkeypatch.setenv("MY_TUYA_SECRET", ACCESS_SECRET)
    config = {"tuya_cloud": {"access_id": "cfg-id",
                             "access_secret": "env:MY_TUYA_SECRET",
                             "uid": UID}}
    http = FakeHttp()
    driver = TuyaCloudDriver(http=http, config=config)
    assert len(driver.discover()) == 2
    token_call = http.last_call("/v1.0/token")
    assert token_call["headers"]["client_id"] == "cfg-id"


# -- control -------------------------------------------------------------------


def test_set_property_sends_commands_with_scale_conversion():
    http = FakeHttp()
    driver = make_driver(http)
    driver.discover()

    driver.set_property(f"tuyac-{BULB_ID}", "brightness", 100)
    body = json.loads(http.last_call("/commands")["data"])
    assert body == {"commands": [{"code": "bright_value", "value": 1000}]}
    assert f"/v1.0/devices/{BULB_ID}/commands" in http.last_call("/commands")["url"]

    driver.set_property(f"tuyac-{BULB_ID}", "color_temp", 2700)
    body = json.loads(http.last_call("/commands")["data"])
    assert body == {"commands": [{"code": "temp_value", "value": 0}]}

    driver.set_property(f"tuyac-{PLUG_ID}", "onoff", False)
    body = json.loads(http.last_call("/commands")["data"])
    assert body == {"commands": [{"code": "switch", "value": False}]}

    result = driver.set_property(f"tuyac-{BULB_ID}", "onoff", False)
    body = json.loads(http.last_call("/commands")["data"])
    assert body == {"commands": [{"code": "switch_led", "value": False}]}
    assert result == {"onoff": False}


def test_readonly_property_rejected():
    driver = make_driver(FakeHttp())
    driver.discover()
    with pytest.raises(PropertyValidationError):
        driver.set_property(f"tuyac-{PLUG_ID}", "power", 10)


def test_actions_mirror_local_driver():
    http = FakeHttp()
    driver = make_driver(http)
    driver.discover()
    assert driver.call_action(f"tuyac-{PLUG_ID}", "turn_off", {}) == {"onoff": False}
    assert driver.call_action(f"tuyac-{PLUG_ID}", "toggle", {}) == {"onoff": True}


# -- token handling --------------------------------------------------------------


def test_token_cached_across_operations():
    http = FakeHttp()
    driver = make_driver(http)
    driver.discover()
    driver.get_state(f"tuyac-{BULB_ID}")
    driver.set_property(f"tuyac-{BULB_ID}", "onoff", True)
    assert http.count("/v1.0/token") == 1


def test_token_refreshed_when_expired(monkeypatch):
    http = FakeHttp(expire_time=7200)
    driver = make_driver(http)
    driver.discover()
    assert http.count("/v1.0/token") == 1
    # Move the driver's clock past the token's lifetime: the next
    # operation must fetch a fresh token instead of reusing the dead one.
    import omnibutler.drivers.tuya_cloud as tc_module

    real_now = tc_module.time.time()
    monkeypatch.setattr(tc_module.time, "time", lambda: real_now + 10000)
    driver.get_state(f"tuyac-{BULB_ID}")
    assert http.count("/v1.0/token") == 2


# -- errors ------------------------------------------------------------------------


def test_missing_credentials_plain_language():
    driver = TuyaCloudDriver(config={})
    with pytest.raises(DriverNotConfiguredError) as excinfo:
        driver.discover()
    message = str(excinfo.value)
    assert "TUYA_CLOUD_ACCESS_ID" in message
    assert "iot.tuya.com" in message


def test_token_refusal_is_auth_failed():
    http = FakeHttp()
    http.token_response = HttpResponse(
        200, json.dumps({"success": False, "code": 2001, "msg": "no"}))
    driver = make_driver(http)
    with pytest.raises(TuyaCloudError) as excinfo:
        driver.discover()
    assert excinfo.value.kind == "auth_failed"
    assert ACCESS_SECRET not in str(excinfo.value)


def test_business_failure_is_bad_response():
    http = FakeHttp()
    http.command_response = HttpResponse(
        200, json.dumps({"success": False, "code": 1100, "msg": "bad"}))
    driver = make_driver(http)
    driver.discover()
    with pytest.raises(TuyaCloudError) as excinfo:
        driver.set_property(f"tuyac-{PLUG_ID}", "onoff", True)
    assert excinfo.value.kind == "bad_response"
    assert "1100" in str(excinfo.value)


def test_http_500_is_network():
    http = FakeHttp()
    http.list_response = HttpResponse(500, "server error")
    driver = make_driver(http)
    with pytest.raises(TuyaCloudError) as excinfo:
        driver.discover()
    assert excinfo.value.kind == "network"


# -- secrecy -------------------------------------------------------------------------


def test_secret_and_token_never_leak(caplog):
    http = FakeHttp()
    driver = make_driver(http)
    with caplog.at_level(logging.DEBUG):
        devices = driver.discover()
        http.command_response = HttpResponse(
            200, json.dumps({"success": False, "code": 1100}))
        with pytest.raises(TuyaCloudError) as excinfo:
            driver.set_property(f"tuyac-{PLUG_ID}", "onoff", True)
    surfaces = [
        caplog.text,
        str(excinfo.value),
        repr(driver),
        json.dumps([d.to_dict() for d in devices]),
    ]
    for surface in surfaces:
        assert ACCESS_SECRET not in surface
        assert TOKEN not in surface


# -- runtime registration -------------------------------------------------------------


def test_runtime_registration_and_all_exclusion():
    from omnibutler.runtime import DRIVER_NAMES, _build_drivers

    assert "tuya_cloud" in DRIVER_NAMES
    built = _build_drivers("tuya_cloud")
    assert isinstance(built["tuya_cloud"], TuyaCloudDriver)
    # The cloud channel must be chosen explicitly, never via "all".
    assert "tuya_cloud" not in _build_drivers("all")

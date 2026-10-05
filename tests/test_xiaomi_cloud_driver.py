"""Tests for the Xiaomi cloud fallback driver (fake HTTP, no real cloud)."""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.parse

import pytest

from omnibutler.cloud_keys import (
    HttpResponse,
    _xiaomi_signature,
    _xiaomi_signed_nonce,
)
from omnibutler.core.errors import DriverNotConfiguredError, PropertyValidationError
from omnibutler.drivers.miio import _FAMILY_PROPERTIES
from omnibutler.drivers.xiaomi_cloud import XiaomiCloudDriver, XiaomiCloudError

USERNAME = "user@example.com"
PASSWORD = "p4ssw0rd-value"
SVC_TOKEN = "svc-token-xyz"
SSEC = "c2VjdXJpdHktbWF0ZXJpYWw="  # base64 of "security-material"

AC_DID = "1001"
PURIFIER_DID = "1002"
MYSTERY_DID = "1003"

DEVICE_LIST = [
    {"did": AC_DID, "name": "Living room AC",
     "model": "zhimi.aircondition.v1", "isOnline": True},
    {"did": PURIFIER_DID, "name": "Bedroom purifier",
     "model": "zhimi.airpurifier.ma4", "isOnline": True},
    {"did": MYSTERY_DID, "name": "Mystery gadget",
     "model": "unknown.gadget.v9", "isOnline": True},
]

# MIoT wire values by (siid, piid), using the miio driver's tables:
# AC mode 1 = "cool", AC fan level 3 = "high".
AC_VALUES = {(2, 1): True, (2, 2): 1, (2, 3): 26, (3, 1): 3}
PURIFIER_VALUES = {(2, 1): True, (2, 2): 0, (3, 1): 12, (4, 1): 80}


def _form(call: dict) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(call["data"].decode("utf-8"))
    return {key: values[0] for key, values in parsed.items()}


class FakeHttp:
    """Substring-routed fake of the cloud_keys HTTP contract."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.auth_response: HttpResponse | None = None
        self.prop_values = {
            AC_DID: dict(AC_VALUES),
            PURIFIER_DID: dict(PURIFIER_VALUES),
        }
        self.prop_401_remaining = 0
        self.prop_response: HttpResponse | None = None
        self.set_item_code = 0

    def count(self, part: str) -> int:
        return sum(1 for call in self.calls if part in call["url"])

    def last_call(self, part: str) -> dict:
        matches = [c for c in self.calls if part in c["url"]]
        assert matches, f"no call containing {part!r}"
        return matches[-1]

    def __call__(self, method, url, *, headers=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url,
                           "headers": headers or {}, "data": data})
        if "serviceLoginAuth2" in url:
            if self.auth_response is not None:
                return self.auth_response
            return HttpResponse(200, json.dumps({
                "code": 0, "ssecurity": SSEC,
                "location": "https://sts.example.com/sts?ticket=1",
                "userId": 123456,
            }))
        if "serviceLogin" in url:
            body = json.dumps({"_sign": "SIGN", "callback": "https://cb",
                               "qs": "qs-value"})
            return HttpResponse(200, f"&&&START&&&{body}")
        if "sts.example.com" in url:
            return HttpResponse(
                200, "ok",
                {"Set-Cookie": f"serviceToken={SVC_TOKEN}; Path=/"})
        if "/app/home/device_list" in url:
            return HttpResponse(200, json.dumps(
                {"code": 0, "result": {"list": DEVICE_LIST}}))
        if "/app/miotspec/prop" in url:
            if self.prop_401_remaining > 0:
                self.prop_401_remaining -= 1
                return HttpResponse(401, "")
            if self.prop_response is not None:
                return self.prop_response
            params = json.loads(_form(self.calls[-1])["data"])["params"]
            result = []
            for item in params:
                key = (item["siid"], item["piid"])
                store = self.prop_values.setdefault(item["did"], {})
                if "value" in item:
                    store[key] = item["value"]
                    result.append({"did": item["did"], "siid": item["siid"],
                                   "piid": item["piid"],
                                   "code": self.set_item_code})
                else:
                    result.append({"did": item["did"], "siid": item["siid"],
                                   "piid": item["piid"], "code": 0,
                                   "value": store.get(key)})
            return HttpResponse(200, json.dumps({"code": 0, "result": result}))
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("XIAOMI_CLOUD_USERNAME", "XIAOMI_CLOUD_PASSWORD",
                "XIAOMI_CLOUD_COUNTRY"):
        monkeypatch.delenv(var, raising=False)


def make_driver(http: FakeHttp, **kwargs) -> XiaomiCloudDriver:
    kwargs.setdefault("config", {})
    return XiaomiCloudDriver(USERNAME, PASSWORD, http=http, **kwargs)


# -- discovery & modelling ---------------------------------------------------


def test_discover_builds_devices_with_miio_mapping():
    driver = make_driver(FakeHttp())
    devices = driver.discover()
    by_id = {d.id: d for d in devices}
    # The unknown model has no miio mapping: skipped, not guessed at.
    assert set(by_id) == {f"xmc-{AC_DID}", f"xmc-{PURIFIER_DID}"}
    assert driver.skipped_unmapped == 1

    ac = by_id[f"xmc-{AC_DID}"]
    assert ac.driver == "xiaomi_cloud"
    assert ac.brand == "Xiaomi"
    # Same property set as the local miio driver's air_conditioner.
    assert set(ac.properties) == set(_FAMILY_PROPERTIES["air_conditioner"])
    # Same conversions as the local driver: mode wire 1 -> "cool",
    # fan level wire 3 -> 75 percent.
    assert ac.state == {"onoff": True, "mode": "cool",
                        "target_temperature": 26, "fan_speed": 75}

    purifier = by_id[f"xmc-{PURIFIER_DID}"]
    assert set(purifier.properties) == set(_FAMILY_PROPERTIES["air_purifier"])
    assert purifier.state == {"onoff": True, "mode": "auto", "pm25": 12,
                              "filter_life": 80}


def test_login_flow_and_signed_forms():
    http = FakeHttp()
    driver = make_driver(http)
    driver.discover()

    # The full cloud_keys login sequence ran, in order.
    urls = [call["url"] for call in http.calls]
    assert any("serviceLogin?" in u for u in urls)
    assert http.count("serviceLoginAuth2") == 1
    assert http.count("sts.example.com") == 1
    assert http.count("/app/home/device_list") == 1
    assert http.count("/app/miotspec/prop") >= 1

    # Auth2 carried the in-memory MD5 hash, never the raw password.
    auth_form = _form(http.last_call("serviceLoginAuth2"))
    assert auth_form["user"] == USERNAME
    assert auth_form["hash"] == hashlib.md5(
        PASSWORD.encode("utf-8")).hexdigest().upper()
    assert PASSWORD not in http.last_call("serviceLoginAuth2")["data"].decode()

    # Signed calls carry the full field set, and the signature checks
    # out against cloud_keys' own construction.
    for part, path in (("/app/home/device_list", "/app/home/device_list"),
                       ("/app/miotspec/prop", "/app/miotspec/prop")):
        form = _form(http.last_call(part))
        for field in ("signature", "_nonce", "ssecurity", "data"):
            assert field in form, f"{part} missing {field}"
        signed_nonce = _xiaomi_signed_nonce(form["ssecurity"], form["_nonce"])
        expected = _xiaomi_signature(
            path, signed_nonce, form["_nonce"], {"data": form["data"]})
        assert form["signature"] == expected

    # The session cookie rides on the miotspec calls.
    cookie = http.last_call("/app/miotspec/prop")["headers"]["Cookie"]
    assert f"serviceToken={SVC_TOKEN}" in cookie


def test_credentials_from_config_with_env_ref(monkeypatch):
    monkeypatch.setenv("MY_XIAOMI_PW", PASSWORD)
    config = {"xiaomi_cloud": {"username": USERNAME,
                               "password": "env:MY_XIAOMI_PW",
                               "country": "cn"}}
    http = FakeHttp()
    driver = XiaomiCloudDriver(http=http, config=config)
    assert len(driver.discover()) == 2
    assert http.count("api.io.mi.com") >= 1  # cn host


# -- control -------------------------------------------------------------------


def test_set_property_uses_miio_wire_conversion():
    http = FakeHttp()
    driver = make_driver(http)
    driver.discover()

    # Enum: "heat" is index 3 of the miio AC mode list.
    result = driver.set_property(f"xmc-{AC_DID}", "mode", "heat")
    params = json.loads(_form(http.last_call("/app/miotspec/prop"))["data"])
    assert params == {"params": [{"did": AC_DID, "siid": 2, "piid": 2,
                                  "value": 3}]}
    assert result == {"mode": "heat"}

    # Fan level: 50 percent is the "medium" level, wire index 2.
    driver.set_property(f"xmc-{AC_DID}", "fan_speed", 50)
    params = json.loads(_form(http.last_call("/app/miotspec/prop"))["data"])
    assert params["params"][0]["value"] == 2

    # Numbers pass through unchanged.
    driver.set_property(f"xmc-{AC_DID}", "target_temperature", 24)
    params = json.loads(_form(http.last_call("/app/miotspec/prop"))["data"])
    assert params["params"][0] == {"did": AC_DID, "siid": 2, "piid": 3,
                                   "value": 24}


def test_readonly_property_rejected():
    driver = make_driver(FakeHttp())
    driver.discover()
    with pytest.raises(PropertyValidationError):
        driver.set_property(f"xmc-{PURIFIER_DID}", "pm25", 10)


def test_actions_mirror_local_driver():
    http = FakeHttp()
    driver = make_driver(http)
    driver.discover()
    assert driver.call_action(f"xmc-{AC_DID}", "turn_off", {}) == {"onoff": False}
    assert driver.call_action(f"xmc-{AC_DID}", "toggle", {}) == {"onoff": True}


def test_set_refusal_is_bad_response_with_code():
    http = FakeHttp()
    http.set_item_code = -4005
    driver = make_driver(http)
    driver.discover()
    with pytest.raises(XiaomiCloudError) as excinfo:
        driver.set_property(f"xmc-{AC_DID}", "onoff", False)
    assert excinfo.value.kind == "bad_response"
    assert "-4005" in str(excinfo.value)


def test_business_code_nonzero_is_bad_response():
    http = FakeHttp()
    driver = make_driver(http)
    driver.discover()
    http.prop_response = HttpResponse(
        200, json.dumps({"code": 12345, "message": "nope"}))
    with pytest.raises(XiaomiCloudError) as excinfo:
        driver.get_state(f"xmc-{AC_DID}")
    assert excinfo.value.kind == "bad_response"
    assert "12345" in str(excinfo.value)


# -- session handling ------------------------------------------------------------


def test_session_expiry_relogs_in_once():
    http = FakeHttp()
    http.prop_401_remaining = 1
    driver = make_driver(http)
    devices = driver.discover()
    assert len(devices) == 2
    # One login for the flow, one more after the 401.
    assert http.count("serviceLoginAuth2") == 2


def test_session_cached_across_operations():
    http = FakeHttp()
    driver = make_driver(http)
    driver.discover()
    driver.get_state(f"xmc-{AC_DID}")
    driver.set_property(f"xmc-{AC_DID}", "onoff", True)
    assert http.count("serviceLoginAuth2") == 1


# -- errors ------------------------------------------------------------------------


def test_missing_credentials_plain_language():
    driver = XiaomiCloudDriver(config={})
    with pytest.raises(DriverNotConfiguredError) as excinfo:
        driver.discover()
    message = str(excinfo.value)
    assert "XIAOMI_CLOUD_USERNAME" in message
    assert "xiaomi_cloud" in message


def test_captcha_is_needs_human_verification():
    http = FakeHttp()
    http.auth_response = HttpResponse(200, json.dumps(
        {"code": 87001, "captchaUrl": "https://example.invalid/captcha"}))
    driver = make_driver(http)
    with pytest.raises(XiaomiCloudError) as excinfo:
        driver.discover()
    assert excinfo.value.kind == "needs_human_verification"
    assert PASSWORD not in str(excinfo.value)


def test_two_step_is_needs_human_verification():
    http = FakeHttp()
    http.auth_response = HttpResponse(200, json.dumps(
        {"notificationUrl": "https://example.invalid/verify"}))
    driver = make_driver(http)
    with pytest.raises(XiaomiCloudError) as excinfo:
        driver.discover()
    assert excinfo.value.kind == "needs_human_verification"


def test_wrong_password_is_auth_failed():
    http = FakeHttp()
    http.auth_response = HttpResponse(
        200, json.dumps({"code": 70016, "desc": "invalid credentials"}))
    driver = make_driver(http)
    with pytest.raises(XiaomiCloudError) as excinfo:
        driver.discover()
    assert excinfo.value.kind == "auth_failed"
    assert PASSWORD not in str(excinfo.value)


# -- secrecy -------------------------------------------------------------------------


def test_secret_and_tokens_never_leak(caplog):
    http = FakeHttp()
    driver = make_driver(http)
    with caplog.at_level(logging.DEBUG):
        devices = driver.discover()
        http.prop_response = HttpResponse(
            200, json.dumps({"code": 12345}))
        with pytest.raises(XiaomiCloudError) as excinfo:
            driver.get_state(f"xmc-{AC_DID}")
    surfaces = [
        caplog.text,
        str(excinfo.value),
        repr(driver),
        json.dumps([d.to_dict() for d in devices]),
    ]
    for surface in surfaces:
        assert PASSWORD not in surface
        assert SVC_TOKEN not in surface
        assert SSEC not in surface


# -- runtime registration -------------------------------------------------------------


def test_runtime_registration_and_all_exclusion():
    from omnibutler.runtime import DRIVER_NAMES, _build_drivers

    assert "xiaomi_cloud" in DRIVER_NAMES
    built = _build_drivers("xiaomi_cloud")
    assert isinstance(built["xiaomi_cloud"], XiaomiCloudDriver)
    # The cloud channel must be chosen explicitly, never via "all".
    assert "xiaomi_cloud" not in _build_drivers("all")

"""Tests for omnibutler.cloud_keys - all HTTP is faked, nothing real is sent."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import urllib.error
from types import SimpleNamespace

import pytest

from omnibutler.cloud_keys import (
    KIND_AUTH_FAILED,
    KIND_BAD_RESPONSE,
    KIND_NEEDS_HUMAN,
    KIND_NETWORK,
    CloudKeyError,
    HttpResponse,
    _tuya_sign,
    _tuya_string_to_sign,
    _xiaomi_signature,
    _xiaomi_signed_nonce,
    fetch_tuya_local_keys,
    fetch_xiaomi_tokens,
)

PASSWORD = "Sup3rSecret!pw"
SSECURITY = base64.b64encode(b"fake-ssecurity-bytes").decode()
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class FakeHttp:
    """Route by URL substring (first match wins); records every call."""

    def __init__(self) -> None:
        self.calls: list[SimpleNamespace] = []
        self.routes: list[tuple[str, object]] = []

    def add(self, needle: str, response) -> "FakeHttp":
        self.routes.append((needle, response))
        return self

    def __call__(self, method, url, *, headers=None, data=None, timeout=None):
        call = SimpleNamespace(method=method, url=url,
                               headers=headers or {}, data=data)
        self.calls.append(call)
        for needle, response in self.routes:
            if needle in url:
                return response(call) if callable(response) else response
        raise AssertionError(f"no fake route for {url}")


def _json(payload, status=200, headers=None) -> HttpResponse:
    return HttpResponse(status=status, text=json.dumps(payload),
                        headers=headers or {})


# ---------------------------------------------------------------------------
# Xiaomi
# ---------------------------------------------------------------------------


def _xiaomi_http(step2_payload) -> FakeHttp:
    http = FakeHttp()
    http.add("serviceLoginAuth2", _json(step2_payload))
    http.add("serviceLogin?", _json({
        "sid": "xiaomiio", "qs": "%3Fsid%3Dxiaomiio",
        "callback": "https://sts.api.io.mi.com/sts",
        "_sign": "FAKE_SIGN==",
    }))
    return http


def _xiaomi_happy_http() -> FakeHttp:
    http = _xiaomi_http({
        "code": 0, "ssecurity": SSECURITY, "userId": 123456,
        "cUserId": "789", "passToken": "pt",
        "location": "https://sts.api.io.mi.com/sts?d=abc",
    })
    http.add("sts.api.io.mi.com", HttpResponse(
        status=200, text="",
        headers={"Set-Cookie": "userId=123456; serviceToken=STS-TOKEN-XYZ; Path=/"}))
    http.add("device_list", _json({
        "code": 0,
        "result": {"list": [
            {"name": "客厅空调", "model": "xiaomi.airconditioner.m4",
             "did": "111", "mac": "aa:bb:cc:dd:ee:ff",
             "localip": "192.168.1.20",
             "token": "0123456789abcdef0123456789abcdef"},
            {"name": "共享的灯", "model": "yeelink.light", "did": "222",
             "mac": "aa:bb:cc:dd:ee:00", "localip": "192.168.1.21",
             "token": ""},
        ]},
    }))
    return http


def test_xiaomi_happy_path_returns_tokens():
    http = _xiaomi_happy_http()
    result = fetch_xiaomi_tokens("user@example.com", PASSWORD, http=http)
    assert result == [{
        "name": "客厅空调", "model": "xiaomi.airconditioner.m4",
        "did": "111", "mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.20",
        "token": "0123456789abcdef0123456789abcdef",
    }]
    # The wire saw the MD5 hash (Xiaomi's own scheme), never the raw password.
    auth_call = next(c for c in http.calls if "serviceLoginAuth2" in c.url)
    body = auth_call.data.decode()
    expected_hash = hashlib.md5(PASSWORD.encode()).hexdigest().upper()
    assert f"hash={expected_hash}" in body
    assert PASSWORD not in body
    # The device call carried the STS serviceToken as a cookie.
    api_call = next(c for c in http.calls if "device_list" in c.url)
    assert "serviceToken=STS-TOKEN-XYZ" in api_call.headers["Cookie"]


def test_xiaomi_wrong_password_is_auth_failed_without_leaking(caplog):
    http = _xiaomi_http({"code": 70016, "desc": "pwd error"})
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(CloudKeyError) as excinfo:
            fetch_xiaomi_tokens("user@example.com", PASSWORD, http=http)
    assert excinfo.value.kind == KIND_AUTH_FAILED
    assert PASSWORD not in str(excinfo.value)
    assert PASSWORD not in caplog.text


@pytest.mark.parametrize("payload", [
    {"code": 87001},
    {"code": 0, "captchaUrl": "https://account.xiaomi.com/captcha"},
])
def test_xiaomi_captcha_is_needs_human(payload):
    http = _xiaomi_http(payload)
    with pytest.raises(CloudKeyError) as excinfo:
        fetch_xiaomi_tokens("user@example.com", PASSWORD, http=http)
    assert excinfo.value.kind == KIND_NEEDS_HUMAN
    assert "browser" in str(excinfo.value)


def test_xiaomi_two_step_is_needs_human():
    http = _xiaomi_http({"code": 0, "notificationUrl": "https://x/2fa"})
    with pytest.raises(CloudKeyError) as excinfo:
        fetch_xiaomi_tokens("user@example.com", PASSWORD, http=http)
    assert excinfo.value.kind == KIND_NEEDS_HUMAN


def test_xiaomi_network_error_classified():
    def boom(method, url, *, headers=None, data=None, timeout=None):
        raise urllib.error.URLError("no route")

    with pytest.raises(CloudKeyError) as excinfo:
        fetch_xiaomi_tokens("user@example.com", PASSWORD, http=boom)
    assert excinfo.value.kind == KIND_NETWORK


def test_xiaomi_bad_json_is_bad_response():
    http = FakeHttp()
    http.add("serviceLoginAuth2", HttpResponse(200, "<html>oops</html>"))
    http.add("serviceLogin?", _json({"_sign": "s", "qs": "", "callback": ""}))
    with pytest.raises(CloudKeyError) as excinfo:
        fetch_xiaomi_tokens("user@example.com", PASSWORD, http=http)
    assert excinfo.value.kind == KIND_BAD_RESPONSE


def test_xiaomi_signing_helpers_are_deterministic():
    nonce = base64.b64encode(b"\x01" * 12).decode()
    signed = _xiaomi_signed_nonce(SSECURITY, nonce)
    expected = base64.b64encode(hashlib.sha256(
        base64.b64decode(SSECURITY) + base64.b64decode(nonce)).digest()).decode()
    assert signed == expected
    sig = _xiaomi_signature("/app/home/device_list", signed, nonce,
                            {"data": "{}"})
    expected_sig = base64.b64encode(hashlib.sha256(
        f"/app/home/device_list&{signed}&{nonce}&data={{}}".encode()
    ).digest()).decode()
    assert sig == expected_sig


# ---------------------------------------------------------------------------
# Tuya
# ---------------------------------------------------------------------------

ACCESS_ID = "tuyaAccessId123"
ACCESS_SECRET = "tuyaAccessSecret456"
ACCESS_TOKEN = "ACCESS-TOKEN-XYZ"


def test_tuya_sign_matches_independent_vector():
    """Fixed inputs; expected value recomputed here from the raw algorithm."""
    string_to_sign = _tuya_string_to_sign("GET", "/v1.0/token?grant_type=1")
    assert string_to_sign == (
        f"GET\n{EMPTY_SHA256}\n\n/v1.0/token?grant_type=1"
    )
    t = "1700000000000"
    source = ACCESS_ID + t + string_to_sign
    expected = hmac.new(ACCESS_SECRET.encode(), source.encode(),
                        hashlib.sha256).hexdigest().upper()
    assert _tuya_sign(ACCESS_ID, ACCESS_SECRET, None, t,
                      string_to_sign) == expected
    # With a business token the token joins the signed source.
    source2 = ACCESS_ID + ACCESS_TOKEN + t + string_to_sign
    expected2 = hmac.new(ACCESS_SECRET.encode(), source2.encode(),
                         hashlib.sha256).hexdigest().upper()
    assert _tuya_sign(ACCESS_ID, ACCESS_SECRET, ACCESS_TOKEN, t,
                      string_to_sign) == expected2


def _tuya_happy_http() -> FakeHttp:
    http = FakeHttp()
    http.add("/v1.0/token?grant_type=1", _json({
        "success": True,
        "result": {"access_token": ACCESS_TOKEN, "expire_time": 7200,
                   "uid": "uid-42"},
    }))
    http.add("/v1.0/users/uid-42/devices", _json({
        "success": True,
        "result": [
            {"id": "dev1", "name": "插座"},
            {"id": "dev2", "name": "台灯", "local_key": "from-list-key"},
            {"id": "dev3", "name": "网关"},
        ],
    }))
    http.add("/v1.0/devices/dev1", _json({
        "success": True,
        "result": {"id": "dev1", "name": "插座", "local_key": "key-one-123"},
    }))
    http.add("/v1.0/devices/dev2", _json({
        "success": True,
        "result": {"id": "dev2", "name": "台灯"},
    }))
    http.add("/v1.0/devices/dev3", _json({
        "success": True,
        "result": {"id": "dev3", "name": "网关"},
    }))
    return http


def test_tuya_chain_token_list_detail_local_keys(caplog):
    http = _tuya_happy_http()
    with caplog.at_level(logging.DEBUG):
        result = fetch_tuya_local_keys(ACCESS_ID, ACCESS_SECRET, "uid-42",
                                        http=http)
    assert result == [
        {"device_id": "dev1", "name": "插座", "local_key": "key-one-123"},
        {"device_id": "dev2", "name": "台灯", "local_key": "from-list-key"},
    ]
    # Token request was signed per the published algorithm.
    token_call = http.calls[0]
    assert token_call.url.endswith("/v1.0/token?grant_type=1")
    t = token_call.headers["t"]
    string_to_sign = f"GET\n{EMPTY_SHA256}\n\n/v1.0/token?grant_type=1"
    expected = hmac.new(ACCESS_SECRET.encode(),
                        (ACCESS_ID + t + string_to_sign).encode(),
                        hashlib.sha256).hexdigest().upper()
    assert token_call.headers["sign"] == expected
    assert token_call.headers["sign_method"] == "HMAC-SHA256"
    # Business calls carry the access token and their own sign.
    detail_call = next(c for c in http.calls if "/devices/dev1" in c.url)
    assert detail_call.headers["access_token"] == ACCESS_TOKEN
    # Neither the secret nor the token leaks into results or logs.
    assert ACCESS_SECRET not in repr(result)
    assert ACCESS_TOKEN not in repr(result)
    assert ACCESS_SECRET not in caplog.text
    assert ACCESS_TOKEN not in caplog.text


def test_tuya_bad_credentials_are_auth_failed_without_leaking():
    http = FakeHttp()
    http.add("/v1.0/token", _json({
        "success": False, "code": 1004, "msg": "sign invalid",
    }))
    with pytest.raises(CloudKeyError) as excinfo:
        fetch_tuya_local_keys(ACCESS_ID, ACCESS_SECRET, "uid-42", http=http)
    assert excinfo.value.kind == KIND_AUTH_FAILED
    assert ACCESS_SECRET not in str(excinfo.value)


def test_tuya_http_500_is_network():
    http = FakeHttp()
    http.add("/v1.0/token", HttpResponse(500, "server error"))
    with pytest.raises(CloudKeyError) as excinfo:
        fetch_tuya_local_keys(ACCESS_ID, ACCESS_SECRET, "uid-42", http=http)
    assert excinfo.value.kind == KIND_NETWORK


def test_tuya_missing_access_token_is_bad_response():
    http = FakeHttp()
    http.add("/v1.0/token", _json({"success": True, "result": {}}))
    with pytest.raises(CloudKeyError) as excinfo:
        fetch_tuya_local_keys(ACCESS_ID, ACCESS_SECRET, "uid-42", http=http)
    assert excinfo.value.kind == KIND_BAD_RESPONSE

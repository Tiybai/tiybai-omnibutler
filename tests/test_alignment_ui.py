"""v0.10 line-D alignment tests: approvals page languages + hardening,
CLI output hygiene, Z2M friendly_name validation, wire-format JSON and
cloud signature consistency for non-ASCII payloads.

The approvals fixtures mirror tests/test_approvals.py (same engine /
manager fixtures from conftest, same queue seeding).
"""

import hashlib
import hmac
import http.client
import json
import socket
import threading
import urllib.parse
from types import SimpleNamespace

import pytest

from omnibutler import approvals_web, cli
from omnibutler.cloud_keys import (
    HttpResponse,
    _xiaomi_signature,
    _xiaomi_signed_nonce,
)
from omnibutler.core.errors import DriverNotConfiguredError
from omnibutler.drivers.tuya_cloud import _TuyaCloudClient
from omnibutler.drivers.xiaomi_cloud import _XiaomiCloudClient
from omnibutler.drivers.zigbee2mqtt import Zigbee2MqttDriver

TOKEN = "<redacted>"


# -- approvals page: languages -------------------------------------------------

def _seed(queue, **overrides):
    kwargs = dict(
        device_id="garage_door", kind="call_action", name="open", params={},
        requested_by="scene:garage-arrival", scene="garage-arrival",
        risk="high",
    )
    kwargs.update(overrides)
    return queue.add(**kwargs)


@pytest.fixture()
def web(engine, manager):
    httpd = approvals_web.create_http_server(
        engine, engine.confirmations, manager,
        host="127.0.0.1", port=0, token=TOKEN,
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _request(port, method, path, *, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    text = response.read().decode("utf-8")
    result = (response.status, text, dict(response.getheaders()))
    conn.close()
    return result


def test_page_defaults_to_english_without_chinese_preference(web, engine):
    _seed(engine.confirmations)
    status, body, _ = _request(web, "GET", f"/?token={TOKEN}")
    assert status == 200
    assert 'lang="en"' in body
    assert "Approve &amp; run" in body and "Reject" in body
    assert "待确认" not in body and "批准执行" not in body


def test_page_chinese_when_accept_language_prefers_zh(web, engine):
    _seed(engine.confirmations)
    status, body, _ = _request(
        web, "GET", f"/?token={TOKEN}",
        headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    assert status == 200
    assert 'lang="zh-CN"' in body
    assert "批准执行" in body and "拒绝" in body


def test_explicit_lang_query_overrides_accept_language(web, engine):
    _seed(engine.confirmations)
    _, body_en, _ = _request(
        web, "GET", f"/?token={TOKEN}&lang=en",
        headers={"Accept-Language": "zh-CN"})
    assert 'lang="en"' in body_en and "批准执行" not in body_en
    _, body_zh, _ = _request(
        web, "GET", f"/?token={TOKEN}&lang=zh",
        headers={"Accept-Language": "en-US"})
    assert 'lang="zh-CN"' in body_zh and "批准执行" in body_zh


def test_decision_notice_follows_request_language(web, engine):
    item_en = _seed(engine.confirmations)
    status, body, _ = _request(
        web, "POST", f"/approve/{item_en.id}?token={TOKEN}&lang=en")
    assert status == 200
    assert "Approved and executed:" in body

    item_zh = _seed(engine.confirmations)
    status, body, _ = _request(
        web, "POST", f"/reject/{item_zh.id}?token={TOKEN}",
        headers={"Accept-Language": "zh-CN"})
    assert status == 200
    assert "已拒绝" in body


def test_page_css_supports_dark_mode_and_touch_targets(web, engine):
    _seed(engine.confirmations)
    _, body, _ = _request(web, "GET", f"/?token={TOKEN}")
    assert "prefers-color-scheme: dark" in body
    assert "overflow-wrap: anywhere" in body
    assert "min-height: 44px" in body


# -- approvals page: hardening ----------------------------------------------------

def test_non_loopback_host_header_is_rejected(web, engine):
    _seed(engine.confirmations)
    status, _, _ = _request(
        web, "GET", "/",
        headers={"Host": "evil.example.com",
                 "Authorization": f"Bearer {TOKEN}"})
    assert status == 403
    # The normal loopback Host still works.
    status, _, _ = _request(
        web, "GET", "/", headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 200


def test_oversized_post_body_gets_413_and_connection_close(web, engine):
    item = _seed(engine.confirmations)
    status, _, headers = _request(
        web, "POST", f"/approve/{item.id}?token={TOKEN}",
        body=b"x" * (approvals_web.MAX_BODY_BYTES + 1))
    assert status == 413
    assert headers.get("Connection") == "close"
    # Nothing was decided behind the oversized body.
    assert engine.confirmations.get(item.id).status == "pending"


def test_malformed_content_length_gets_400_and_connection_close(web, engine):
    _seed(engine.confirmations)
    with socket.create_connection(("127.0.0.1", web), timeout=5) as sock:
        sock.sendall(
            f"POST /approve/x?token={TOKEN} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\nContent-Length: abc\r\n\r\n".encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
    head = response.decode("latin-1")
    assert " 400 " in head.split("\r\n", 1)[0]
    assert "connection: close" in head.lower()


# -- CLI output hygiene -------------------------------------------------------------

def test_clean_strips_ansi_escapes_and_control_characters():
    assert cli._clean("客\x1b[1;31m厅\x07\x00灯") == "客厅灯"
    assert cli._clean("plain name") == "plain name"
    assert cli._clean("line\nbreak\ttab") == "linebreaktab"


def test_print_devices_sanitizes_names_and_rooms(capsys):
    device = SimpleNamespace(
        id="d1", name="客\x1b[31m厅灯", room="liv\x1bing",
        risk=SimpleNamespace(value="low"), online=True,
        properties={}, driver="mock")
    cli._print_devices([device])
    out = capsys.readouterr().out
    assert "\x1b" not in out and "[31m" not in out
    assert "客厅灯" in out and "living" in out


def test_main_reports_oserror_and_returns_1(monkeypatch, capsys):
    def boom(**_kwargs):
        raise OSError("state dir vanished")
    monkeypatch.setattr(cli, "build_runtime", boom)
    assert cli.main(["devices"]) == 1
    assert "error: state dir vanished" in capsys.readouterr().err


def test_main_does_not_swallow_keyboard_interrupt(monkeypatch):
    def boom(**_kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "build_runtime", boom)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["devices"])


# -- Zigbee2MQTT friendly_name validation --------------------------------------------

class _StubMqttClient:
    on_message = None
    connected = False


@pytest.mark.parametrize("friendly", [
    "a/b", "a+b", "a#b", " padded", "padded ", "  ",
])
def test_z2m_friendly_name_rejects_topic_breakers(friendly):
    with pytest.raises(DriverNotConfiguredError) as excinfo:
        Zigbee2MqttDriver(
            devices=[{"friendly_name": friendly, "kind": "light",
                      "name": "坏灯"}],
            client=_StubMqttClient())
    message = str(excinfo.value)
    assert "坏灯" in message  # the error names the offending device
    assert "MQTT" in message


def test_z2m_friendly_name_accepts_plain_names():
    driver = Zigbee2MqttDriver(
        devices=[{"friendly_name": "living_light", "kind": "light"}],
        client=_StubMqttClient())
    assert driver._friendly == {"living_light": "z2m-living_light"}


# -- wire JSON + cloud signature consistency (non-ASCII) ------------------------------

class _FakeTuyaHttp:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, *, headers=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url,
                           "headers": headers or {}, "data": data})
        if "/v1.0/token" in url:
            return HttpResponse(200, json.dumps({
                "success": True,
                "result": {"access_token": "tok-1", "expire_time": 7200,
                           "uid": "uid-1"},
            }))
        return HttpResponse(200, json.dumps({"success": True, "result": True}))


def test_tuya_sign_covers_the_exact_non_ascii_body_sent():
    http = _FakeTuyaHttp()
    client = _TuyaCloudClient("access-id-1", "access-secret-1", http,
                              "https://openapi.tuyacn.com")
    path = "/v1.0/devices/dev-1/commands"
    client.request("POST", path, what="test command", body_obj={
        "commands": [{"code": "switch_led", "value": True},
                     {"code": "nickname", "value": "客厅灯"}],
    })
    call = http.calls[-1]
    body = call["data"]
    assert isinstance(body, bytes)
    # The Chinese device nickname travels as raw UTF-8, not \uXXXX.
    assert "客厅灯".encode() in body
    assert "\\u5ba2" not in body.decode("utf-8")
    # Independently recompute the documented HMAC over the sent bytes:
    # the signature must cover exactly what went on the wire.
    content_hash = hashlib.sha256(body).hexdigest()
    string_to_sign = "\n".join(["POST", content_hash, "", path])
    source = "access-id-1" + "tok-1" + call["headers"]["t"] + string_to_sign
    expected = hmac.new(b"access-secret-1", source.encode("utf-8"),
                        hashlib.sha256).hexdigest().upper()
    assert call["headers"]["sign"] == expected


_SSEC = "AAECAwQFBgcICQoLDA0ODw=="  # base64 of bytes 0..15
_SVC_TOKEN = "svc-token-1"


class _FakeXiaomiHttp:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, *, headers=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url,
                           "headers": headers or {}, "data": data})
        if "serviceLoginAuth2" in url:
            return HttpResponse(200, json.dumps({
                "code": 0, "ssecurity": _SSEC,
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
                {"Set-Cookie": f"serviceToken={_SVC_TOKEN}; Path=/"})
        if "/app/miotspec/prop" in url:
            return HttpResponse(200, json.dumps({"code": 0, "result": []}))
        raise AssertionError(f"unexpected URL {url}")


def test_xiaomi_sign_covers_the_exact_non_ascii_data_sent():
    http = _FakeXiaomiHttp()
    client = _XiaomiCloudClient("user@example.com", "pw", "cn", http)
    path = "/app/miotspec/prop"
    client._signed_post(
        path,
        [{"did": "12345", "siid": 2, "piid": 1, "value": "客厅模式"}],
        what="test set")
    call = http.calls[-1]
    form = {key: values[0] for key, values in
            urllib.parse.parse_qs(call["data"].decode("utf-8")).items()}
    data_str = form["data"]
    # The Chinese value travels as raw UTF-8 inside the signed string.
    assert "客厅模式" in data_str
    assert "\\u" not in data_str
    # The signature in the form must be the signature of the exact
    # data string that was sent (recomputed from the form itself).
    signed_nonce = _xiaomi_signed_nonce(form["ssecurity"], form["_nonce"])
    expected = _xiaomi_signature(
        path, signed_nonce, form["_nonce"], {"data": data_str})
    assert form["signature"] == expected

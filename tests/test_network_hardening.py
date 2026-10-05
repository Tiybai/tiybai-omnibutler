"""Network-surface hardening (v0.10 line B2).

Covers the audit findings: the gateway event-type whitelist, the
per-batch ingest cap, HTTP body/Host/timeout hardening on both stdlib
servers (gateway + MCP HTTP transport), bounded response reads and
URL quoting in the cloud/HA drivers, the async webhook notifier plus
the Q-1 no-URL-in-failure-output check, scene-loader size/duplicate
guards, and backup archive permissions.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from omnibutler import gateway, notify_webhook
from omnibutler.core.events import EventBus
from omnibutler.core.streams import DataStream, StreamStore
from omnibutler.gateway import build_event, ingest_points, make_handler
from omnibutler.mcp_server import http_transport

TOKEN = "test-gateway-token"

STEPS = DataStream(id="phone-steps", kind="health.steps",
                   source="phone", unit="count")


@pytest.fixture()
def store(tmp_path):
    return StreamStore(path=tmp_path / "streams.jsonl")


@pytest.fixture()
def bus():
    return EventBus()


def _serve(handler_class):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


def _stop(httpd, thread):
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _raw_exchange(httpd, request: bytes, timeout: float = 5.0):
    """Send raw bytes; return (response_bytes, server_closed_cleanly)."""
    host, port = httpd.server_address[0], httpd.server_address[1]
    if host in ("0.0.0.0", "::"):  # connect via loopback, not the wildcard
        host = "127.0.0.1"
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(request)
        chunks = []
        closed = False
        while True:
            try:
                data = sock.recv(65536)
            except TimeoutError:
                break
            if not data:
                closed = True
                break
            chunks.append(data)
    return b"".join(chunks), closed


# -- gateway: event whitelist ----------------------------------------------------


@pytest.mark.parametrize("event_type", [
    "state_change", "schedule", "session_opened", "session_closed",
    "stream_appended", "device_removed", "anything_else",
])
def test_build_event_rejects_non_phone_types(event_type):
    with pytest.raises(ValueError, match="not accepted from the phone"):
        build_event({"type": event_type, "device_id": "x", "zone": "home",
                     "transition": "enter"})


def test_build_event_accepts_presence():
    event = build_event({"type": "presence", "person": "zhou",
                         "present": True})
    assert event.type == "presence"
    assert event.source == "phone"
    assert event.data == {"person": "zhou", "present": True}


def test_build_event_accepts_geofence():
    event = build_event({"type": "geofence", "zone": "home",
                         "transition": "enter"})
    assert event.type == "geofence"


def test_gateway_default_port_is_8767():
    # 8766 belongs to the approvals page; CLI and docs already use 8767.
    assert gateway.DEFAULT_PORT == 8767


# -- gateway: ingest batch cap -----------------------------------------------------


def _batch(count):
    return {
        "stream": STEPS.to_dict(),
        "points": [{"value": i} for i in range(count)],
    }


def test_ingest_accepts_exactly_1000_points(store):
    assert ingest_points(store, _batch(1000)) == 1000


def test_ingest_rejects_1001_points(store):
    with pytest.raises(ValueError, match="limit is 1000"):
        ingest_points(store, _batch(1001))
    # Whole batch refused: nothing was written.
    assert store.latest(STEPS.id) is None


# -- gateway: HTTP-level hardening -------------------------------------------------


@pytest.fixture()
def gateway_server(bus, store):
    httpd, thread = _serve(make_handler(bus, store, TOKEN))
    try:
        yield httpd
    finally:
        _stop(httpd, thread)


def test_event_endpoint_rejects_forged_state_change(gateway_server):
    body = json.dumps({"type": "state_change", "device_id": "lock",
                       "state": {"locked": False}}).encode()
    raw, _ = _raw_exchange(gateway_server, (
        b"POST /event HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Authorization: Bearer " + TOKEN.encode() + b"\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    ))
    assert b" 400 " in raw.split(b"\r\n", 1)[0]
    assert b"not accepted from the phone" in raw


def test_oversized_body_gets_413_and_connection_close(gateway_server):
    raw, closed = _raw_exchange(gateway_server, (
        b"POST /ingest HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Authorization: Bearer " + TOKEN.encode() + b"\r\n"
        b"Content-Length: 2097152\r\n\r\n"  # body never sent
    ))
    head = raw.split(b"\r\n\r\n", 1)[0]
    assert b" 413 " in head.split(b"\r\n", 1)[0]
    assert b"connection: close" in head.lower()
    assert closed  # server hung up instead of awaiting the phantom body


@pytest.mark.parametrize("bad_length", [b"abc", b"-5", b"1.5"])
def test_invalid_content_length_gets_400_and_close(gateway_server,
                                                   bad_length):
    raw, closed = _raw_exchange(gateway_server, (
        b"POST /ingest HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Authorization: Bearer " + TOKEN.encode() + b"\r\n"
        b"Content-Length: " + bad_length + b"\r\n\r\n"
    ))
    head = raw.split(b"\r\n\r\n", 1)[0]
    assert b" 400 " in head.split(b"\r\n", 1)[0]
    assert b"connection: close" in head.lower()
    assert closed


def test_gateway_handler_has_socket_timeout():
    assert make_handler(EventBus(), StreamStore(), TOKEN).timeout == 30.0


def test_stalled_connection_is_dropped_by_timeout(bus, store):
    class FastTimeoutHandler(make_handler(bus, store, TOKEN)):
        timeout = 0.3

    httpd, thread = _serve(FastTimeoutHandler)
    try:
        with socket.create_connection(httpd.server_address,
                                      timeout=3) as sock:
            sock.sendall(b"POST /ingest HTTP/1.1\r\nHost: x\r\n")
            started = time.monotonic()
            assert sock.recv(1024) == b""  # server gave up and closed
            assert time.monotonic() - started < 3
    finally:
        _stop(httpd, thread)


# -- Host header rules (both HTTP modules share the semantics) ---------------------


@pytest.mark.parametrize("module", [gateway, http_transport])
def test_host_header_rules(module):
    allowed = module._host_header_allowed
    # Loopback binds: no check (only local processes can connect).
    assert allowed("evil.example", "127.0.0.1")
    assert allowed("evil.example", "::1")
    # Non-loopback binds.
    assert allowed(None, "0.0.0.0")
    assert allowed("", "0.0.0.0")
    assert allowed("localhost", "0.0.0.0")
    assert allowed("localhost:8767", "0.0.0.0")
    assert allowed("192.168.1.20:8767", "0.0.0.0")   # an IP literal
    assert allowed("192.168.1.20", "192.168.1.20")   # the bind itself
    assert allowed("[::1]:8767", "0.0.0.0")
    assert allowed(socket.gethostname(), "0.0.0.0")  # the machine's name
    assert not allowed("evil.example", "0.0.0.0")
    assert not allowed("evil.example:8767", "192.168.1.20")
    assert not allowed("omnibutler.attacker.dev", "0.0.0.0")


def test_host_check_enforced_on_non_loopback_bind(bus, store):
    httpd = ThreadingHTTPServer(("0.0.0.0", 0),
                                make_handler(bus, store, TOKEN))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        raw, _ = _raw_exchange(httpd, (
            b"GET /health HTTP/1.1\r\nHost: evil.example\r\n\r\n"))
        assert b" 403 " in raw.split(b"\r\n", 1)[0]
        raw, _ = _raw_exchange(httpd, (
            b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n"))
        assert b" 200 " in raw.split(b"\r\n", 1)[0]
    finally:
        _stop(httpd, thread)


# -- MCP HTTP transport hardening ---------------------------------------------------


class _StubMcpServer:
    def handle(self, message):
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {}}


@pytest.fixture()
def mcp_http_server():
    handler = http_transport.make_handler(_StubMcpServer(), "tok")
    httpd, thread = _serve(handler)
    try:
        yield httpd
    finally:
        _stop(httpd, thread)


def test_mcp_http_handler_has_socket_timeout():
    handler = http_transport.make_handler(_StubMcpServer(), "tok")
    assert handler.timeout == 30.0


def test_mcp_http_oversized_body_closes_connection(mcp_http_server):
    raw, closed = _raw_exchange(mcp_http_server, (
        b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Authorization: Bearer tok\r\nContent-Length: 2097152\r\n\r\n"
    ))
    head = raw.split(b"\r\n\r\n", 1)[0]
    assert b" 413 " in head.split(b"\r\n", 1)[0]
    assert b"connection: close" in head.lower()
    assert closed


def test_mcp_http_negative_content_length_rejected(mcp_http_server):
    raw, closed = _raw_exchange(mcp_http_server, (
        b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Authorization: Bearer tok\r\nContent-Length: -1\r\n\r\n"
    ))
    head = raw.split(b"\r\n\r\n", 1)[0]
    assert b" 400 " in head.split(b"\r\n", 1)[0]
    assert b"connection: close" in head.lower()
    assert closed


# -- bounded response reads ----------------------------------------------------------


class _FakeHttpResponse:
    """urlopen stand-in response with an honest read(size)."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status
        self.headers = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        if size is None or size < 0:
            return self._body
        return self._body[:size]


def _patch_urlopen(monkeypatch, response):
    def fake_urlopen(request, timeout=None):
        return response
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def test_cloud_keys_oversized_response_is_bad_response(monkeypatch):
    from omnibutler import cloud_keys

    big = b"x" * (cloud_keys.MAX_RESPONSE_BYTES + 1)
    _patch_urlopen(monkeypatch, _FakeHttpResponse(big))
    with pytest.raises(cloud_keys.CloudKeyError) as excinfo:
        cloud_keys._urllib_http("GET", "https://vendor.invalid/devices")
    assert excinfo.value.kind == cloud_keys.KIND_BAD_RESPONSE


def test_cloud_keys_normal_response_still_read(monkeypatch):
    from omnibutler import cloud_keys

    _patch_urlopen(monkeypatch, _FakeHttpResponse(b'{"ok": true}'))
    response = cloud_keys._urllib_http("GET", "https://vendor.invalid/x")
    assert response.status == 200
    assert response.text == '{"ok": true}'


def test_tuya_cloud_oversized_response_is_bad_response(monkeypatch):
    from omnibutler.drivers import tuya_cloud

    big = b"x" * (tuya_cloud.MAX_RESPONSE_BYTES + 1)
    _patch_urlopen(monkeypatch, _FakeHttpResponse(big))
    with pytest.raises(tuya_cloud.TuyaCloudError) as excinfo:
        tuya_cloud._urllib_http("GET", "https://vendor.invalid/devices")
    assert excinfo.value.kind == tuya_cloud.KIND_BAD_RESPONSE


def test_homeassistant_oversized_response_refused(monkeypatch):
    from omnibutler.drivers.homeassistant import (
        HomeAssistantDriver,
        HomeAssistantError,
    )

    driver = HomeAssistantDriver(base_url="http://ha.local:8123",
                                 token="tok")
    big = b"x" * (HomeAssistantDriver.MAX_RESPONSE_BYTES + 1)
    _patch_urlopen(monkeypatch, _FakeHttpResponse(big))
    with pytest.raises(HomeAssistantError, match="MiB"):
        driver.get_state("light.kitchen")


# -- URL quoting ---------------------------------------------------------------------


class _CapturingUrlopen:
    def __init__(self, payload):
        self.urls = []
        self._payload = payload

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
        body = json.dumps(self._payload).encode()
        return _FakeHttpResponse(body)


def test_homeassistant_quotes_entity_and_service_paths(monkeypatch):
    from omnibutler.drivers.homeassistant import HomeAssistantDriver

    driver = HomeAssistantDriver(base_url="http://ha.local:8123",
                                 token="tok")
    capture = _CapturingUrlopen({
        "entity_id": "light.kitchen lamp", "state": "on",
        "attributes": {}})
    monkeypatch.setattr(urllib.request, "urlopen", capture)
    driver.get_state("light.kitchen lamp")
    assert capture.urls[-1].endswith("/api/states/light.kitchen%20lamp")

    capture2 = _CapturingUrlopen({})
    monkeypatch.setattr(urllib.request, "urlopen", capture2)
    driver.call_action("weird/domain.light", "turn_on", {})
    assert "/api/services/weird%2Fdomain/turn_on" in capture2.urls[-1]


class _StubTuyaClient:
    def __init__(self, uid):
        self.token_uid = uid
        self.paths = []

    def access_token(self):
        return "token"

    def request(self, method, path, what=None, body_obj=None):
        self.paths.append(path)
        return []  # the client unwraps the envelope before this layer


def test_tuya_cloud_quotes_ids_in_paths():
    from omnibutler.drivers.tuya_cloud import TuyaCloudDriver

    driver = TuyaCloudDriver.__new__(TuyaCloudDriver)
    client = _StubTuyaClient("u id/1")
    driver._list_cloud_devices(client)
    assert client.paths == ["/v1.0/users/u%20id%2F1/devices"]

    client2 = _StubTuyaClient("uid")
    driver._device_status(client2, "dev/ice 1")
    assert client2.paths == ["/v1.0/devices/dev%2Fice%201/status"]


# -- webhook: async notifier, daemon wiring, Q-1 --------------------------------------


class _RecordingSyncNotifier:
    def __init__(self):
        self.items = []
        self.done = threading.Event()

    def notify(self, item):
        self.items.append(item)
        self.done.set()
        return True


def test_async_notifier_delivers_and_flushes_on_close():
    inner = _RecordingSyncNotifier()
    notifier = notify_webhook.AsyncNotifier(inner)
    assert notifier.notify("one") is True
    assert inner.done.wait(timeout=5)
    notifier.notify("two")
    notifier.close(timeout=5)
    assert inner.items == ["one", "two"]
    assert notifier.notify("three") is False  # closed: dropped, not raised


def test_async_notifier_drops_when_queue_full():
    gate = threading.Event()

    class BlockingNotifier:
        def notify(self, item):
            gate.wait(timeout=10)
            return True

    notifier = notify_webhook.AsyncNotifier(BlockingNotifier(),
                                             max_pending=1)
    assert notifier.notify("first") is True   # worker picks it up, blocks
    deadline = time.monotonic() + 5
    while notifier._queue.qsize() != 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert notifier.notify("second") is True  # fills the single slot
    assert notifier.notify("third") is False  # full: dropped with a note
    gate.set()
    notifier.close(timeout=5)


def test_daemon_wraps_self_resolved_notifier_and_flushes(
        tmp_path, monkeypatch):
    from omnibutler.core.audit import AuditLog
    from omnibutler.core.confirmations import ConfirmationQueue
    from omnibutler.core.manager import DeviceManager
    from omnibutler.daemon import Daemon
    from omnibutler.drivers.mock import MockDriver
    from omnibutler.runtime import Runtime
    from omnibutler.scenes.engine import SceneEngine

    calls = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout=None):
        calls.append(request.full_url)
        return _Response()

    real = notify_webhook.WebhookNotifier("https://ntfy.example/t",
                                          opener=opener)
    monkeypatch.setattr(notify_webhook, "make_webhook_notifier",
                        lambda *a, **k: real)

    audit = AuditLog(path=tmp_path / "audit.jsonl")
    driver = MockDriver()
    manager = DeviceManager(drivers={driver.name: driver}, audit=audit)
    confirmations = ConfirmationQueue(path=tmp_path / "confirmations.json")
    engine = SceneEngine(manager, confirmations)
    runtime = Runtime(manager=manager, engine=engine,
                      confirmations=confirmations, bus=manager.bus,
                      driver_name=driver.name)
    daemon = Daemon(runtime)
    daemon._start_webhook_extra()
    assert isinstance(daemon.webhook_notifier,
                      notify_webhook.AsyncNotifier)
    confirmations.add("garage_door", "call_action", "open",
                      requested_by="scene:test", scene="test",
                      risk="high")
    daemon._maybe_notify_approvals()
    assert daemon.stats["webhook_notifications"] == 1
    daemon._stop_extras()  # bounded flush delivers the queued POST
    assert calls == ["https://ntfy.example/t"]


# Q-1: webhook failure output must never carry the full URL (it can
# embed a credential: an ntfy topic or a Bark key).

SECRET_PATH = "s3cr3t-topic-do-not-print"


def test_q1_unreachable_webhook_output_hides_url(capsys):
    url = f"http://127.0.0.1:1/{SECRET_PATH}"
    assert notify_webhook.send_webhook(url, {"x": 1}, timeout=2) is False
    err = capsys.readouterr().err
    assert "webhook" in err
    assert url not in err
    assert SECRET_PATH not in err


def test_q1_leaking_exception_text_is_redacted(capsys):
    url = f"https://ntfy.example/{SECRET_PATH}"

    def leaking_opener(request, timeout=None):
        raise RuntimeError(f"boom while POSTing to {request.full_url}")

    assert notify_webhook.send_webhook(url, {"x": 1},
                                       opener=leaking_opener) is False
    err = capsys.readouterr().err
    assert url not in err
    assert SECRET_PATH not in err
    assert "<webhook URL>" in err


# -- scene loader guards ----------------------------------------------------------


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_scene_loader_rejects_duplicate_keys(tmp_path):
    from omnibutler.scenes.loader import (
        SceneValidationError,
        load_scene_file,
    )

    path = _write(tmp_path, "dup.yaml", (
        "name: dup-scene\n"
        "name: other-name\n"
        "trigger: {type: manual}\n"
        "actions: []\n"
    ))
    with pytest.raises(SceneValidationError, match="duplicate key 'name'"):
        load_scene_file(path)


def test_scene_loader_rejects_nested_duplicate_keys(tmp_path):
    from omnibutler.scenes.loader import (
        SceneValidationError,
        load_scene_file,
    )

    path = _write(tmp_path, "nested.yaml", (
        "name: nested-dup\n"
        "trigger:\n"
        "  type: state\n"
        "  type: manual\n"
        "actions: []\n"
    ))
    with pytest.raises(SceneValidationError, match="duplicate key 'type'"):
        load_scene_file(path)


def test_scene_loader_rejects_oversize_file(tmp_path):
    from omnibutler.scenes.loader import (
        MAX_SCENE_FILE_BYTES,
        SceneValidationError,
        load_scene_file,
    )

    padding = "# pad\n" * (MAX_SCENE_FILE_BYTES // 6 + 100)
    path = _write(tmp_path, "big.yaml",
                  "name: big\n" + padding)
    assert path.stat().st_size > MAX_SCENE_FILE_BYTES
    with pytest.raises(SceneValidationError, match="limit is"):
        load_scene_file(path)


def test_scene_loader_still_loads_valid_scene(tmp_path):
    from omnibutler.scenes.loader import load_scene_file

    path = _write(tmp_path, "ok.yaml", (
        "name: ok-scene\n"
        "trigger: {type: geofence, zone: home, transition: enter}\n"
        "actions:\n"
        "  - device: living_ac\n"
        "    set: {onoff: true}\n"
    ))
    scene = load_scene_file(path)
    assert scene.name == "ok-scene"


# -- backup permissions -------------------------------------------------------------


def test_backup_archive_is_owner_only(tmp_path):
    from omnibutler.backup import create_backup

    config = tmp_path / "config.json"
    config.write_text('{"version": 1}', encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    (state / "audit.jsonl").write_text("{}\n", encoding="utf-8")
    dest = tmp_path / "backup.tar.gz"
    create_backup(dest, config, state)
    assert dest.exists()
    if os.name == "posix":
        assert os.stat(dest).st_mode & 0o777 == 0o600

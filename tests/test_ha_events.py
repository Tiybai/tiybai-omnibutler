"""HA WebSocket event subscription: near-real-time state for the daemon.

No network anywhere: a fake ``websockets`` module is installed into
``sys.modules`` (the same trick the Matter driver tests use) whose
connections are wired to a scripted fake HA server, and the REST side
is a scripted ``urlopen``. The subscription runs on the driver's own
thread, so tests poll with ``wait_until`` instead of sleeping fixed
amounts.
"""

import asyncio
import json
import sys
import threading
import time
import types
import urllib.request

import pytest

from omnibutler import runtime as runtime_module
from omnibutler.config import ha_settings
from omnibutler.core.audit import AuditLog
from omnibutler.core.confirmations import ConfirmationQueue
from omnibutler.core.manager import DeviceManager
from omnibutler.daemon import Daemon
from omnibutler.drivers.homeassistant import HomeAssistantDriver
from omnibutler.runtime import Runtime
from omnibutler.scenes.engine import SceneEngine

HA_URL = "http://ha.local:8123"
TOKEN = "tok"


# -- fake HA WebSocket server -------------------------------------------------

class FakeHAConnection:
    """One client connection: greets with auth_required, then answers
    through the server's script; ``recv`` waits for queued messages."""

    def __init__(self, server: "FakeHAServer") -> None:
        self._server = server
        self._incoming: list[str] = [
            json.dumps({"type": "auth_required", "ha_version": "2026.10.0"})
        ]
        self._dropped = False
        self.sent: list[dict] = []

    async def __aenter__(self) -> "FakeHAConnection":
        if self._server.fail_connect:
            raise ConnectionRefusedError("fake: connection refused")
        self._server.connects += 1
        self._server.connections.append(self)
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        self._server.handle(self, message)

    async def recv(self) -> str:
        while True:
            if self._incoming:
                return self._incoming.pop(0)
            if self._dropped:
                raise ConnectionError("fake: connection dropped")
            await asyncio.sleep(0.002)

    def drop(self) -> None:
        self._dropped = True

    def push(self, message: dict) -> None:
        self._incoming.append(json.dumps(message))


class FakeHAServer:
    """The HA side of the WebSocket wire protocol."""

    def __init__(self) -> None:
        self.connects = 0
        self.connections: list[FakeHAConnection] = []
        self.subscribed: list[FakeHAConnection] = []
        self.urls: list[str] = []
        self.fail_connect = False
        self.auth_ok = True

    def handle(self, conn: FakeHAConnection, message: dict) -> None:
        kind = message.get("type")
        if kind == "auth":
            if self.auth_ok:
                conn.push({"type": "auth_ok", "ha_version": "2026.10.0"})
            else:
                conn.push({"type": "auth_invalid",
                           "message": "Invalid access token or password."})
        elif kind == "subscribe_events":
            self.subscribed.append(conn)
            conn.push({"id": message.get("id"), "type": "result",
                       "success": True, "result": None})

    def push_raw(self, message: dict) -> None:
        for conn in list(self.subscribed):
            conn.push(message)

    def push_state(self, entity_id: str, new_state) -> None:
        self.push_raw({
            "id": 1, "type": "event",
            "event": {
                "event_type": "state_changed",
                "data": {"entity_id": entity_id, "old_state": None,
                         "new_state": new_state},
                "origin": "LOCAL",
                "time_fired": "2026-10-05T12:00:00+00:00",
                "context": {"id": "fake"},
            },
        })


@pytest.fixture()
def fake_ha(monkeypatch):
    """Install the fake websockets module; shield tests from host env."""
    monkeypatch.delenv("OMNIBUTLER_HA_NO_SUBSCRIBE", raising=False)
    server = FakeHAServer()
    module = types.ModuleType("websockets")

    def connect(url):
        server.urls.append(url)
        return FakeHAConnection(server)

    module.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", module)
    return server


def _driver(**kwargs) -> HomeAssistantDriver:
    kwargs.setdefault("base_url", HA_URL)
    kwargs.setdefault("token", TOKEN)
    kwargs.setdefault("reconnect_initial_delay", 0.01)
    kwargs.setdefault("reconnect_max_delay", 0.05)
    return HomeAssistantDriver(**kwargs)


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _light_item(state: str = "on", brightness: int = 128) -> dict:
    return {"entity_id": "light.kitchen", "state": state,
            "attributes": {"friendly_name": "Kitchen light",
                           "brightness": brightness}}


# -- URL + handshake ------------------------------------------------------------

def test_websocket_url_derivation():
    assert _driver().websocket_url == "ws://ha.local:8123/api/websocket"
    secure = HomeAssistantDriver(base_url="https://ha.example:8443", token="t")
    assert secure.websocket_url == "wss://ha.example:8443/api/websocket"


def test_auth_and_subscribe_flow(fake_ha):
    driver = _driver()
    received = []
    assert driver.start_event_subscription(
        lambda device_id, state: received.append((device_id, state))) is True
    try:
        assert wait_until(lambda: len(fake_ha.subscribed) == 1)
        assert fake_ha.urls == ["ws://ha.local:8123/api/websocket"]
        conn = fake_ha.connections[0]
        assert conn.sent[0] == {"type": "auth", "access_token": TOKEN}
        assert conn.sent[1]["type"] == "subscribe_events"
        assert conn.sent[1]["event_type"] == "state_changed"
        assert wait_until(lambda: driver.subscription_active)
    finally:
        driver.stop_event_subscription()
    assert driver.subscription_active is False


# -- event -> canonical state -----------------------------------------------------

def test_state_changed_becomes_canonical_state(fake_ha):
    driver = _driver()
    received = []
    assert driver.start_event_subscription(
        lambda device_id, state: received.append((device_id, state)))
    try:
        assert wait_until(lambda: len(fake_ha.subscribed) == 1)
        fake_ha.push_state("light.kitchen", _light_item("on", 128))
        assert wait_until(lambda: len(received) == 1)
        device_id, state = received[0]
        assert device_id == "light.kitchen"
        assert state["onoff"] is True
        assert state["brightness"] == 50  # 128/255 as a percentage
        # An unsupported domain and an entity removal change nothing.
        fake_ha.push_state("person.zhou",
                           {"entity_id": "person.zhou", "state": "home",
                            "attributes": {}})
        fake_ha.push_state("light.kitchen", None)
        time.sleep(0.15)
        assert len(received) == 1
    finally:
        driver.stop_event_subscription()


def test_prefix_filter_applies_to_events(fake_ha):
    driver = _driver(entity_prefixes="light.")
    received = []
    assert driver.start_event_subscription(
        lambda device_id, state: received.append(device_id))
    try:
        assert wait_until(lambda: len(fake_ha.subscribed) == 1)
        fake_ha.push_state("climate.bedroom",
                           {"entity_id": "climate.bedroom", "state": "cool",
                            "attributes": {"hvac_mode": "cool"}})
        fake_ha.push_state("light.kitchen", _light_item("off"))
        assert wait_until(lambda: received == ["light.kitchen"])
    finally:
        driver.stop_event_subscription()


def test_callback_error_does_not_kill_the_feed(fake_ha):
    driver = _driver()
    calls = []

    def flaky(device_id, state):
        calls.append(device_id)
        if len(calls) == 1:
            raise RuntimeError("consumer exploded")

    assert driver.start_event_subscription(flaky)
    try:
        assert wait_until(lambda: len(fake_ha.subscribed) == 1)
        fake_ha.push_state("light.kitchen", _light_item("on"))
        fake_ha.push_state("light.kitchen", _light_item("off"))
        assert wait_until(lambda: len(calls) == 2)
        assert driver.subscription_active
    finally:
        driver.stop_event_subscription()


# -- reconnect discipline ---------------------------------------------------------

def test_reconnect_after_drop(fake_ha):
    driver = _driver()
    received = []
    assert driver.start_event_subscription(
        lambda device_id, state: received.append(device_id))
    try:
        assert wait_until(lambda: len(fake_ha.subscribed) == 1)
        fake_ha.connections[0].drop()
        assert wait_until(lambda: fake_ha.connects == 2)
        assert wait_until(lambda: len(fake_ha.subscribed) == 2)
        # The new session re-authenticated before re-subscribing.
        assert fake_ha.connections[1].sent[0]["type"] == "auth"
        fake_ha.push_state("light.kitchen", _light_item("on"))
        assert wait_until(lambda: received == ["light.kitchen"])
    finally:
        driver.stop_event_subscription()


def test_auth_invalid_stops_without_reconnect(fake_ha):
    fake_ha.auth_ok = False
    driver = _driver()
    assert driver.start_event_subscription(lambda device_id, state: None)
    try:
        assert wait_until(lambda: fake_ha.connects == 1)
        # Many backoff cycles pass; a refused token must not retry.
        time.sleep(0.4)
        assert fake_ha.connects == 1
        assert driver.subscription_active is False
    finally:
        driver.stop_event_subscription()


# -- graceful absence ---------------------------------------------------------------

class _FakeHttpResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self._body


def test_missing_websockets_rest_unaffected(monkeypatch):
    monkeypatch.delenv("OMNIBUTLER_HA_NO_SUBSCRIBE", raising=False)
    monkeypatch.setitem(sys.modules, "websockets", None)  # import fails
    driver = _driver()
    assert driver.events_available is False
    assert driver.start_event_subscription(lambda d, s: None) is False

    def urlopen(request, timeout=None):
        url = request.full_url
        if url.endswith("/api/states"):
            return _FakeHttpResponse([_light_item("off")])
        assert url.endswith("/api/states/light.kitchen")
        return _FakeHttpResponse(_light_item("off"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    devices = driver.list_devices()  # the REST path never needed the library
    assert [d.id for d in devices] == ["light.kitchen"]
    assert driver.get_state("light.kitchen")["onoff"] is False


def test_disabled_by_argument_and_by_env(fake_ha, monkeypatch):
    driver = _driver(subscribe_events=False)
    assert driver.start_event_subscription(lambda d, s: None) is False

    monkeypatch.setenv("OMNIBUTLER_HA_NO_SUBSCRIBE", "1")
    killed = _driver()
    assert killed.subscribe_events is False
    assert killed.start_event_subscription(lambda d, s: None) is False

    time.sleep(0.1)
    assert fake_ha.connects == 0  # neither driver ever opened a socket


# -- config plumbing ------------------------------------------------------------------

def test_config_subscribe_events_plumbing(monkeypatch):
    assert ha_settings({"ha": {"subscribe_events": False}},
                       environ={})["subscribe_events"] is False
    assert ha_settings({"ha": {"subscribe_events": True}},
                       environ={})["subscribe_events"] is True
    # Unset or non-boolean values leave the driver's own default in charge.
    assert ha_settings({"ha": {}}, environ={})["subscribe_events"] is None
    assert ha_settings({"ha": {"subscribe_events": "no"}},
                       environ={})["subscribe_events"] is None

    config = {"ha": {"url": HA_URL, "token": "abc",
                     "subscribe_events": False}}
    monkeypatch.setattr(runtime_module, "load_config", lambda: config)
    built = runtime_module._build_drivers("homeassistant")
    assert built["homeassistant"].subscribe_events is False


# -- daemon wiring: push fires once, the poll then dedupes -----------------------------

class _FakeHARest:
    """Scripted REST side: serves the items in ``states`` by URL."""

    def __init__(self, states: dict):
        self.states = states

    def urlopen(self, request, timeout=None):
        url = request.full_url
        if url.endswith("/api/states"):
            return _FakeHttpResponse(list(self.states.values()))
        entity_id = url.rsplit("/api/states/", 1)[-1]
        return _FakeHttpResponse(self.states[entity_id])


def test_daemon_push_fires_once_and_poll_dedupes(fake_ha, monkeypatch, tmp_path):
    rest = _FakeHARest({"light.kitchen": _light_item("off")})
    monkeypatch.setattr(urllib.request, "urlopen", rest.urlopen)

    driver = _driver()
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    manager = DeviceManager(drivers={"homeassistant": driver}, audit=audit)
    confirmations = ConfirmationQueue(path=tmp_path / "confirmations.json")
    engine = SceneEngine(manager, confirmations)
    runtime = Runtime(manager=manager, engine=engine,
                      confirmations=confirmations, bus=manager.bus,
                      driver_name="homeassistant")

    events = []
    original = engine.handle_event

    def spy(event):
        events.append(event)
        return original(event)

    engine.handle_event = spy
    daemon = Daemon(runtime, poll_interval=30, tick_seconds=0.02,
                    sleep_fn=time.sleep)
    thread = threading.Thread(target=daemon.run, daemon=True)
    thread.start()
    try:
        # Baseline from the first poll, feed live: only then push, so the
        # ordering this test asserts is deterministic.
        assert wait_until(lambda: "light.kitchen" in daemon._snapshot)
        assert wait_until(lambda: driver.subscription_active)
        on_item = _light_item("on")
        rest.states["light.kitchen"] = on_item
        fake_ha.push_state("light.kitchen", on_item)

        def state_changes():
            return [e for e in events if e.type == "state_change"]

        assert wait_until(lambda: len(state_changes()) == 1)
        event = state_changes()[0]
        assert event.data["device"] == "light.kitchen"
        assert event.data["property"] == "onoff"
        assert event.data["value"] is True
        assert event.data["old_value"] is False
        assert daemon.stats["state_changes"] == 1

        # The next poll reads the very same state REST-side: the shared
        # snapshot diff must stay silent - no second firing.
        daemon._poll_driver("homeassistant")
        assert daemon.stats["state_changes"] == 1
        assert len(state_changes()) == 1
    finally:
        daemon.stop()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert driver.subscription_active is False

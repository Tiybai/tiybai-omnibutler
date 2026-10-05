"""Phone gateway embedded in the daemon (tob run --gateway).

One process must own both the scene engine and the gateway: the gateway
publishes geofence events onto the runtime's bus, and the engine reacts
on that same bus. These tests run a real Daemon (mock driver) in a
thread with the gateway extra enabled and talk to it over a real socket.
"""

import http.client
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from omnibutler.core.audit import AuditLog
from omnibutler.core.confirmations import ConfirmationQueue
from omnibutler.core.manager import DeviceManager
from omnibutler.core.streams import StreamStore
from omnibutler.daemon import Daemon
from omnibutler.drivers.mock import MockDriver
from omnibutler.gateway import TOKEN_ENV_VAR
from omnibutler.runtime import Runtime
from omnibutler.scenes.engine import SceneEngine
from omnibutler.scenes.loader import load_scenes_dir

SCENES_DIR = Path(__file__).resolve().parent.parent / "examples" / "scenes"
TOKEN = "daemon-gateway-test-token"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _runtime(tmp_path):
    """A mock runtime whose engine is attached to its bus (like
    build_runtime), with the stream store and audit log in tmp_path."""
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    driver = MockDriver()
    manager = DeviceManager(drivers={driver.name: driver}, audit=audit)
    confirmations = ConfirmationQueue(path=tmp_path / "confirmations.json")
    engine = SceneEngine(manager, confirmations)
    engine.add_scenes(load_scenes_dir(SCENES_DIR))
    bus = manager.bus
    engine.attach(bus)
    streams = StreamStore(path=tmp_path / "streams.jsonl")
    return Runtime(manager=manager, engine=engine, confirmations=confirmations,
                   bus=bus, driver_name=driver.name, streams=streams)


def _post(port, path, payload, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    conn.request("POST", path, body=json.dumps(payload), headers=headers)
    response = conn.getresponse()
    body = json.loads(response.read() or b"{}")
    conn.close()
    return response.status, body


def _wait_listening(port, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"port {port} never started listening")


def _wait_port_released(port, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                time.sleep(0.05)
        except OSError:
            return
    raise AssertionError(f"port {port} still listening after daemon stop")


@pytest.fixture
def running_daemon(tmp_path, monkeypatch):
    """A Daemon with the gateway extra, running in a background thread."""
    monkeypatch.setenv(TOKEN_ENV_VAR, TOKEN)
    runtime = _runtime(tmp_path)
    port = _free_port()
    daemon = Daemon(runtime, poll_interval=30, tick_seconds=0.05,
                    gateway_port=port)
    thread = threading.Thread(target=daemon.run, name="test-daemon",
                              daemon=True)
    thread.start()
    _wait_listening(port)
    yield runtime, daemon, port
    daemon.stop()
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_gateway_ingest_and_event_inside_daemon(running_daemon):
    runtime, daemon, port = running_daemon

    # Audit trail says the gateway started, on the port we asked for.
    started = [e for e in runtime.manager.audit.read_all()
               if e["action"] == "daemon:gateway_started"]
    assert len(started) == 1
    assert started[0]["result"]["port"] == port

    # /ingest: no token -> 401, right token -> 200 and the point lands
    # in the runtime's own stream store.
    payload = {
        "stream": {"id": "phone-steps", "kind": "health.steps",
                   "source": "phone", "unit": "count"},
        "points": [{"ts": 1000.0, "value": 4321}],
    }
    status, _ = _post(port, "/ingest", payload)
    assert status == 401
    status, body = _post(port, "/ingest", payload, token=TOKEN)
    assert status == 200
    assert body == {"status": "ok", "accepted": 1}
    assert runtime.streams.latest("phone-steps").value == 4321

    # /event: geofence enter fires the arrive-home scene through the
    # same bus the daemon's engine is attached to.
    assert runtime.manager.get_state("living_ac")["onoff"] is False
    status, body = _post(port, "/event", {
        "type": "geofence", "zone": "home", "transition": "enter"},
        token=TOKEN)
    assert status == 200
    assert body == {"status": "published", "type": "geofence"}
    state = runtime.manager.get_state("living_ac")
    assert state["onoff"] is True
    assert state["target_temperature"] == 26

    # The daemon counted the phone event in its stats.
    assert daemon.stats["gateway_events"] == 1
    assert daemon.stats["gateway_errors"] == 0


def test_gateway_port_released_on_stop(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV_VAR, TOKEN)
    runtime = _runtime(tmp_path)
    port = _free_port()
    daemon = Daemon(runtime, tick_seconds=0.05, gateway_port=port)
    thread = threading.Thread(target=daemon.run, daemon=True)
    thread.start()
    _wait_listening(port)

    daemon.stop()
    thread.join(timeout=10)
    assert not thread.is_alive()
    _wait_port_released(port)
    # And the port can be bound again right away.
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))


def test_missing_token_refuses_gateway_but_daemon_runs(tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    runtime = _runtime(tmp_path)
    port = _free_port()
    daemon = Daemon(runtime, tick_seconds=0.01, gateway_port=port)

    # The daemon itself starts and ticks normally (main thread, bounded).
    stats = daemon.run(max_ticks=3)
    assert stats["ticks"] == 3
    assert stats["gateway_errors"] == 1
    assert stats["gateway_events"] == 0

    # Nothing is listening on the gateway port.
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()

    # The refusal reason is in the audit log, naming the missing token.
    refused = [e for e in runtime.manager.audit.read_all()
               if e["action"] == "daemon:gateway_error"]
    assert len(refused) == 1
    assert refused[0]["ok"] is False
    assert TOKEN_ENV_VAR in refused[0]["result"]["reason"]


def test_gateway_off_by_default(tmp_path):
    runtime = _runtime(tmp_path)
    daemon = Daemon(runtime, tick_seconds=0.01)
    stats = daemon.run(max_ticks=2)
    assert stats["gateway_events"] == 0
    assert stats["gateway_errors"] == 0
    actions = [e["action"] for e in runtime.manager.audit.read_all()]
    assert "daemon:gateway_started" not in actions
    assert "daemon:gateway_error" not in actions

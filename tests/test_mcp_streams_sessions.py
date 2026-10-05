"""MCP tools for data streams and terminal sessions (v0.4 integration).

The server is built the way the CLI builds it - manager/engine/
confirmations from the conftest fixtures - with an in-memory-file
StreamStore and a SessionManager injected on a fake clock, so the
tools/list surface (12 tools), the stream reads and the whole session
lifecycle can be asserted at the MCP layer alone. One test also covers
the no-injection defaults: a server built without sessions/streams
must still answer (streams re-read from the state-dir file, sessions
on the manager's own bus).
"""

from __future__ import annotations

import json

import pytest

from omnibutler.core.events import EventBus
from omnibutler.core.sessions import SessionManager
from omnibutler.core.streams import DataStream, StreamStore
from omnibutler.mcp_server.server import TOOLS, create_server

STEPS = DataStream(id="phone-steps", kind="health.steps",
                   source="phone", unit="count")
SLEEP = DataStream(id="watch-sleep", kind="health.sleep",
                   source="watch", unit="minutes")

EXPECTED_TOOL_NAMES = {
    "list_devices", "get_device_state", "set_device_property",
    "call_device_action", "list_scenes", "enable_scene",
    "get_pending_confirmations",
    "list_data_streams", "get_stream_data",
    "list_terminal_sessions", "open_terminal_session",
    "close_terminal_session",
}


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _rpc(server, method, params=None):
    return server.handle({"jsonrpc": "2.0", "id": 1,
                          "method": method, "params": params or {}})


def _call(server, name, arguments=None):
    response = _rpc(server, "tools/call",
                    {"name": name, "arguments": arguments or {}})
    result = response["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


@pytest.fixture()
def store(tmp_path):
    streams = StreamStore(path=tmp_path / "streams.jsonl")
    streams.register(STEPS)
    streams.register(SLEEP)
    for ts, value in ((1000.0, 100), (2000.0, 250), (3000.0, 400)):
        streams.append("phone-steps", value, ts=ts)
    streams.append("watch-sleep", 432, ts=2500.0)
    return streams


@pytest.fixture()
def bus():
    return EventBus()


@pytest.fixture()
def sessions(bus, audit_log, clock):
    return SessionManager(bus=bus, audit=audit_log, clock=clock,
                          idle_timeout=3600.0)


@pytest.fixture()
def audit_log(tmp_path):
    from omnibutler.core.audit import AuditLog

    return AuditLog(path=tmp_path / "audit.jsonl")


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def server(manager, engine, store, sessions):
    return create_server(manager, engine=engine,
                         confirmations=engine.confirmations,
                         sessions=sessions, streams=store)


# -- tool table -------------------------------------------------------------------


def test_tool_table_has_all_twelve_tools(server):
    tools = _rpc(server, "tools/list")["result"]["tools"]
    assert len(tools) == 12
    assert {t["name"] for t in tools} == EXPECTED_TOOL_NAMES
    assert {t["name"] for t in TOOLS} == EXPECTED_TOOL_NAMES
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]  # every tool explains itself


# -- data streams -------------------------------------------------------------------


def test_list_data_streams_shows_latest(store, server):
    payload, is_error = _call(server, "list_data_streams")
    assert not is_error
    by_id = {s["id"]: s for s in payload}
    assert set(by_id) == {"phone-steps", "watch-sleep"}
    steps = by_id["phone-steps"]
    assert (steps["kind"], steps["source"], steps["unit"]) == (
        "health.steps", "phone", "count")
    assert steps["latest"] == {"ts": 3000.0, "value": 400}
    assert by_id["watch-sleep"]["latest"] == {"ts": 2500.0, "value": 432}


def test_get_stream_data_default_and_limited_history(server):
    payload, is_error = _call(server, "get_stream_data",
                              {"stream_id": "phone-steps"})
    assert not is_error
    assert payload["stream"]["id"] == "phone-steps"
    assert payload["latest"] == {"ts": 3000.0, "value": 400}
    assert payload["history"] == [
        {"ts": 1000.0, "value": 100},
        {"ts": 2000.0, "value": 250},
        {"ts": 3000.0, "value": 400},
    ]

    payload, is_error = _call(server, "get_stream_data",
                              {"stream_id": "phone-steps", "limit": 2})
    assert not is_error
    # The most recent N points, still oldest first.
    assert payload["history"] == [
        {"ts": 2000.0, "value": 250},
        {"ts": 3000.0, "value": 400},
    ]

    payload, is_error = _call(server, "get_stream_data",
                              {"stream_id": "phone-steps", "limit": 0})
    assert not is_error
    assert len(payload["history"]) == 1  # clamped, not empty/crash


def test_get_stream_data_unknown_stream_is_a_tool_error(server):
    payload, is_error = _call(server, "get_stream_data",
                              {"stream_id": "no-such-stream"})
    assert is_error
    assert "no-such-stream" in payload["error"]


def test_get_stream_data_bad_arguments_are_tool_errors(server):
    payload, is_error = _call(server, "get_stream_data", {})
    assert is_error and "stream_id" in payload["error"]
    payload, is_error = _call(server, "get_stream_data",
                              {"stream_id": "phone-steps", "limit": "many"})
    assert is_error and "limit" in payload["error"]


# -- terminal sessions ----------------------------------------------------------


def test_session_lifecycle_over_mcp(server, sessions):
    payload, is_error = _call(server, "list_terminal_sessions")
    assert not is_error and payload == []

    payload, is_error = _call(server, "open_terminal_session",
                              {"device_id": "glasses", "kind": "glasses"})
    assert not is_error and payload["ok"] is True
    session_id = payload["session_id"]
    assert payload["session"]["state"] == "active"
    assert payload["session"]["device_id"] == "glasses"

    payload, is_error = _call(server, "list_terminal_sessions")
    assert not is_error and len(payload) == 1
    entry = payload[0]
    assert entry["id"] == session_id
    assert entry["device_id"] == "glasses"
    assert entry["kind"] == "glasses"
    assert entry["state"] == "active"
    assert entry["started_at"] == 1000.0  # fake clock start

    payload, is_error = _call(server, "close_terminal_session",
                              {"session_id": session_id})
    assert not is_error and payload["ok"] is True
    assert payload["session"]["state"] == "closed"

    payload, is_error = _call(server, "list_terminal_sessions")
    assert not is_error and payload == []


def test_session_errors_are_tool_errors(server):
    payload, is_error = _call(server, "close_terminal_session",
                              {"session_id": "ses-9999"})
    assert is_error and "ses-9999" in payload["error"]

    payload, is_error = _call(server, "open_terminal_session",
                              {"device_id": "glasses"})
    assert is_error and "kind" in payload["error"]

    payload, is_error = _call(server, "open_terminal_session",
                              {"device_id": "glasses", "kind": "glasses"})
    assert not is_error
    session_id = payload["session_id"]
    _call(server, "close_terminal_session", {"session_id": session_id})
    payload, is_error = _call(server, "close_terminal_session",
                              {"session_id": session_id})
    assert is_error and "already closed" in payload["error"]


def test_open_session_announces_on_the_bus(server, bus):
    seen = []
    bus.subscribe("session_opened", seen.append)
    bus.subscribe("session_closed", seen.append)
    payload, _ = _call(server, "open_terminal_session",
                       {"device_id": "glasses", "kind": "glasses"})
    assert [e.type for e in seen] == ["session_opened"]
    assert seen[0].get("device") == "glasses"
    assert seen[0].get("session_id") == payload["session_id"]

    _call(server, "close_terminal_session",
          {"session_id": payload["session_id"]})
    assert [e.type for e in seen] == ["session_opened", "session_closed"]


def test_open_session_is_audited_once_by_the_session_manager(
        server, audit_log):
    payload, _ = _call(server, "open_terminal_session",
                       {"device_id": "glasses", "kind": "glasses"})
    _call(server, "close_terminal_session",
          {"session_id": payload["session_id"]})
    actions = [e["action"] for e in audit_log.read_all()]
    assert actions == ["session:opened", "session:activated", "session:closed"]


def test_open_session_never_enters_the_confirmation_queue(server, engine):
    payload, is_error = _call(server, "open_terminal_session",
                              {"device_id": "glasses", "kind": "glasses"})
    assert not is_error
    assert engine.confirmations.pending() == []


# -- defaults when nothing is injected ------------------------------------------


def test_defaults_read_streams_from_the_state_dir(manager, engine, tmp_path,
                                                  monkeypatch):
    # conftest points $OMNIBUTLER_STATE_DIR at a tmp dir; write the
    # streams file there the way the gateway process would have.
    import os

    state_dir = os.environ["OMNIBUTLER_STATE_DIR"]
    on_disk = StreamStore(state_dir=state_dir)
    on_disk.append("phone-steps", 777, ts=5000.0,
                   stream=DataStream(id="phone-steps", kind="health.steps",
                                     source="phone", unit="count"))

    bare = create_server(manager, engine=engine,
                         confirmations=engine.confirmations)
    payload, is_error = _call(bare, "list_data_streams")
    assert not is_error
    assert [s["id"] for s in payload] == ["phone-steps"]
    assert payload[0]["latest"] == {"ts": 5000.0, "value": 777}

    payload, is_error = _call(bare, "get_stream_data",
                              {"stream_id": "phone-steps"})
    assert not is_error and payload["latest"]["value"] == 777


def test_defaults_sessions_share_the_manager_bus(manager, engine):
    bare = create_server(manager, engine=engine,
                         confirmations=engine.confirmations)
    seen = []
    manager.bus.subscribe("session_opened", seen.append)
    payload, is_error = _call(bare, "open_terminal_session",
                              {"device_id": "watch-1", "kind": "watch"})
    assert not is_error
    assert [e.type for e in seen] == ["session_opened"]
    payload, is_error = _call(bare, "list_terminal_sessions")
    assert not is_error and len(payload) == 1
    assert payload[0]["kind"] == "watch"

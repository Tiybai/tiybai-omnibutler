"""Terminal sessions: lifecycle, timeout, events, audit, and the mock
glasses driver - including a session-triggered scene walked end to end.
"""

from __future__ import annotations

import pytest

from omnibutler.core.audit import AuditLog
from omnibutler.core.events import EventBus
from omnibutler.core.manager import DeviceManager
from omnibutler.core.sessions import (
    SessionError,
    SessionManager,
    SessionState,
    TerminalSession,
)
from omnibutler.drivers.terminal_mock import TerminalMockDriver


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def bus():
    return EventBus()


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(path=tmp_path / "audit.jsonl")


@pytest.fixture()
def sessions(bus, audit, clock):
    return SessionManager(bus=bus, audit=audit, clock=clock, idle_timeout=60.0)


# -- lifecycle ---------------------------------------------------------------

def test_open_starts_opening_with_sequential_ids(sessions, clock):
    first = sessions.open_session("glasses", "glasses")
    second = sessions.open_session("watch", "watch")
    assert (first.id, second.id) == ("ses-0001", "ses-0002")
    assert first.state is SessionState.OPENING
    assert first.device_id == "glasses" and first.kind == "glasses"
    assert first.started_at == clock.now and first.last_activity == clock.now
    assert first.is_open and first.closed_at is None


def test_open_requires_device_and_kind(sessions):
    with pytest.raises(SessionError):
        sessions.open_session("", "glasses")
    with pytest.raises(SessionError):
        sessions.open_session("glasses", " ")


def test_activate_moves_to_active_and_touch_refreshes(sessions, clock):
    session = sessions.open_session("glasses", "glasses")
    clock.advance(10)
    sessions.activate(session.id)
    assert session.state is SessionState.ACTIVE
    assert session.last_activity == clock.now
    clock.advance(5)
    sessions.touch(session.id)
    assert session.last_activity == clock.now


def test_close_records_reason_and_blocks_further_transitions(sessions, clock):
    session = sessions.open_session("glasses", "glasses")
    sessions.activate(session.id)
    clock.advance(3)
    sessions.close(session.id, reason="user_done")
    assert session.state is SessionState.CLOSED
    assert session.closed_at == clock.now and session.close_reason == "user_done"
    assert not session.is_open
    with pytest.raises(SessionError):
        sessions.close(session.id)
    with pytest.raises(SessionError):
        sessions.activate(session.id)
    with pytest.raises(SessionError):
        sessions.touch(session.id)


def test_get_unknown_session_raises(sessions):
    with pytest.raises(SessionError):
        sessions.get("ses-9999")


def test_active_for_device(sessions):
    assert sessions.active_for_device("glasses") is None
    session = sessions.open_session("glasses", "glasses")
    assert sessions.active_for_device("glasses") is session
    sessions.close(session.id)
    assert sessions.active_for_device("glasses") is None


# -- timeout -------------------------------------------------------------------

def test_idle_session_is_closed_as_timeout_by_sweep(sessions, clock):
    session = sessions.open_session("glasses", "glasses")
    sessions.activate(session.id)
    clock.advance(61)
    expired = sessions.expire_idle()
    assert expired == [session]
    assert session.state is SessionState.CLOSED
    assert session.close_reason == "timeout"


def test_touch_keeps_session_alive_past_wall_age(sessions, clock):
    session = sessions.open_session("glasses", "glasses")
    sessions.activate(session.id)
    clock.advance(50)
    sessions.touch(session.id)  # activity resets the idle clock
    clock.advance(50)           # 100s old, but only 50s idle
    assert sessions.expire_idle() == []
    assert session.is_open
    clock.advance(11)           # now 61s idle
    assert sessions.expire_idle() == [session]


def test_list_active_sweeps_and_hides_closed(sessions, clock):
    old = sessions.open_session("glasses", "glasses")
    fresh = sessions.open_session("watch", "watch")
    clock.advance(30)
    sessions.touch(fresh.id)
    clock.advance(31)  # old is 61s idle, fresh is 31s idle
    assert sessions.list_active() == [fresh]
    assert old.state is SessionState.CLOSED
    assert sessions.list_sessions(include_closed=True) == [old, fresh]


def test_opening_session_can_time_out_too(sessions, clock):
    session = sessions.open_session("glasses", "glasses")  # never activated
    clock.advance(61)
    assert sessions.expire_idle() == [session]
    assert session.close_reason == "timeout"


# -- events & audit --------------------------------------------------------------

def test_lifecycle_events_and_audit_trail(sessions, bus, audit, clock):
    seen = []
    bus.subscribe("*", seen.append)
    session = sessions.open_session("glasses", "glasses", agent="mcp:test")
    sessions.activate(session.id)
    clock.advance(61)
    sessions.expire_idle()

    assert [e.type for e in seen] == ["session_opened", "session_closed"]
    opened, closed = seen
    assert opened.source == "sessions"
    assert opened.get("session_id") == session.id
    assert opened.get("device_id") == "glasses" and opened.get("kind") == "glasses"
    assert closed.get("reason") == "timeout" and closed.get("state") == "closed"

    entries = audit.read_all()
    assert [e["action"] for e in entries] == [
        "session:opened", "session:activated", "session:closed"]
    assert all(e["device"] == "glasses" for e in entries)
    assert entries[0]["agent"] == "mcp:test"
    assert entries[0]["params"]["session_id"] == session.id
    assert entries[2]["params"]["reason"] == "timeout"


def test_manager_works_without_bus_or_audit(clock):
    bare = SessionManager(clock=clock, idle_timeout=10)
    session = bare.open_session("glasses", "glasses")
    bare.close(session.id)
    assert session.state is SessionState.CLOSED


def test_session_to_dict_is_json_friendly(sessions):
    session = sessions.open_session("glasses", "glasses",
                                    metadata={"wearer": "zhou"})
    data = session.to_dict()
    assert data["state"] == "opening" and data["kind"] == "glasses"
    assert data["metadata"] == {"wearer": "zhou"}


# -- terminal mock driver ---------------------------------------------------------

@pytest.fixture()
def driver():
    return TerminalMockDriver()


def test_terminal_device_is_not_a_switch(driver):
    (device,) = driver.discover()
    assert device.id == "glasses" and device.driver == "terminal_mock"
    assert not device.has_capability("onoff")
    assert device.actions == ["display_text", "speak"]
    assert device.has_capability("battery")


def test_terminal_state_is_read_only_facts(driver):
    state = driver.get_state("glasses")
    assert state["battery"] == 82
    assert state["microphone"] is True
    with pytest.raises(Exception):
        driver.set_property("glasses", "battery", 50)


def test_terminal_output_goes_to_outbox(driver):
    driver.call_action("glasses", "display_text", {"text": "Dinner at 7"})
    driver.call_action("glasses", "speak", {"text": "Dinner at seven"})
    assert [(e["action"], e["text"]) for e in driver.outbox] == [
        ("display_text", "Dinner at 7"),
        ("speak", "Dinner at seven"),
    ]
    driver.clear_outbox()
    assert driver.outbox == []


def test_terminal_actions_validate_input(driver):
    with pytest.raises(Exception):
        driver.call_action("glasses", "display_text", {})
    with pytest.raises(Exception):
        driver.call_action("glasses", "display_text", {"text": "  "})
    with pytest.raises(Exception):
        driver.call_action("glasses", "turn_on", {})
    with pytest.raises(Exception):
        driver.call_action("nope", "speak", {"text": "hi"})


# -- a session scene, walked end to end --------------------------------------------

def test_session_opened_scene_greets_through_glasses(bus, audit, clock):
    """An in-test scene: when a glasses session opens, greet the wearer.

    The YAML scene engine's trigger vocabulary does not know
    ``session_opened`` yet - that is integration work - so this "scene"
    is a plain bus subscriber doing exactly what such a scene would do:
    react to the session event by outputting through the terminal.
    """
    driver = TerminalMockDriver()
    manager = DeviceManager(drivers={"terminal_mock": driver}, audit=audit)
    sessions = SessionManager(bus=bus, audit=audit, clock=clock,
                              idle_timeout=60.0)

    def greet_on_session_opened(event):
        if event.get("kind") != "glasses":
            return
        manager.call_action(
            event.get("device_id"), "display_text",
            {"text": "Welcome back"}, agent="scene:session-greeting")
        manager.call_action(
            event.get("device_id"), "speak",
            {"text": "Welcome back"}, agent="scene:session-greeting")

    bus.subscribe("session_opened", greet_on_session_opened)

    session = sessions.open_session("glasses", "glasses", agent="test")
    sessions.activate(session.id)

    assert [(e["action"], e["text"]) for e in driver.outbox] == [
        ("display_text", "Welcome back"),
        ("speak", "Welcome back"),
    ]
    actions = [e["action"] for e in audit.read_all()]
    assert actions[0] == "session:opened"
    assert "action:display_text" in actions  # greetings went through the manager
    assert "action:speak" in actions


def test_session_is_the_unit_not_the_device(bus, audit, clock):
    """Two sequential sessions on the same glasses are distinct objects."""
    sessions = SessionManager(bus=bus, audit=audit, clock=clock,
                              idle_timeout=60.0)
    first = sessions.open_session("glasses", "glasses")
    sessions.close(first.id, reason="glasses_removed")
    second = sessions.open_session("glasses", "glasses")
    assert isinstance(second, TerminalSession)
    assert second.id != first.id and second.is_open
    assert sessions.list_active() == [second]

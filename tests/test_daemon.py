"""Daemon mode: schedule ticks, state polling, resilience, clean exit."""

import datetime
from pathlib import Path

from omnibutler.core.audit import AuditLog
from omnibutler.core.confirmations import ConfirmationQueue
from omnibutler.core.manager import DeviceManager
from omnibutler.daemon import Daemon, run_daemon
from omnibutler.drivers.mock import MockDriver
from omnibutler.runtime import Runtime
from omnibutler.scenes.engine import SceneEngine
from omnibutler.scenes.loader import load_scenes_dir, parse_scene

SCENES_DIR = Path(__file__).resolve().parent.parent / "examples" / "scenes"


class FakeClock:
    """A clock the test advances by hand (via the daemon's sleep calls)."""

    def __init__(self, start: datetime.datetime) -> None:
        self.current = start

    def now(self) -> datetime.datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += datetime.timedelta(seconds=seconds)


def _runtime(tmp_path, driver=None, extra_scenes=()):
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    driver = driver or MockDriver()
    manager = DeviceManager(drivers={driver.name: driver}, audit=audit)
    # Isolated queue file: ConfirmationQueue is file-persistent, and the
    # default location is shared between processes/test runs.
    confirmations = ConfirmationQueue(path=tmp_path / "confirmations.json")
    engine = SceneEngine(manager, confirmations)
    engine.add_scenes(load_scenes_dir(SCENES_DIR))
    for scene in extra_scenes:
        engine.add_scene(scene)
    runtime = Runtime(manager=manager, engine=engine, confirmations=confirmations,
                      bus=manager.bus, driver_name=driver.name)
    return runtime


def _spy_on_engine(engine):
    """Wrap engine.handle_event, recording every report it returns."""
    reports = []
    original = engine.handle_event

    def spy(event):
        report = original(event)
        reports.append((event, report))
        return report

    engine.handle_event = spy
    return reports


# -- schedule ticks ---------------------------------------------------------

def test_schedule_fires_once_per_minute(tmp_path):
    runtime = _runtime(tmp_path)
    reports = _spy_on_engine(runtime.engine)
    clock = FakeClock(datetime.datetime(2026, 10, 5, 22, 29, 30))
    stats = run_daemon(
        runtime, poll_interval=3600, tick_seconds=1, max_ticks=90,
        sleep_fn=clock.sleep, now_fn=clock.now,
    )
    schedule_reports = [r for ev, r in reports if ev.type == "schedule"]
    # 90 one-second ticks span exactly two minute boundaries: 22:29, 22:30.
    assert len(schedule_reports) == 2
    assert stats["schedule_events"] == 2
    assert stats["ticks"] == 90
    # sleep-mode (at 22:30) was evaluated exactly once despite 60 ticks
    # landing inside that minute.
    evaluated = [name for r in schedule_reports for name in r.evaluated]
    assert evaluated.count("sleep-mode") == 1
    assert runtime.manager.get_state("bedroom_ac")["target_temperature"] == 27


def test_schedule_event_shape_matches_engine_expectations(tmp_path):
    runtime = _runtime(tmp_path)
    reports = _spy_on_engine(runtime.engine)
    clock = FakeClock(datetime.datetime(2026, 10, 5, 8, 15, 0))
    run_daemon(runtime, poll_interval=3600, tick_seconds=1, max_ticks=1,
               sleep_fn=clock.sleep, now_fn=clock.now)
    event = reports[0][0]
    assert event.type == "schedule"
    assert event.get("time") == "08:15"
    assert event.get("minute") == 15


# -- state polling ------------------------------------------------------------

def test_state_change_triggers_scene(tmp_path):
    driver = MockDriver()
    runtime = _runtime(tmp_path, driver=driver)
    clock = FakeClock(datetime.datetime(2026, 10, 5, 12, 0, 0))
    slept = {"done": False}

    def sleep_and_pollute(seconds):
        clock.sleep(seconds)
        if not slept["done"]:
            slept["done"] = True
            driver._devices["air_purifier"].state["pm25"] = 90

    stats = run_daemon(
        runtime, poll_interval=1, tick_seconds=1, max_ticks=4,
        sleep_fn=sleep_and_pollute, now_fn=clock.now,
    )
    # Baseline poll saw pm25=12; the next poll saw 90 and the
    # air-quality-guard scene reacted on its own.
    assert stats["state_changes"] >= 1
    assert runtime.manager.get_state("air_purifier")["mode"] == "turbo"


def test_first_poll_is_baseline_not_a_change(tmp_path):
    runtime = _runtime(tmp_path)
    clock = FakeClock(datetime.datetime(2026, 10, 5, 12, 0, 0))
    stats = run_daemon(
        runtime, poll_interval=1, tick_seconds=1, max_ticks=3,
        sleep_fn=clock.sleep, now_fn=clock.now,
    )
    assert stats["polls"] == 3
    assert stats["state_changes"] == 0


# -- safety ---------------------------------------------------------------------

def test_high_risk_action_is_queued_not_executed(tmp_path):
    scene = parse_scene({
        "name": "light-means-open-garage",
        "trigger": {"type": "state_change", "device": "living_light",
                    "property": "onoff"},
        "actions": [{"device": "garage_door", "set": {"open_close": True}}],
    })
    driver = MockDriver()
    runtime = _runtime(tmp_path, driver=driver, extra_scenes=[scene])
    clock = FakeClock(datetime.datetime(2026, 10, 5, 12, 0, 0))
    slept = {"done": False}

    def sleep_and_flip(seconds):
        clock.sleep(seconds)
        if not slept["done"]:
            slept["done"] = True
            driver._devices["living_light"].state["onoff"] = True

    run_daemon(runtime, poll_interval=1, tick_seconds=1, max_ticks=4,
               sleep_fn=sleep_and_flip, now_fn=clock.now)
    pending = runtime.confirmations.pending()
    assert len(pending) == 1
    assert pending[0].scene == "light-means-open-garage"
    # The garage door itself was NOT opened - it waits for a human.
    assert runtime.manager.get_state("garage_door")["open_close"] is False


# -- resilience -------------------------------------------------------------------

class FlakyMock(MockDriver):
    """Fails the first ``fail_calls`` get_state calls, then behaves."""

    def __init__(self, fail_calls: int = 2) -> None:
        super().__init__()
        self.calls = 0
        self.fail_calls = fail_calls

    def get_state(self, device_id):
        self.calls += 1
        if self.calls <= self.fail_calls:
            raise RuntimeError("device unreachable (simulated)")
        return super().get_state(device_id)


def test_poll_error_is_audited_and_daemon_keeps_running(tmp_path):
    runtime = _runtime(tmp_path, driver=FlakyMock(fail_calls=2))
    clock = FakeClock(datetime.datetime(2026, 10, 5, 12, 0, 0))
    stats = run_daemon(
        runtime, poll_interval=1, tick_seconds=1, max_ticks=5,
        sleep_fn=clock.sleep, now_fn=clock.now,
    )
    assert stats["ticks"] == 5
    assert stats["errors"] == 2
    entries = runtime.manager.audit.read_all()
    poll_errors = [e for e in entries if e["action"] == "daemon:poll_error"]
    assert len(poll_errors) == 2
    assert all(not e["ok"] for e in poll_errors)
    # And once the driver recovered, polling produced real state again.
    assert runtime.manager.get_state("living_ac")["onoff"] is False


# -- shutdown -----------------------------------------------------------------------

def test_max_ticks_exits_cleanly_and_audits_start_stop(tmp_path):
    runtime = _runtime(tmp_path)
    clock = FakeClock(datetime.datetime(2026, 10, 5, 12, 0, 0))
    stats = run_daemon(
        runtime, poll_interval=3600, tick_seconds=1, max_ticks=3,
        sleep_fn=clock.sleep, now_fn=clock.now,
    )
    assert stats["ticks"] == 3
    actions = [e["action"] for e in runtime.manager.audit.read_all()]
    assert "daemon:start" in actions
    assert "daemon:stop" in actions
    # Every fired schedule event left an audit trail too.
    assert "daemon:event" in actions


def test_stop_flag_exits_before_max_ticks(tmp_path):
    runtime = _runtime(tmp_path)
    clock = FakeClock(datetime.datetime(2026, 10, 5, 12, 0, 0))
    daemon = Daemon(runtime, poll_interval=3600, tick_seconds=1,
                    sleep_fn=lambda s: (clock.sleep(s), daemon.stop()),
                    now_fn=clock.now)
    stats = daemon.run(max_ticks=100)
    assert stats["ticks"] < 100
    assert daemon.stopped

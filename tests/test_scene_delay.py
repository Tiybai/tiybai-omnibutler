"""Scene ``delay`` actions: pause an action list, run the rest when due.

Loader tests pin the validation (positive seconds, capped at
``MAX_DELAY_SECONDS``, never the last item, never combined with device
keys); engine tests drive a fake monotonic clock - no real waiting -
and pin the semantics: the prefix runs immediately, the remainder runs
only via :meth:`SceneEngine.process_due`, a high-risk action after a
delay is queued when it comes due, and re-triggering a scene replaces
(restarts) its pending segment instead of stacking a second one.
"""

import datetime
import time
from pathlib import Path

import pytest

from omnibutler.core.audit import AuditLog
from omnibutler.core.confirmations import ConfirmationQueue
from omnibutler.core.events import Event
from omnibutler.core.manager import DeviceManager
from omnibutler.daemon import run_daemon
from omnibutler.drivers.mock import MockDriver
from omnibutler.runtime import Runtime
from omnibutler.scenes.engine import SceneEngine
from omnibutler.scenes.loader import SceneValidationError, load_scene_file, parse_scene
from omnibutler.scenes.model import MAX_DELAY_SECONDS, SceneAction, SceneDelay

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples" / "scenes"


@pytest.fixture()
def clock(monkeypatch):
    """A controllable time.monotonic, in seconds."""
    now = [10_000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    return now


def _scene(actions, **overrides):
    scene = {
        "name": "demo",
        "trigger": {"type": "geofence", "zone": "home", "transition": "enter"},
        "actions": actions,
    }
    scene.update(overrides)
    return scene


def _enter_home():
    return Event(type="geofence", data={"zone": "home", "transition": "enter"})


def _light_timer_engine(manager, seconds=300):
    engine = SceneEngine(manager)
    engine.add_scene(parse_scene(_scene([
        {"device": "living_light", "set": {"onoff": True}},
        {"delay": seconds},
        {"device": "living_light", "set": {"onoff": False}},
    ], name="light-timer")))
    return engine


# -- loader -------------------------------------------------------------------

def test_loader_accepts_delay_between_actions():
    scene = parse_scene(_scene([
        {"device": "living_light", "set": {"onoff": True}},
        {"delay": 300},
        {"device": "living_light", "set": {"onoff": False}},
    ]))
    assert isinstance(scene.actions[0], SceneAction)
    assert isinstance(scene.actions[1], SceneDelay)
    assert scene.actions[1].seconds == 300.0
    assert isinstance(scene.actions[2], SceneAction)


def test_loader_accepts_fractional_and_leading_delay():
    scene = parse_scene(_scene([
        {"delay": 1.5},
        {"device": "living_light", "set": {"onoff": True}},
    ]))
    assert scene.actions[0] == SceneDelay(seconds=1.5)


def test_loader_accepts_delay_at_the_cap():
    scene = parse_scene(_scene([
        {"delay": MAX_DELAY_SECONDS},
        {"device": "living_light", "set": {"onoff": True}},
    ]))
    assert scene.actions[0].seconds == MAX_DELAY_SECONDS


@pytest.mark.parametrize("bad", [0, -5, "300", True, None, MAX_DELAY_SECONDS + 1])
def test_loader_rejects_bad_delay_values(bad):
    with pytest.raises(SceneValidationError) as exc:
        parse_scene(_scene([
            {"delay": bad},
            {"device": "living_light", "set": {"onoff": True}},
        ]))
    assert "delay" in str(exc.value)


def test_loader_rejects_delay_combined_with_device_keys():
    with pytest.raises(SceneValidationError) as exc:
        parse_scene(_scene([
            {"delay": 60, "device": "living_light", "set": {"onoff": True}},
            {"device": "living_light", "set": {"onoff": False}},
        ]))
    assert "delay" in str(exc.value)


def test_loader_rejects_trailing_delay():
    with pytest.raises(SceneValidationError) as exc:
        parse_scene(_scene([
            {"device": "living_light", "set": {"onoff": True}},
            {"delay": 60},
        ]))
    assert "delay" in str(exc.value)


def test_loader_rejects_delay_only_action_list():
    with pytest.raises(SceneValidationError):
        parse_scene(_scene([{"delay": 60}]))


# -- engine: basic due behaviour ----------------------------------------------

def test_prefix_runs_now_remainder_runs_when_due(manager, clock):
    engine = _light_timer_engine(manager)
    report = engine.handle_event(_enter_home())
    assert [o.status for o in report.outcomes] == ["executed"]
    assert manager.get_state("living_light")["onoff"] is True
    assert engine.delayed_pending == 1

    # Not yet due: process_due is a no-op and the light stays on.
    report = engine.process_due()
    assert report.outcomes == []
    assert manager.get_state("living_light")["onoff"] is True

    clock[0] += 299  # one second short
    assert engine.process_due().outcomes == []
    assert manager.get_state("living_light")["onoff"] is True

    clock[0] += 1  # exactly at the deadline
    report = engine.process_due()
    assert report.event_type == "delay"
    assert report.evaluated == ["light-timer"]
    assert [o.status for o in report.outcomes] == ["executed"]
    assert manager.get_state("living_light")["onoff"] is False
    assert engine.delayed_pending == 0


def test_process_due_with_nothing_pending_is_an_empty_report(manager, clock):
    engine = SceneEngine(manager)
    report = engine.process_due()
    assert report.event_type == "delay"
    assert report.outcomes == []
    assert report.evaluated == []


def test_chained_delays_run_segment_by_segment(manager, clock):
    engine = SceneEngine(manager)
    engine.add_scene(parse_scene(_scene([
        {"device": "living_light", "set": {"onoff": True}},
        {"delay": 60},
        {"device": "bedroom_curtain", "set": {"position": 0}},
        {"delay": 60},
        {"device": "living_light", "set": {"onoff": False}},
    ], name="two-stage")))
    engine.handle_event(_enter_home())
    assert manager.get_state("bedroom_curtain")["position"] == 100

    clock[0] += 60
    report = engine.process_due()
    assert [o.device for o in report.outcomes] == ["bedroom_curtain"]
    assert manager.get_state("bedroom_curtain")["position"] == 0
    # The second delay parked the rest again - the light is still on.
    assert manager.get_state("living_light")["onoff"] is True
    assert engine.delayed_pending == 1

    clock[0] += 60
    engine.process_due()
    assert manager.get_state("living_light")["onoff"] is False
    assert engine.delayed_pending == 0


# -- engine: safety -------------------------------------------------------------

def test_high_risk_action_after_delay_is_queued_when_due(manager, clock):
    engine = SceneEngine(manager)
    engine.add_scene(parse_scene(_scene([
        {"device": "living_light", "set": {"onoff": True}},
        {"delay": 60},
        {"device": "garage_door", "set": {"open_close": True}},
    ], name="delayed-garage")))
    engine.handle_event(_enter_home())
    # Nothing queued yet: the risky half has not come due.
    assert engine.confirmations.pending() == []
    assert manager.get_state("garage_door")["open_close"] is False

    clock[0] += 60
    report = engine.process_due()
    assert [o.status for o in report.outcomes] == ["queued"]
    assert len(engine.confirmations.pending()) == 1
    # Queued, not executed - a human still has to approve it.
    assert manager.get_state("garage_door")["open_close"] is False


# -- engine: retrigger semantics (replace, never stack) -------------------------

def test_retrigger_replaces_pending_segment(manager, clock):
    engine = _light_timer_engine(manager)
    engine.handle_event(_enter_home())          # segment due at t+300
    clock[0] += 100
    engine.handle_event(_enter_home())          # re-trigger: restart
    assert engine.delayed_pending == 1          # replaced, not stacked

    clock[0] += 200                             # the ORIGINAL deadline
    assert engine.process_due().outcomes == []  # ...passes with no run
    assert manager.get_state("living_light")["onoff"] is True

    clock[0] += 100                             # the restarted deadline
    report = engine.process_due()
    assert [o.status for o in report.outcomes] == ["executed"]
    assert manager.get_state("living_light")["onoff"] is False


def test_retrigger_of_one_scene_leaves_other_scenes_pending(manager, clock):
    engine = _light_timer_engine(manager)
    engine.add_scene(parse_scene(_scene([
        {"device": "bedroom_ac", "set": {"onoff": True}},
        {"delay": 600},
        {"device": "bedroom_ac", "set": {"onoff": False}},
    ], name="ac-timer",
        trigger={"type": "schedule", "at": "08:00"})))
    engine.handle_event(_enter_home())
    engine.handle_event(Event(type="schedule", data={"time": "08:00", "minute": 0}))
    assert engine.delayed_pending == 2

    clock[0] += 100
    engine.handle_event(_enter_home())          # re-trigger only light-timer:
    assert engine.delayed_pending == 2          # its deadline restarts,
    #                                             ac-timer's is untouched
    clock[0] += 200                             # light-timer's ORIGINAL
    assert engine.process_due().outcomes == []  # deadline passes quietly
    assert engine.delayed_pending == 2

    clock[0] += 300                             # both deadlines now reached
    report = engine.process_due()
    assert sorted(o.scene for o in report.outcomes) == ["ac-timer", "light-timer"]
    assert engine.delayed_pending == 0


# -- bundled examples -----------------------------------------------------------

def test_bundled_hallway_light_timer_loads_and_runs(engine, manager, clock):
    scene = engine.scenes["hallway-light-timer"]
    delays = [a for a in scene.actions if isinstance(a, SceneDelay)]
    assert [d.seconds for d in delays] == [300.0]

    engine.handle_event(_enter_home())
    assert manager.get_state("living_light")["onoff"] is True
    assert engine.delayed_pending == 1
    clock[0] += 300
    engine.process_due()
    assert manager.get_state("living_light")["onoff"] is False


def test_bundled_leave_home_vacuum_loads():
    scene = load_scene_file(EXAMPLES_DIR / "leave-home-vacuum.yaml")
    assert scene.name == "leave-home-vacuum"
    assert scene.trigger.type == "geofence"
    assert scene.trigger.transition == "exit"
    # The mock vacuum device itself is owned by the mock-driver work;
    # this line only pins the agreed device id.
    assert scene.actions[0].device == "living_room_vacuum"


def test_bundled_examples_have_synced_mirrors():
    packaged = EXAMPLES_DIR.parent.parent / "omnibutler" / "_scenes"
    for name in ("hallway-light-timer.yaml", "leave-home-vacuum.yaml"):
        assert (packaged / name).read_text(encoding="utf-8") == \
            (EXAMPLES_DIR / name).read_text(encoding="utf-8")


# -- daemon wiring ----------------------------------------------------------------

def test_daemon_runs_delayed_actions_when_due(tmp_path, clock):
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    driver = MockDriver()
    manager = DeviceManager(drivers={driver.name: driver}, audit=audit)
    confirmations = ConfirmationQueue(path=tmp_path / "confirmations.json")
    engine = SceneEngine(manager, confirmations)
    engine.add_scene(parse_scene({
        "name": "morning-light",
        "trigger": {"type": "schedule", "at": "07:30"},
        "actions": [
            {"device": "living_light", "set": {"onoff": True}},
            {"delay": 120},
            {"device": "living_light", "set": {"onoff": False}},
        ],
    }))
    runtime = Runtime(manager=manager, engine=engine,
                      confirmations=confirmations, bus=manager.bus,
                      driver_name=driver.name)

    wall = {"current": datetime.datetime(2026, 10, 5, 7, 29, 30)}

    def sleep(seconds):
        wall["current"] += datetime.timedelta(seconds=seconds)
        clock[0] += seconds  # the same fake seconds drive the monotonic clock

    stats = run_daemon(
        runtime, poll_interval=3600, tick_seconds=1, max_ticks=200,
        sleep_fn=sleep, now_fn=lambda: wall["current"],
    )
    # 07:30 fired the scene (light on), and 120 ticks later the daemon's
    # per-tick process_due ran the delayed half (light off) - no sleeping
    # on the delay anywhere.
    assert manager.get_state("living_light")["onoff"] is False
    assert stats["delayed_actions"] == 1
    assert stats["errors"] == 0

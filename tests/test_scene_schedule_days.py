"""Schedule triggers with ``days``: a schedule scene can be limited to
given weekdays (``days: [mon, tue, wed, thu, fri]``), so rules like
"weekdays at 07:30" are expressible in the trigger itself.

Loader tests pin the validation (three-letter lowercase abbreviations
only, non-empty list, stored as ``datetime.weekday()`` integers);
engine tests build schedule events whose timestamps fall on known
weekdays - the engine reads the weekday from the event timestamp in
local time, so no clock mocking is needed.
"""

import datetime

import pytest

from omnibutler.core.events import Event
from omnibutler.scenes.engine import SceneEngine
from omnibutler.scenes.loader import (
    SceneValidationError,
    load_scene_file,
    parse_scene,
)

# Known weekdays in October 2026 (pinned by the sanity test below).
MONDAY = datetime.datetime(2026, 10, 5, 7, 30)
FRIDAY = datetime.datetime(2026, 10, 9, 7, 30)
SATURDAY = datetime.datetime(2026, 10, 10, 7, 30)
SUNDAY = datetime.datetime(2026, 10, 4, 7, 30)

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]


def test_fixture_dates_have_the_expected_weekdays():
    assert (MONDAY.weekday(), FRIDAY.weekday(),
            SATURDAY.weekday(), SUNDAY.weekday()) == (0, 4, 5, 6)


def _scene(trigger, name="demo"):
    return parse_scene({
        "name": name,
        "trigger": trigger,
        "actions": [{"device": "living_light", "set": {"onoff": True}}],
    })


def _engine(manager, trigger, name="demo"):
    engine = SceneEngine(manager)
    engine.add_scene(_scene(trigger, name=name))
    return engine


def _tick(moment, **overrides):
    """A schedule event the way the daemon emits it, timestamped then."""
    data = {"time": moment.strftime("%H:%M"), "minute": moment.minute}
    data.update(overrides)
    return Event(type="schedule", source="test", data=data,
                 timestamp=moment.timestamp())


# -- loader -------------------------------------------------------------------

def test_loader_parses_days_into_weekday_ints():
    scene = _scene({"type": "schedule", "at": "07:30", "days": WEEKDAYS})
    assert scene.trigger.days == frozenset({0, 1, 2, 3, 4})


def test_loader_days_default_to_none():
    scene = _scene({"type": "schedule", "at": "07:30"})
    assert scene.trigger.days is None


def test_loader_days_combine_with_every_minutes():
    scene = _scene({"type": "schedule", "every_minutes": 30,
                    "days": ["sat", "sun"]})
    assert scene.trigger.days == frozenset({5, 6})


def test_loader_duplicate_days_collapse():
    scene = _scene({"type": "schedule", "at": "07:30",
                    "days": ["mon", "mon", "fri"]})
    assert scene.trigger.days == frozenset({0, 4})


@pytest.mark.parametrize("bad", ["monday", "Mon", "funday", "", 1, None])
def test_loader_rejects_unknown_day_entries(bad):
    with pytest.raises(SceneValidationError) as exc:
        _scene({"type": "schedule", "at": "07:30", "days": ["mon", bad]})
    assert "days" in str(exc.value)


def test_loader_rejects_empty_days():
    with pytest.raises(SceneValidationError) as exc:
        _scene({"type": "schedule", "at": "07:30", "days": []})
    assert "days" in str(exc.value)


@pytest.mark.parametrize("bad", ["mon", {"mon": True}, 5])
def test_loader_rejects_non_list_days(bad):
    with pytest.raises(SceneValidationError) as exc:
        _scene({"type": "schedule", "at": "07:30", "days": bad})
    assert "days" in str(exc.value)


# -- engine: 'at' form ----------------------------------------------------------

def test_fires_on_a_listed_weekday(manager):
    engine = _engine(manager, {"type": "schedule", "at": "07:30",
                               "days": WEEKDAYS})
    report = engine.handle_event(_tick(MONDAY))
    assert "demo" in report.evaluated
    assert manager.get_state("living_light")["onoff"] is True


def test_fires_on_the_last_listed_weekday(manager):
    engine = _engine(manager, {"type": "schedule", "at": "07:30",
                               "days": WEEKDAYS})
    report = engine.handle_event(_tick(FRIDAY))
    assert "demo" in report.evaluated
    assert manager.get_state("living_light")["onoff"] is True


@pytest.mark.parametrize("moment", [SATURDAY, SUNDAY])
def test_does_not_fire_on_an_unlisted_weekday(manager, moment):
    engine = _engine(manager, {"type": "schedule", "at": "07:30",
                               "days": WEEKDAYS})
    report = engine.handle_event(_tick(moment))
    assert "demo" not in report.evaluated
    assert manager.get_state("living_light")["onoff"] is False


def test_days_still_require_the_time_to_match(manager):
    engine = _engine(manager, {"type": "schedule", "at": "07:30",
                               "days": WEEKDAYS})
    report = engine.handle_event(_tick(MONDAY, time="08:00", minute=0))
    assert "demo" not in report.evaluated
    assert manager.get_state("living_light")["onoff"] is False


def test_no_days_fires_every_day(manager):
    engine = _engine(manager, {"type": "schedule", "at": "07:30"})
    report = engine.handle_event(_tick(SATURDAY))
    assert "demo" in report.evaluated
    assert manager.get_state("living_light")["onoff"] is True


# -- engine: 'every_minutes' form ----------------------------------------------

def test_days_combine_with_every_minutes(manager):
    engine = _engine(manager, {"type": "schedule", "every_minutes": 30,
                               "days": ["sat", "sun"]}, name="weekend")
    report = engine.handle_event(_tick(SATURDAY))  # minute 30, a Saturday
    assert "weekend" in report.evaluated
    assert manager.get_state("living_light")["onoff"] is True


def test_every_minutes_still_blocked_by_days(manager):
    engine = _engine(manager, {"type": "schedule", "every_minutes": 30,
                               "days": ["sat", "sun"]}, name="weekend")
    report = engine.handle_event(_tick(MONDAY))  # minute matches, day does not
    assert "weekend" not in report.evaluated
    assert manager.get_state("living_light")["onoff"] is False


def test_every_minutes_still_requires_the_minute(manager):
    engine = _engine(manager, {"type": "schedule", "every_minutes": 30,
                               "days": ["sat", "sun"]}, name="weekend")
    report = engine.handle_event(_tick(SATURDAY, time="07:15", minute=15))
    assert "weekend" not in report.evaluated
    assert manager.get_state("living_light")["onoff"] is False


# -- engine: where the weekday comes from ---------------------------------------

def test_injected_clock_date_wins_over_event_timestamp(manager):
    # Clock says Sunday while the event is timestamped Monday: the clock
    # wins, mirroring _now_minutes precedence, and the scene stays quiet.
    engine = SceneEngine(manager, clock=lambda: SUNDAY)
    engine.add_scene(_scene({"type": "schedule", "at": "07:30",
                             "days": WEEKDAYS}))
    report = engine.handle_event(_tick(MONDAY))
    assert "demo" not in report.evaluated
    assert manager.get_state("living_light")["onoff"] is False


# -- the bundled example ---------------------------------------------------------

def test_weekday_morning_example(manager, scenes_dir):
    scene = load_scene_file(scenes_dir / "weekday-morning.yaml")
    assert scene.trigger.days == frozenset({0, 1, 2, 3, 4})
    engine = SceneEngine(manager)
    engine.add_scene(scene)
    manager.registry.get("bedroom_curtain").state["position"] = 0

    report = engine.handle_event(_tick(SATURDAY))
    assert "weekday-morning" not in report.evaluated
    assert manager.get_state("bedroom_ac")["onoff"] is False
    assert manager.get_state("bedroom_curtain")["position"] == 0

    report = engine.handle_event(_tick(MONDAY))
    assert "weekday-morning" in report.evaluated
    assert manager.get_state("bedroom_ac")["onoff"] is True
    assert manager.get_state("bedroom_curtain")["position"] == 100

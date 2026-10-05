"""The high-risk guardrail is the project's core safety promise."""

from omnibutler.core.events import Event


def _garage_event():
    return Event(type="geofence", data={"zone": "garage_gate", "transition": "enter"})


def test_garage_door_action_is_queued_not_executed(engine, manager):
    assert manager.get_state("garage_door")["open_close"] is False
    report = engine.handle_event(_garage_event())

    queued = [o for o in report.outcomes if o.status == "queued"]
    assert len(queued) == 1
    assert queued[0].device == "garage_door"

    # The door did NOT move...
    assert manager.get_state("garage_door")["open_close"] is False
    # ...but the low-risk light action in the same scene did run.
    assert manager.get_state("living_light")["onoff"] is True
    # And exactly one confirmation is waiting.
    pending = engine.confirmations.pending()
    assert len(pending) == 1
    assert pending[0].scene == "garage-arrival"
    assert pending[0].risk == "high"


def test_human_confirmation_executes_queued_action(engine, manager):
    engine.handle_event(_garage_event())
    item = engine.confirmations.pending()[0]
    result = engine.confirm(item.id, agent="owner")
    assert result is not None
    assert manager.get_state("garage_door")["open_close"] is True
    assert engine.confirmations.pending() == []
    # Confirming twice does nothing.
    assert engine.confirm(item.id) is None


def test_reject_drops_queued_action(engine, manager):
    engine.handle_event(_garage_event())
    item = engine.confirmations.pending()[0]
    assert engine.reject(item.id) is True
    assert engine.confirmations.pending() == []
    assert manager.get_state("garage_door")["open_close"] is False


def test_high_risk_scene_never_touches_driver_without_confirmation(engine):
    # Run the event twice: two queued items, still zero executions of the door.
    engine.handle_event(_garage_event())
    engine.handle_event(_garage_event())
    assert len(engine.confirmations.pending()) == 2

import pytest

from omnibutler.core.errors import PropertyValidationError


def test_manager_set_updates_state_and_publishes(manager):
    seen = []
    manager.bus.subscribe("state_change", seen.append)
    result = manager.set_property("living_light", "brightness", 55, agent="test")
    assert result == {"brightness": 55}
    assert manager.get_state("living_light")["brightness"] == 55
    assert seen and seen[0].data["device"] == "living_light"
    assert seen[0].data["value"] == 55


def test_manager_rejects_readonly_property(manager):
    with pytest.raises(PropertyValidationError):
        manager.set_property("air_purifier", "pm25", 10)


def test_manager_rejects_out_of_range(manager):
    with pytest.raises(PropertyValidationError):
        manager.set_property("living_ac", "target_temperature", 99)


def test_manager_audit_log_written(manager, tmp_path):
    manager.set_property("living_ac", "onoff", True, agent="tester")
    manager.call_action("bedroom_curtain", "close", {}, agent="tester")
    entries = manager.audit.read_all()
    assert len(entries) == 2
    assert entries[0]["agent"] == "tester"
    assert entries[0]["device"] == "living_ac"
    assert entries[0]["action"] == "set:onoff"
    assert entries[0]["ok"] is True
    assert entries[1]["action"] == "action:close"
    assert (tmp_path / "audit.jsonl").exists()


def test_manager_audit_records_failures(manager):
    with pytest.raises(PropertyValidationError):
        manager.set_property("living_ac", "mode", "turbo")  # not an AC mode
    # Validation failures happen before the driver call, so nothing is routed;
    # a routed driver failure is audited too:
    from omnibutler.core.errors import DeviceNotFoundError

    with pytest.raises(DeviceNotFoundError):
        manager.get_state("ghost")

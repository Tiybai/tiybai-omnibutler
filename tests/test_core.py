import pytest

from omnibutler.core.errors import DeviceNotFoundError, PropertyValidationError
from omnibutler.core.models import Capability, Device, Property, RiskLevel
from omnibutler.core.registry import DeviceRegistry


def _device(device_id="d1", room="kitchen"):
    return Device(
        id=device_id, name="Test", driver="mock", room=room,
        properties={"onoff": Property(Capability.ONOFF),
                    "pm25": Property(Capability.PM25)},
        state={"onoff": False, "pm25": 10},
    )


def test_capability_specs_cover_enum():
    from omnibutler.core.models import CAPABILITY_SPECS

    assert set(CAPABILITY_SPECS) == set(Capability)


def test_property_number_range_validation():
    prop = Property(Capability.TARGET_TEMPERATURE)
    assert prop.validate(24) == 24
    with pytest.raises(PropertyValidationError):
        prop.validate(40)  # above the 32 degree canonical maximum


def test_property_bool_coercion_and_rejection():
    prop = Property(Capability.ONOFF)
    assert prop.validate("on") is True
    assert prop.validate(False) is False
    with pytest.raises(PropertyValidationError):
        prop.validate("banana")


def test_property_enum_options():
    prop = Property(Capability.MODE, options=["cool", "heat"])
    assert prop.validate("cool") == "cool"
    with pytest.raises(PropertyValidationError):
        prop.validate("turbo")


def test_readonly_capability_flag():
    assert Property(Capability.PM25).is_writable is False
    override = Property(Capability.PM25, writable=True)
    assert override.is_writable is True


def test_registry_queries(manager):
    registry = manager.registry
    assert len(registry) >= 7
    assert {d.id for d in registry.by_room("bedroom")} == {"bedroom_ac", "bedroom_curtain"}
    assert "living_ac" in {d.id for d in registry.by_capability("target_temperature")}
    assert "bathroom_scale" in {d.id for d in registry.by_capability(Capability.WEIGHT)}
    with pytest.raises(DeviceNotFoundError):
        registry.get("nope")


def test_registry_register_and_rooms():
    registry = DeviceRegistry()
    registry.register(_device())
    registry.register(_device("d2", room="garage"))
    assert registry.rooms() == ["garage", "kitchen"]
    assert RiskLevel.parse("HIGH") is RiskLevel.HIGH

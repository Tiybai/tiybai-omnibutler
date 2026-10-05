import pytest

from omnibutler.core.errors import DeviceNotFoundError, PropertyValidationError
from omnibutler.drivers.mock import MockDriver


@pytest.fixture()
def driver():
    return MockDriver()


def test_mock_catalogue(driver):
    ids = {d.id for d in driver.list_devices()}
    assert {"living_ac", "bedroom_ac", "air_purifier", "living_light",
            "bedroom_curtain", "bathroom_scale", "garage_door"} <= ids
    garage = next(d for d in driver.list_devices() if d.id == "garage_door")
    assert garage.risk.value == "high"


def test_mock_state_changes(driver):
    assert driver.get_state("living_ac")["onoff"] is False
    driver.set_property("living_ac", "onoff", True)
    driver.set_property("living_ac", "target_temperature", 23)
    state = driver.get_state("living_ac")
    assert state["onoff"] is True
    assert state["target_temperature"] == 23


def test_mock_curtain_actions(driver):
    assert driver.call_action("bedroom_curtain", "close", {})["position"] == 0
    assert driver.call_action("bedroom_curtain", "open", {})["position"] == 100


def test_mock_scale_is_read_via_action(driver):
    result = driver.call_action("bathroom_scale", "measure", {})
    assert "weight" in result


def test_mock_garage_actions(driver):
    result = driver.call_action("garage_door", "open", {})
    assert result["open_close"] is True
    assert driver.get_state("garage_door")["locked"] is False


def test_mock_unknown_device_and_action(driver):
    with pytest.raises(DeviceNotFoundError):
        driver.get_state("ghost")
    with pytest.raises(PropertyValidationError):
        driver.call_action("living_light", "explode", {})

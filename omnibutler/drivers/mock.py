"""Mock driver: a set of virtual devices with real, mutable in-memory state.

This driver is what lets anyone run the full bridge - CLI, scene engine and
MCP server - with no hardware at all. It is also the test fixture driver.
"""

from __future__ import annotations

from typing import Any

from omnibutler.core.errors import DeviceNotFoundError, PropertyValidationError
from omnibutler.core.models import Capability as Cap
from omnibutler.core.models import Device, Property, RiskLevel
from omnibutler.drivers.base import Driver

P = Property


def _catalogue() -> list[Device]:
    """Build a fresh set of virtual devices (state is per-instance)."""
    ac_modes = ["cool", "heat", "dry", "fan", "auto"]
    return [
        Device(
            id="living_ac", name="Living Room AC", driver="mock",
            room="living_room", brand="Demo", model="Virtual AC 1.5HP",
            properties={
                "onoff": P(Cap.ONOFF),
                "target_temperature": P(Cap.TARGET_TEMPERATURE),
                "current_temperature": P(Cap.CURRENT_TEMPERATURE),
                "mode": P(Cap.MODE, options=ac_modes),
                "fan_speed": P(Cap.FAN_SPEED),
                "swing": P(Cap.SWING),
                "humidity": P(Cap.HUMIDITY),
            },
            state={"onoff": False, "target_temperature": 26, "current_temperature": 29,
                   "mode": "cool", "fan_speed": 40, "swing": False, "humidity": 62},
            actions=["turn_on", "turn_off"],
        ),
        Device(
            id="bedroom_ac", name="Bedroom AC", driver="mock",
            room="bedroom", brand="Demo", model="Virtual AC 1.5HP",
            properties={
                "onoff": P(Cap.ONOFF),
                "target_temperature": P(Cap.TARGET_TEMPERATURE),
                "current_temperature": P(Cap.CURRENT_TEMPERATURE),
                "mode": P(Cap.MODE, options=ac_modes),
                "fan_speed": P(Cap.FAN_SPEED),
            },
            state={"onoff": False, "target_temperature": 26, "current_temperature": 28,
                   "mode": "cool", "fan_speed": 30},
            actions=["turn_on", "turn_off"],
        ),
        Device(
            id="air_purifier", name="Air Purifier", driver="mock",
            room="living_room", brand="Demo", model="Virtual Purifier 4",
            properties={
                "onoff": P(Cap.ONOFF),
                "mode": P(Cap.MODE, options=["auto", "silent", "turbo", "manual"]),
                "fan_speed": P(Cap.FAN_SPEED),
                "pm25": P(Cap.PM25),
                "humidity": P(Cap.HUMIDITY),
            },
            state={"onoff": True, "mode": "auto", "fan_speed": 35, "pm25": 12,
                   "humidity": 58},
            actions=["turn_on", "turn_off"],
        ),
        Device(
            id="living_light", name="Living Room Light", driver="mock",
            room="living_room", brand="Demo", model="Virtual Ceiling Light",
            properties={
                "onoff": P(Cap.ONOFF),
                "brightness": P(Cap.BRIGHTNESS),
                "color_temp": P(Cap.COLOR_TEMP),
            },
            state={"onoff": False, "brightness": 80, "color_temp": 4000},
            actions=["turn_on", "turn_off", "toggle"],
        ),
        Device(
            id="living_room_vacuum", name="Living Room Vacuum", driver="mock",
            room="living_room", brand="Demo", model="Virtual Robot Vacuum",
            properties={
                "onoff": P(Cap.ONOFF),
                "battery": P(Cap.BATTERY),
            },
            state={"onoff": False, "battery": 86},
            actions=["turn_on", "turn_off"],
        ),
        Device(
            id="bedroom_curtain", name="Bedroom Curtain", driver="mock",
            room="bedroom", brand="Demo", model="Virtual Curtain Motor",
            properties={
                "onoff": P(Cap.ONOFF),
                "position": P(Cap.POSITION),
                "battery": P(Cap.BATTERY),
            },
            state={"onoff": True, "position": 100, "battery": 87},
            actions=["open", "close", "stop"],
        ),
        Device(
            id="bathroom_scale", name="Smart Scale", driver="mock",
            room="bathroom", brand="Demo", model="Virtual Body Scale",
            properties={
                "weight": P(Cap.WEIGHT),
                "body_fat": P(Cap.BODY_FAT),
                "battery": P(Cap.BATTERY),
            },
            state={"weight": 72.4, "body_fat": 21.3, "battery": 76},
            actions=["measure"],
            risk=RiskLevel.LOW,
        ),
        Device(
            id="garage_door", name="Garage Door", driver="mock",
            room="garage", brand="Demo", model="Virtual Garage Opener",
            properties={
                "open_close": P(Cap.OPEN_CLOSE),
                "position": P(Cap.POSITION),
                "locked": P(Cap.LOCKED),
            },
            state={"open_close": False, "position": 0, "locked": True},
            actions=["open", "close", "lock", "unlock"],
            risk=RiskLevel.HIGH,
        ),
    ]


class MockDriver(Driver):
    name = "mock"

    def __init__(self) -> None:
        self._devices: dict[str, Device] = {d.id: d for d in _catalogue()}

    def _device(self, device_id: str) -> Device:
        try:
            return self._devices[device_id]
        except KeyError:
            raise DeviceNotFoundError(f"mock driver has no device {device_id!r}") from None

    def discover(self) -> list[Device]:
        return list(self._devices.values())

    def list_devices(self) -> list[Device]:
        return list(self._devices.values())

    def get_state(self, device_id: str) -> dict[str, Any]:
        return dict(self._device(device_id).state)

    def set_property(self, device_id: str, property_name: str, value: Any) -> dict[str, Any]:
        device = self._device(device_id)
        device.state[property_name] = value
        # Keep derived virtual readings plausible when the AC runs.
        is_ac = device_id.endswith("_ac") or device.id in {"living_ac", "bedroom_ac"}
        if is_ac and device.state.get("onoff") and "current_temperature" in device.state:
            target = device.state.get("target_temperature", 26)
            current = device.state["current_temperature"]
            device.state["current_temperature"] = current + (target - current) / 2
        return {property_name: value}

    def call_action(
        self, device_id: str, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        device = self._device(device_id)
        state = device.state
        if action == "turn_on":
            state["onoff"] = True
            return {"onoff": True}
        if action == "turn_off":
            state["onoff"] = False
            return {"onoff": False}
        if action == "toggle":
            state["onoff"] = not state.get("onoff", False)
            return {"onoff": state["onoff"]}
        if action in {"open", "close"}:
            opened = action == "open"
            result: dict[str, Any] = {}
            if "open_close" in device.properties:
                state["open_close"] = opened
                result["open_close"] = opened
            if "position" in device.properties:
                state["position"] = 100 if opened else 0
                result["position"] = state["position"]
            if "locked" in device.properties and opened:
                state["locked"] = False
                result["locked"] = False
            return result
        if action in {"lock", "unlock"}:
            state["locked"] = action == "lock"
            return {"locked": state["locked"]}
        if action == "stop":
            return {"position": state.get("position", 0)}
        if action == "measure":
            # A virtual weigh-in: readings drift slightly per call.
            state["weight"] = round(state.get("weight", 70.0) + 0.1, 1)
            return {"weight": state["weight"], "body_fat": state.get("body_fat")}
        raise PropertyValidationError(
            f"device {device_id!r} does not support action {action!r}; "
            f"supported: {device.actions}"
        )

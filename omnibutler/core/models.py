"""Typed capability model shared by every driver.

A *capability* describes one thing a device can report or accept: whether the
living-room air conditioner is on, what temperature it targets, how much PM2.5
the purifier sees. Drivers translate vendor-specific models (MiOT-Spec
services, Tuya data points, Matter clusters, Home Assistant domains) into this
single vocabulary, so scenes and AI agents never need vendor knowledge.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from .errors import PropertyValidationError


class RiskLevel(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]

    @classmethod
    def max_of(cls, *levels: "RiskLevel") -> "RiskLevel":
        return max(levels, key=lambda level: level.rank) if levels else cls.LOW

    @classmethod
    def parse(cls, value: Any) -> "RiskLevel":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).lower())
        except ValueError:
            raise PropertyValidationError(
                f"invalid risk level {value!r}; expected one of low, medium, high"
            ) from None


class Capability(str, enum.Enum):
    """Canonical property names understood across all drivers."""

    ONOFF = "onoff"
    BRIGHTNESS = "brightness"                    # 0-100 %
    COLOR_TEMP = "color_temp"                    # kelvin
    COLOR = "color"                              # hex string, e.g. "#ffcc88"
    TARGET_TEMPERATURE = "target_temperature"    # celsius
    CURRENT_TEMPERATURE = "current_temperature"  # celsius, read-only
    MODE = "mode"                                # enum, device specific values
    FAN_SPEED = "fan_speed"                      # 0-100 % or enum label
    SWING = "swing"                              # bool
    PM25 = "pm25"                                # ug/m3, read-only
    PM10 = "pm10"                                # ug/m3, read-only
    HUMIDITY = "humidity"                        # % RH
    CO2 = "co2"                                  # ppm, read-only
    BATTERY = "battery"                          # %, read-only
    POSITION = "position"                        # 0 closed - 100 open, %
    OPEN_CLOSE = "open_close"                    # bool, True = open
    CONTACT = "contact"                          # bool sensor, read-only
    MOTION = "motion"                            # bool sensor, read-only
    VOLUME = "volume"                            # 0-100 %
    PLAYBACK = "playback"                        # enum: playing/paused/stopped
    WEIGHT = "weight"                            # kg, read-only
    BODY_FAT = "body_fat"                        # %, read-only
    POWER = "power"                              # W, read-only
    ENERGY = "energy"                            # kWh, read-only
    LOCKED = "locked"                            # bool, high risk when writable
    RUNNING = "running"                          # bool, appliance is mid-cycle
    MICROPHONE = "microphone"                    # bool, terminal mic present, read-only


@dataclass(frozen=True)
class CapabilitySpec:
    value_type: str  # "bool" | "number" | "string" | "enum"
    writable: bool = True
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None


CAPABILITY_SPECS: dict[Capability, CapabilitySpec] = {
    Capability.ONOFF: CapabilitySpec("bool"),
    Capability.BRIGHTNESS: CapabilitySpec("number", unit="%", minimum=0, maximum=100),
    Capability.COLOR_TEMP: CapabilitySpec("number", unit="K", minimum=2200, maximum=6500),
    Capability.COLOR: CapabilitySpec("string"),
    Capability.TARGET_TEMPERATURE: CapabilitySpec(
        "number", unit="celsius", minimum=16, maximum=32
    ),
    Capability.CURRENT_TEMPERATURE: CapabilitySpec(
        "number", writable=False, unit="celsius", minimum=-40, maximum=80
    ),
    Capability.MODE: CapabilitySpec("enum"),
    Capability.FAN_SPEED: CapabilitySpec("number", unit="%", minimum=0, maximum=100),
    Capability.SWING: CapabilitySpec("bool"),
    Capability.PM25: CapabilitySpec("number", writable=False, unit="ug/m3", minimum=0),
    Capability.PM10: CapabilitySpec("number", writable=False, unit="ug/m3", minimum=0),
    Capability.HUMIDITY: CapabilitySpec("number", unit="%RH", minimum=0, maximum=100),
    Capability.CO2: CapabilitySpec("number", writable=False, unit="ppm", minimum=0),
    Capability.BATTERY: CapabilitySpec("number", writable=False, unit="%", minimum=0, maximum=100),
    Capability.POSITION: CapabilitySpec("number", unit="%", minimum=0, maximum=100),
    Capability.OPEN_CLOSE: CapabilitySpec("bool"),
    Capability.CONTACT: CapabilitySpec("bool", writable=False),
    Capability.MOTION: CapabilitySpec("bool", writable=False),
    Capability.VOLUME: CapabilitySpec("number", unit="%", minimum=0, maximum=100),
    Capability.PLAYBACK: CapabilitySpec("enum"),
    Capability.WEIGHT: CapabilitySpec("number", writable=False, unit="kg", minimum=0),
    Capability.BODY_FAT: CapabilitySpec("number", writable=False, unit="%", minimum=0, maximum=100),
    Capability.POWER: CapabilitySpec("number", writable=False, unit="W", minimum=0),
    Capability.ENERGY: CapabilitySpec("number", writable=False, unit="kWh", minimum=0),
    Capability.LOCKED: CapabilitySpec("bool"),
    Capability.RUNNING: CapabilitySpec("bool", writable=False),
    Capability.MICROPHONE: CapabilitySpec("bool", writable=False),
}


@dataclass
class Property:
    """One capability instance on a device, with its concrete constraints."""

    capability: Capability
    writable: bool | None = None  # None -> inherit from the capability spec
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    options: list[str] | None = None  # allowed values for enum capabilities

    @property
    def spec(self) -> CapabilitySpec:
        return CAPABILITY_SPECS[self.capability]

    @property
    def name(self) -> str:
        return self.capability.value

    @property
    def is_writable(self) -> bool:
        return self.spec.writable if self.writable is None else self.writable

    @property
    def value_type(self) -> str:
        return self.spec.value_type

    @property
    def effective_unit(self) -> str | None:
        return self.unit if self.unit is not None else self.spec.unit

    @property
    def effective_minimum(self) -> float | None:
        return self.minimum if self.minimum is not None else self.spec.minimum

    @property
    def effective_maximum(self) -> float | None:
        return self.maximum if self.maximum is not None else self.spec.maximum

    def validate(self, value: Any) -> Any:
        """Coerce and validate *value*, returning the canonical value."""
        kind = self.value_type
        if kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in {"true", "false", "on", "off"}:
                return value.lower() in {"true", "on"}
            raise PropertyValidationError(
                f"{self.name}: expected a boolean, got {value!r}"
            )
        if kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    raise PropertyValidationError(
                        f"{self.name}: expected a number, got {value!r}"
                    ) from None
            number = float(value)
            low, high = self.effective_minimum, self.effective_maximum
            if low is not None and number < low:
                raise PropertyValidationError(
                    f"{self.name}: {number} is below the minimum {low}"
                )
            if high is not None and number > high:
                raise PropertyValidationError(
                    f"{self.name}: {number} is above the maximum {high}"
                )
            return int(number) if float(number).is_integer() and isinstance(value, int) else number
        if kind == "enum":
            text = str(value)
            if self.options is not None and text not in self.options:
                raise PropertyValidationError(
                    f"{self.name}: {text!r} is not one of {self.options}"
                )
            return text
        return str(value)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "type": self.value_type,
            "writable": self.is_writable,
        }
        if self.effective_unit:
            data["unit"] = self.effective_unit
        if self.effective_minimum is not None:
            data["min"] = self.effective_minimum
        if self.effective_maximum is not None:
            data["max"] = self.effective_maximum
        if self.options:
            data["options"] = list(self.options)
        return data


@dataclass
class Device:
    """A single controllable or readable smart device."""

    id: str
    name: str
    driver: str
    room: str = "unknown"
    brand: str = ""
    model: str = ""
    properties: dict[str, Property] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)
    risk: RiskLevel = RiskLevel.LOW
    online: bool = True

    def __post_init__(self) -> None:
        # Accept plain string keys / Property shorthand in property specs.
        normalised: dict[str, Property] = {}
        for key, prop in self.properties.items():
            capability = prop.capability if isinstance(prop, Property) else Capability(key)
            normalised[capability.value] = (
                prop if isinstance(prop, Property) else Property(capability)
            )
        self.properties = normalised
        if isinstance(self.risk, str):
            self.risk = RiskLevel.parse(self.risk)

    def has_capability(self, capability: str | Capability) -> bool:
        name = capability.value if isinstance(capability, Capability) else capability
        return name in self.properties

    def property(self, name: str) -> Property:
        try:
            return self.properties[name]
        except KeyError:
            raise PropertyValidationError(
                f"device {self.id!r} has no property {name!r}; "
                f"available: {sorted(self.properties)}"
            ) from None

    def to_dict(self, include_state: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "driver": self.driver,
            "room": self.room,
            "brand": self.brand,
            "model": self.model,
            "risk": self.risk.value,
            "online": self.online,
            "properties": [prop.to_dict() for prop in self.properties.values()],
            "actions": list(self.actions),
        }
        if include_state:
            data["state"] = dict(self.state)
        return data

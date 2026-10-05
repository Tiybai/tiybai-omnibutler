"""Tuya local-protocol driver, built on tinytuya (MIT) as an optional extra.

Install with::

    pip install "tiybai-omnibutler[tuya]"

tinytuya is imported lazily, only when an operation actually needs the
transport. Without it the driver still constructs and lists configured
devices, but any device operation raises a clear error - nothing silently
pretends to work.

Configuration (constructor argument or environment):

    TuyaDriver(devices=[{
        "device_id": "bf...",          # Tuya device id (required)
        "ip": "192.168.1.50",          # LAN address (required)
        "local_key": "...",            # per-device key (required, secret)
        "version": 3.3,                # protocol version (default 3.3)
        "kind": "bulb",                # "outlet" (default) or "bulb"
        "id": "living_plug",           # OmniButler id (default tuya-<dev id>)
        "name": "Living room plug",    # display name (optional)
        "room": "living",              # room (optional)
    }])

or the same list as JSON in the ``TUYA_DEVICES_JSON`` environment variable.
Each device's ``local_key`` is obtained by the user from their own Tuya
account; keys are secrets - they are passed straight to tinytuya, never
stored on Device objects, never logged, and never echoed in error messages.

Data-point (DP) facts used below are the standard Tuya category definitions
(vendor-published): outlets use DP 1 (switch), DP 17 (accumulated energy,
0.01 kWh units) and DP 19 (current power, 0.1 W units); bulbs use DP 20
(switch), DP 22 (brightness, raw 10-1000), DP 23 (colour temperature, raw
0-1000 across the bulb's kelvin range) and DP 24 (colour, HSV hex string).
Per-model mappings for the device catalogue live in device-data/.
"""

from __future__ import annotations

import colorsys
import json
import os
from typing import Any, Iterable

from omnibutler.core.errors import (
    DeviceNotFoundError,
    DriverNotConfiguredError,
    OmniButlerError,
    PlannedDriverError,
    PropertyValidationError,
)
from omnibutler.core.models import Capability as Cap
from omnibutler.core.models import Device, Property
from omnibutler.drivers.base import Driver

_NOT_INSTALLED = (
    "tinytuya is not installed, so the Tuya local driver is planned but not "
    "implemented in this environment. Install the optional extra with: "
    'pip install "tiybai-omnibutler[tuya]" (tinytuya is MIT-licensed; see '
    "docs/license-audit.md), then retry."
)

_ENV_VAR = "TUYA_DEVICES_JSON"

# Canonical properties per device kind.
KIND_PROPERTIES: dict[str, dict[str, Property]] = {
    "outlet": {
        "onoff": Property(Cap.ONOFF),
        "power": Property(Cap.POWER),
        "energy": Property(Cap.ENERGY),
    },
    "bulb": {
        "onoff": Property(Cap.ONOFF),
        "brightness": Property(Cap.BRIGHTNESS),
        "color_temp": Property(Cap.COLOR_TEMP, minimum=2700, maximum=6500),
        "color": Property(Cap.COLOR),
    },
}

# DP ids (string keys, as tinytuya status() returns them).
_OUTLET_DP = {"switch": "1", "energy": "17", "power": "19"}
_BULB_DP = {"switch": "20", "brightness": "22", "color_temp": "23", "color": "24"}
_BRIGHTNESS_RAW_MIN, _BRIGHTNESS_RAW_MAX = 10, 1000


def _load_tinytuya() -> Any:
    try:
        import tinytuya
    except ImportError:
        raise PlannedDriverError(_NOT_INSTALLED) from None
    return tinytuya


class TuyaDriver(Driver):
    name = "tuya"

    def __init__(self, devices: Iterable[dict[str, Any]] | None = None) -> None:
        if devices is None:
            devices = self._devices_from_env()
        self._devices: dict[str, Device] = {}
        self._configs: dict[str, dict[str, Any]] = {}
        self._connections: dict[str, Any] = {}
        for entry in devices:
            device, config = self._build(entry)
            self._devices[device.id] = device
            self._configs[device.id] = config

    # -- configuration ---------------------------------------------------
    @staticmethod
    def _devices_from_env() -> list[dict[str, Any]]:
        raw = os.environ.get(_ENV_VAR, "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DriverNotConfiguredError(
                f"{_ENV_VAR} is not valid JSON ({exc.msg} at line {exc.lineno}). "
                "It must be a JSON list of Tuya device objects."
            ) from exc
        if not isinstance(parsed, list):
            raise DriverNotConfiguredError(
                f"{_ENV_VAR} must be a JSON list of Tuya device objects."
            )
        return parsed

    @staticmethod
    def _build(entry: dict[str, Any]) -> tuple[Device, dict[str, Any]]:
        if not isinstance(entry, dict):
            raise DriverNotConfiguredError(
                "Each Tuya device config must be an object with device_id, "
                "ip and local_key."
            )
        missing = [key for key in ("device_id", "ip", "local_key") if not entry.get(key)]
        if missing:
            # Field names only - values (above all local_key) are never echoed.
            raise DriverNotConfiguredError(
                f"Tuya device config is missing required field(s): "
                f"{', '.join(missing)}. Each device needs device_id, ip and "
                "local_key (from your own Tuya account)."
            )
        kind = str(entry.get("kind", "outlet")).lower()
        if kind not in KIND_PROPERTIES:
            raise DriverNotConfiguredError(
                f"Unsupported Tuya device kind {kind!r}; expected one of "
                f"{sorted(KIND_PROPERTIES)}."
            )
        tuya_id = str(entry["device_id"])
        device = Device(
            id=str(entry.get("id") or f"tuya-{tuya_id}"),
            name=str(entry.get("name") or f"Tuya {kind} {tuya_id[-4:]}"),
            driver="tuya",
            room=str(entry.get("room", "unknown")),
            brand=str(entry.get("brand", "Tuya")),
            model=str(entry.get("model", kind)),
            properties=dict(KIND_PROPERTIES[kind]),
            actions=["turn_on", "turn_off", "toggle"],
        )
        config = {
            "device_id": tuya_id,
            "ip": str(entry["ip"]),
            "local_key": str(entry["local_key"]),
            "version": entry.get("version", 3.3),
            "kind": kind,
        }
        return device, config

    def __repr__(self) -> str:
        # Deliberately excludes configs: local_key must never surface.
        return f"TuyaDriver(devices={sorted(self._devices)})"

    # -- transport ---------------------------------------------------------
    def _connect(self, device_id: str) -> Any:
        if device_id not in self._connections:
            tinytuya = _load_tinytuya()
            config = self._configs[device_id]
            cls = (
                tinytuya.BulbDevice
                if config["kind"] == "bulb"
                else tinytuya.OutletDevice
            )
            self._connections[device_id] = cls(
                config["device_id"],
                config["ip"],
                local_key=config["local_key"],
                version=config["version"],
            )
        return self._connections[device_id]

    def _lookup(self, device_id: str) -> Device:
        try:
            return self._devices[device_id]
        except KeyError:
            raise DeviceNotFoundError(
                f"Tuya driver has no device {device_id!r}; configured: "
                f"{sorted(self._devices)}"
            ) from None

    # -- value mapping -----------------------------------------------------
    @staticmethod
    def _brightness_to_raw(percent: float) -> int:
        span = _BRIGHTNESS_RAW_MAX - _BRIGHTNESS_RAW_MIN
        return round(_BRIGHTNESS_RAW_MIN + percent / 100 * span)

    @staticmethod
    def _brightness_from_raw(raw: float) -> int:
        span = _BRIGHTNESS_RAW_MAX - _BRIGHTNESS_RAW_MIN
        percent = (raw - _BRIGHTNESS_RAW_MIN) / span * 100
        return max(0, min(100, round(percent)))

    @staticmethod
    def _color_temp_to_raw(kelvin: float, low: float, high: float) -> int:
        return round((kelvin - low) / (high - low) * 1000)

    @staticmethod
    def _color_temp_from_raw(raw: float, low: float, high: float) -> int:
        return round(low + raw / 1000 * (high - low))

    @staticmethod
    def _color_from_raw(raw: Any) -> str:
        """Tuya colour DP is an HSV hex string (hhhhssssvvvv); canonical is #rrggbb."""
        if isinstance(raw, str) and len(raw) == 12:
            try:
                hue = int(raw[0:4], 16) / 360
                sat = int(raw[4:8], 16) / 1000
                val = int(raw[8:12], 16) / 1000
            except ValueError:
                return raw
            red, green, blue = colorsys.hsv_to_rgb(hue, sat, val)
            return "#{:02x}{:02x}{:02x}".format(
                round(red * 255), round(green * 255), round(blue * 255)
            )
        return raw if isinstance(raw, str) else str(raw)

    @staticmethod
    def _color_to_raw(hex_color: str) -> tuple[tuple[int, int, int], str]:
        text = hex_color.lstrip("#")
        red, green, blue = (int(text[i : i + 2], 16) for i in (0, 2, 4))
        hue, sat, val = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
        raw = "{:04x}{:04x}{:04x}".format(
            round(hue * 360), round(sat * 1000), round(val * 1000)
        )
        return (red, green, blue), raw

    def _state_from_dps(self, device: Device, dps: dict[str, Any]) -> dict[str, Any]:
        state: dict[str, Any] = {}
        if self._configs[device.id]["kind"] == "outlet":
            if _OUTLET_DP["switch"] in dps:
                state["onoff"] = bool(dps[_OUTLET_DP["switch"]])
            if _OUTLET_DP["power"] in dps:
                state["power"] = float(dps[_OUTLET_DP["power"]]) / 10  # 0.1 W units
            if _OUTLET_DP["energy"] in dps:
                state["energy"] = float(dps[_OUTLET_DP["energy"]]) / 100  # 0.01 kWh
        else:
            if _BULB_DP["switch"] in dps:
                state["onoff"] = bool(dps[_BULB_DP["switch"]])
            if _BULB_DP["brightness"] in dps:
                state["brightness"] = self._brightness_from_raw(
                    float(dps[_BULB_DP["brightness"]])
                )
            if _BULB_DP["color_temp"] in dps:
                prop = device.properties["color_temp"]
                low = prop.effective_minimum or 2700
                high = prop.effective_maximum or 6500
                state["color_temp"] = self._color_temp_from_raw(
                    float(dps[_BULB_DP["color_temp"]]), low, high
                )
            if _BULB_DP["color"] in dps:
                state["color"] = self._color_from_raw(dps[_BULB_DP["color"]])
        return {key: value for key, value in state.items() if key in device.properties}

    # -- Driver API --------------------------------------------------------
    def discover(self) -> list[Device]:
        # Tuya control requires a per-device local_key, so devices enter via
        # configuration, not broadcast scanning; discovery returns them.
        return self.list_devices()

    def list_devices(self) -> list[Device]:
        return list(self._devices.values())

    def get_state(self, device_id: str) -> dict[str, Any]:
        _load_tinytuya()  # the transport dependency gates every operation
        device = self._lookup(device_id)
        status = self._connect(device_id).status() or {}
        dps = status.get("dps", {}) if isinstance(status, dict) else {}
        device.state = self._state_from_dps(device, dps)
        return dict(device.state)

    def set_property(self, device_id: str, property_name: str, value: Any) -> dict[str, Any]:
        _load_tinytuya()  # the transport dependency gates every operation
        device = self._lookup(device_id)
        prop = device.property(property_name)
        if not prop.is_writable:
            raise PropertyValidationError(f"{property_name}: property is read-only")
        canonical = prop.validate(value)
        connection = self._connect(device_id)
        kind = self._configs[device_id]["kind"]
        if property_name == "onoff":
            if canonical:
                connection.turn_on()
            else:
                connection.turn_off()
        elif kind == "bulb" and property_name == "brightness":
            raw = self._brightness_to_raw(float(canonical))
            setter = getattr(connection, "set_brightness", None)
            if callable(setter):
                setter(raw)
            else:
                connection.set_value(int(_BULB_DP["brightness"]), raw)
        elif kind == "bulb" and property_name == "color_temp":
            low = prop.effective_minimum or 2700
            high = prop.effective_maximum or 6500
            raw = self._color_temp_to_raw(float(canonical), low, high)
            setter = getattr(connection, "set_colourtemp", None)
            if callable(setter):
                setter(raw)
            else:
                connection.set_value(int(_BULB_DP["color_temp"]), raw)
        elif kind == "bulb" and property_name == "color":
            (red, green, blue), raw = self._color_to_raw(str(canonical))
            setter = getattr(connection, "set_colour", None)
            if callable(setter):
                setter(red, green, blue)
            else:
                connection.set_value(int(_BULB_DP["color"]), raw)
        else:
            raise OmniButlerError(
                f"Tuya driver cannot set {property_name!r} on a {kind} device."
            )
        device.state[property_name] = canonical
        return {property_name: canonical}

    def call_action(
        self, device_id: str, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        _load_tinytuya()  # the transport dependency gates every operation
        self._lookup(device_id)
        if action in {"turn_on", "turn_off"}:
            return self.set_property(device_id, "onoff", action == "turn_on")
        if action == "toggle":
            current = self.get_state(device_id).get("onoff", False)
            return self.set_property(device_id, "onoff", not current)
        raise OmniButlerError(
            f"Tuya driver does not support action {action!r}; "
            "supported: turn_on, turn_off, toggle."
        )

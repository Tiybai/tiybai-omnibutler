"""Midea M-Smart local-protocol driver, built on msmart-ng (MIT) as an
optional extra.

Install with::

    pip install "tiybai-omnibutler[midea]"

msmart-ng is imported lazily, only when an operation actually needs the
transport. Without it the driver still constructs and lists configured
devices, but any device operation raises a clear error - nothing silently
pretends to work.

Configuration (constructor argument or environment):

    MideaDriver(devices=[{
        "ip": "192.168.1.70",          # LAN address (required)
        "token": "...",                # V3 token (required, secret)
        "key": "...",                  # V3 key (required, secret)
        "device_id": 15393162840672,   # Midea device id (optional)
        "port": 6444,                  # M-Smart port (default 6444)
        "id": "bedroom_ac",            # OmniButler id (optional)
        "name": "Bedroom AC",          # display name (optional)
        "room": "bedroom",             # room (optional)
    }])

or the same list as JSON in the ``MIDEA_DEVICES_JSON`` environment
variable. ``token`` / ``key`` are the per-device V3 credentials the user
obtains from their own Midea account; they are passed straight to
msmart-ng, never stored on Device objects, never logged, and error
messages from the transport are reported by exception type only, so a
library error can never echo a credential back.

msmart-ng is asyncio-based; this driver's API is synchronous, so each
operation runs the library coroutine to completion (in a helper thread
when an event loop is already running in the caller's thread).

Mapping: canonical modes are auto / cool / dry / heat / fan; target
temperature follows the model profile in device-data/ (17-30 C for the
KFR-35GW series); canonical fan_speed is a percentage - msmart's named
speeds map SILENT 20 / LOW 40 / MEDIUM 60 / HIGH 80 / FULL 100, and the
library's AUTO fan setting has no honest percentage, so it is omitted
from reported state rather than faked.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import threading
from collections.abc import Coroutine, Iterable
from typing import Any, cast

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
    "msmart-ng is not installed, so the Midea local driver cannot talk "
    "to devices in this environment. Install the optional extra with: "
    'pip install "tiybai-omnibutler[midea]" (msmart-ng is MIT-licensed; '
    "see docs/license-audit.md), then retry."
)

_ENV_VAR = "MIDEA_DEVICES_JSON"
_DEFAULT_PORT = 6444

_MODE_OPTIONS = ["auto", "cool", "dry", "heat", "fan"]

AC_PROPERTIES: dict[str, Property] = {
    "onoff": Property(Cap.ONOFF),
    "mode": Property(Cap.MODE, options=list(_MODE_OPTIONS)),
    "target_temperature": Property(
        Cap.TARGET_TEMPERATURE, minimum=17, maximum=30
    ),
    "current_temperature": Property(Cap.CURRENT_TEMPERATURE),
    "fan_speed": Property(Cap.FAN_SPEED),
    "swing": Property(Cap.SWING),
}

# msmart named fan speeds are protocol percentages; AUTO is a mode, not a
# percentage (the library encodes it out of range), so it never maps back.
_FAN_NAME_TO_PERCENT = {
    "SILENT": 20,
    "LOW": 40,
    "MEDIUM": 60,
    "HIGH": 80,
    "FULL": 100,
}
_FAN_THRESHOLDS = [(20, "SILENT"), (40, "LOW"), (60, "MEDIUM"), (80, "HIGH")]


def _load_msmart() -> tuple[Any, Any]:
    try:
        import msmart
        from msmart.device import AC
    except ImportError:
        raise PlannedDriverError(_NOT_INSTALLED) from None
    return msmart, AC


def _load_enum(name: str, ac_class: Any) -> Any:
    """Find an msmart enum class wherever this version exposes it."""
    candidate = getattr(ac_class, name, None)
    if candidate is not None:
        return candidate
    for module_name in ("msmart.base", "msmart.const"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        candidate = getattr(module, name, None)
        if candidate is not None:
            return candidate
    return None


def _await(result: Any) -> Any:
    """Run an awaitable to completion from synchronous driver code."""
    if not inspect.isawaitable(result):
        return result
    # asyncio.run accepts coroutines only. The awaitables handed in here
    # are the vendor library's coroutine objects; anything else already
    # raises inside asyncio.run today, so this cast changes nothing.
    coro = cast("Coroutine[Any, Any, Any]", result)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # The caller already runs an event loop in this thread; asyncio.run
    # would refuse, so drive the coroutine on a helper thread instead.
    holder: dict[str, Any] = {}

    def _runner() -> None:
        try:
            holder["value"] = asyncio.run(coro)
        except BaseException as exc:  # relayed to the caller thread
            holder["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in holder:
        raise holder["error"]
    return holder.get("value")


class MideaDriver(Driver):
    name = "midea"

    def __init__(self, devices: Iterable[dict[str, Any]] | None = None) -> None:
        if devices is None:
            devices = self._devices_from_env()
        self._devices: dict[str, Device] = {}
        self._configs: dict[str, dict[str, Any]] = {}
        self._connections: dict[str, Any] = {}
        self._ac_class: Any = None
        self._enums: dict[str, Any] = {}
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
                "It must be a JSON list of Midea device objects."
            ) from exc
        if not isinstance(parsed, list):
            raise DriverNotConfiguredError(
                f"{_ENV_VAR} must be a JSON list of Midea device objects."
            )
        return parsed

    @staticmethod
    def _build(entry: dict[str, Any]) -> tuple[Device, dict[str, Any]]:
        if not isinstance(entry, dict):
            raise DriverNotConfiguredError(
                "Each Midea device config must be an object with ip, "
                "token and key."
            )
        missing = [key for key in ("ip", "token", "key") if not entry.get(key)]
        if missing:
            # Field names only - values (above all token/key) are never echoed.
            raise DriverNotConfiguredError(
                f"Midea device config is missing required field(s): "
                f"{', '.join(missing)}. Each device needs ip, token and "
                "key (the V3 credentials from your own Midea account)."
            )
        device_id_value = int(entry.get("device_id", 0) or 0)
        device = Device(
            id=str(entry.get("id") or f"midea-{device_id_value or entry['ip']}"),
            name=str(entry.get("name") or "Midea AC"),
            driver="midea",
            room=str(entry.get("room", "unknown")),
            brand=str(entry.get("brand", "Midea")),
            model=str(entry.get("model", "AC")),
            properties=dict(AC_PROPERTIES),
            actions=["turn_on", "turn_off", "toggle"],
        )
        config = {
            "ip": str(entry["ip"]),
            "port": int(entry.get("port", _DEFAULT_PORT)),
            "device_id": device_id_value,
            "token": str(entry["token"]),
            "key": str(entry["key"]),
        }
        return device, config

    def __repr__(self) -> str:
        # Deliberately excludes configs: token/key must never surface.
        return f"MideaDriver(devices={sorted(self._devices)})"

    # -- transport ---------------------------------------------------------
    def _connect(self, device_id: str) -> Any:
        if device_id not in self._connections:
            _module, ac_class = _load_msmart()
            self._ac_class = ac_class
            config = self._configs[device_id]
            try:
                connection = ac_class(
                    ip=config["ip"],
                    port=config["port"],
                    device_id=config["device_id"],
                )
                _await(
                    connection.authenticate(config["token"], config["key"])
                )
            except Exception as exc:
                # Type name only: a library error must never echo the
                # token/key back into logs or messages.
                raise OmniButlerError(
                    f"Cannot connect/authenticate to Midea device at "
                    f"{config['ip']} ({type(exc).__name__}). Check the "
                    "device is on, and that token/key match this unit."
                ) from exc
            self._connections[device_id] = connection
        return self._connections[device_id]

    def _lookup(self, device_id: str) -> Device:
        try:
            return self._devices[device_id]
        except KeyError:
            raise DeviceNotFoundError(
                f"Midea driver has no device {device_id!r}; configured: "
                f"{sorted(self._devices)}"
            ) from None

    def _enum(self, name: str, connection: Any) -> Any:
        if name not in self._enums:
            enum_class = _load_enum(name, self._ac_class)
            if enum_class is None:
                # Last resort: infer from the live attribute's type.
                sample = getattr(connection, _ENUM_ATTRIBUTE[name], None)
                if sample is not None and hasattr(type(sample), "__members__"):
                    enum_class = type(sample)
            if enum_class is None:
                raise OmniButlerError(
                    f"The installed msmart-ng does not expose {name}; "
                    "cannot map Midea values safely."
                )
            self._enums[name] = enum_class
        return self._enums[name]

    # -- value mapping -----------------------------------------------------
    @staticmethod
    def _mode_from(value: Any) -> str | None:
        if value is None:
            return None
        name = getattr(value, "name", None) or str(value)
        name = str(name).lower()
        return name if name in _MODE_OPTIONS else None

    def _mode_to(self, connection: Any, canonical: str) -> Any:
        enum_class = self._enum("OperationalMode", connection)
        try:
            return enum_class[canonical.upper()]
        except KeyError:
            raise OmniButlerError(
                f"msmart-ng has no operational mode for {canonical!r}."
            ) from None

    @staticmethod
    def _fan_speed_from(value: Any) -> int | None:
        if value is None:
            return None
        name = getattr(value, "name", None)
        if name is not None:
            upper = str(name).upper()
            if upper == "AUTO":
                return None  # a mode, not a percentage - report nothing
            if upper in _FAN_NAME_TO_PERCENT:
                return _FAN_NAME_TO_PERCENT[upper]
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if numeric > 100:
            return None  # out-of-range sentinel used for AUTO
        return max(0, min(100, round(numeric)))

    def _fan_speed_to(self, connection: Any, percent: float) -> Any:
        enum_class = _load_enum("FanSpeed", self._ac_class)
        if enum_class is not None:
            for limit, name in _FAN_THRESHOLDS:
                if percent <= limit and hasattr(enum_class, name):
                    return getattr(enum_class, name)
            if hasattr(enum_class, "FULL"):
                return enum_class.FULL
        return round(percent)

    @staticmethod
    def _swing_from(value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        name = getattr(value, "name", None) or str(value)
        return str(name).upper() != "OFF"

    def _swing_to(self, connection: Any, enabled: bool) -> Any:
        enum_class = _load_enum("SwingMode", self._ac_class)
        if enum_class is None:
            sample = getattr(connection, "swing_mode", None)
            if sample is not None and hasattr(type(sample), "__members__"):
                enum_class = type(sample)
        if enum_class is None:
            raise OmniButlerError(
                "The installed msmart-ng does not expose SwingMode; "
                "cannot set swing safely."
            )
        member = "VERTICAL" if enabled else "OFF"
        try:
            return enum_class[member]
        except KeyError:
            raise OmniButlerError(
                f"msmart-ng SwingMode has no {member} setting."
            ) from None

    def _state_from_device(self, connection: Any) -> dict[str, Any]:
        state: dict[str, Any] = {}
        power = getattr(connection, "power_state", None)
        if power is not None:
            state["onoff"] = bool(power)
        mode = self._mode_from(getattr(connection, "operational_mode", None))
        if mode is not None:
            state["mode"] = mode
        target = getattr(connection, "target_temperature", None)
        if target is not None:
            state["target_temperature"] = float(target)
        current = getattr(connection, "indoor_temperature", None)
        if current is not None:
            state["current_temperature"] = float(current)
        fan = self._fan_speed_from(getattr(connection, "fan_speed", None))
        if fan is not None:
            state["fan_speed"] = fan
        swing = self._swing_from(getattr(connection, "swing_mode", None))
        if swing is not None:
            state["swing"] = swing
        return state

    def _refresh(self, device_id: str) -> Any:
        connection = self._connect(device_id)
        try:
            _await(connection.refresh())
        except Exception as exc:
            raise OmniButlerError(
                f"Reading the Midea device at "
                f"{self._configs[device_id]['ip']} failed "
                f"({type(exc).__name__})."
            ) from exc
        return connection

    # -- Driver API --------------------------------------------------------
    def discover(self) -> list[Device]:
        # M-Smart V3 control needs the per-device token/key, so devices
        # enter via configuration, not anonymous LAN scanning; discovery
        # returns the configured ones.
        return self.list_devices()

    def list_devices(self) -> list[Device]:
        return list(self._devices.values())

    def get_state(self, device_id: str) -> dict[str, Any]:
        _load_msmart()  # the transport dependency gates every operation
        device = self._lookup(device_id)
        connection = self._refresh(device_id)
        device.state = self._state_from_device(connection)
        return dict(device.state)

    def set_property(
        self, device_id: str, property_name: str, value: Any
    ) -> dict[str, Any]:
        _load_msmart()  # the transport dependency gates every operation
        device = self._lookup(device_id)
        prop = device.property(property_name)
        if not prop.is_writable:
            raise PropertyValidationError(f"{property_name}: property is read-only")
        canonical = prop.validate(value)
        connection = self._connect(device_id)
        if property_name == "onoff":
            connection.power_state = bool(canonical)
        elif property_name == "mode":
            connection.operational_mode = self._mode_to(
                connection, str(canonical)
            )
        elif property_name == "target_temperature":
            connection.target_temperature = float(canonical)
        elif property_name == "fan_speed":
            connection.fan_speed = self._fan_speed_to(
                connection, float(canonical)
            )
        elif property_name == "swing":
            connection.swing_mode = self._swing_to(connection, bool(canonical))
        else:  # pragma: no cover - validate() already rejects unknown props
            raise OmniButlerError(
                f"Midea driver cannot set {property_name!r}."
            )
        try:
            _await(connection.apply())
        except Exception as exc:
            raise OmniButlerError(
                f"Applying the change on the Midea device at "
                f"{self._configs[device_id]['ip']} failed "
                f"({type(exc).__name__})."
            ) from exc
        device.state[property_name] = canonical
        return {property_name: canonical}

    def call_action(
        self, device_id: str, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        _load_msmart()  # the transport dependency gates every operation
        self._lookup(device_id)
        if action in {"turn_on", "turn_off"}:
            return self.set_property(device_id, "onoff", action == "turn_on")
        if action == "toggle":
            current = self.get_state(device_id).get("onoff", False)
            return self.set_property(device_id, "onoff", not current)
        raise OmniButlerError(
            f"Midea driver does not support action {action!r}; "
            "supported: turn_on, turn_off, toggle."
        )


_ENUM_ATTRIBUTE = {
    "OperationalMode": "operational_mode",
    "FanSpeed": "fan_speed",
    "SwingMode": "swing_mode",
}

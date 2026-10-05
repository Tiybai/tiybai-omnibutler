"""Home Assistant driver: talks to a running HA instance over its REST API.

Configuration comes from the environment, never from code:

    HA_URL             base URL, e.g. http://192.168.1.10:8123
    HA_TOKEN           a Home Assistant long-lived access token
    HA_TIMEOUT         optional per-request timeout in seconds (default 10)
    HA_ENTITY_PREFIXES optional comma-separated entity_id prefixes; when set,
                       only entities whose id starts with one of them are
                       surfaced (e.g. "climate.,light.living")

Only the standard library is used (urllib). Requests time out after
``timeout`` seconds and are retried once on transient failures (timeouts,
connection errors, HTTP 5xx). Failures are classified so callers can react:

    HomeAssistantAuthError        HTTP 401/403 - the token was rejected
    HomeAssistantTimeoutError     no answer within the timeout (after retry)
    HomeAssistantConnectionError  HA unreachable (after retry)
    HomeAssistantError            any other HTTP error from HA

All of them subclass OmniButlerError, so existing error handling keeps
working. When HA is not configured at all, the driver raises
DriverNotConfiguredError with the remedy in the message.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any

from omnibutler.core.errors import (
    DeviceNotFoundError,
    DriverNotConfiguredError,
    OmniButlerError,
)
from omnibutler.core.models import Capability as Cap
from omnibutler.core.models import Device, Property
from omnibutler.drivers.base import Driver

# Home Assistant domain -> canonical capabilities we know how to surface.
DOMAIN_PROPERTIES: dict[str, dict[str, Property]] = {
    "climate": {
        "onoff": Property(Cap.ONOFF),
        "target_temperature": Property(Cap.TARGET_TEMPERATURE),
        "current_temperature": Property(Cap.CURRENT_TEMPERATURE),
        "mode": Property(Cap.MODE, options=["off", "cool", "heat", "dry", "fan_only", "auto"]),
        "fan_speed": Property(Cap.FAN_SPEED),
        "humidity": Property(Cap.HUMIDITY),
    },
    "light": {
        "onoff": Property(Cap.ONOFF),
        "brightness": Property(Cap.BRIGHTNESS),
        "color_temp": Property(Cap.COLOR_TEMP),
    },
    "switch": {"onoff": Property(Cap.ONOFF), "power": Property(Cap.POWER)},
    "fan": {
        "onoff": Property(Cap.ONOFF),
        "fan_speed": Property(Cap.FAN_SPEED),
        "mode": Property(Cap.MODE),
    },
    "cover": {
        "open_close": Property(Cap.OPEN_CLOSE),
        "position": Property(Cap.POSITION),
    },
    "vacuum": {"onoff": Property(Cap.ONOFF), "battery": Property(Cap.BATTERY)},
    "media_player": {
        "onoff": Property(Cap.ONOFF),
        "volume": Property(Cap.VOLUME),
        "playback": Property(Cap.PLAYBACK),
    },
    "sensor": {},
    "binary_sensor": {},
    "lock": {"locked": Property(Cap.LOCKED)},
}

# HA attribute name -> canonical property name for state extraction.
ATTRIBUTE_MAP = {
    "temperature": "target_temperature",
    "current_temperature": "current_temperature",
    "humidity": "humidity",
    "current_humidity": "humidity",
    "brightness": "brightness",
    "color_temp_kelvin": "color_temp",
    "color_temp": "color_temp",  # legacy mireds; converted to kelvin on read
    "fan_mode": "fan_speed",
    "percentage": "fan_speed",  # fan entities report speed as a percentage
    "hvac_mode": "mode",
    "current_position": "position",
    "volume_level": "volume",
    "battery_level": "battery",
    "current_power_w": "power",
}


class HomeAssistantError(OmniButlerError):
    """Base class for failures talking to Home Assistant."""


class HomeAssistantAuthError(HomeAssistantError):
    """HA rejected the access token (HTTP 401/403). Not retried."""


class HomeAssistantConnectionError(HomeAssistantError):
    """HA could not be reached at all (after the single retry)."""


class HomeAssistantTimeoutError(HomeAssistantConnectionError):
    """HA did not answer within the request timeout (after the retry)."""


def _is_timeout(reason: Any) -> bool:
    return isinstance(reason, (TimeoutError, socket.timeout)) or (
        isinstance(reason, str) and "timed out" in reason.lower()
    )


def _parse_prefixes(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


class HomeAssistantDriver(Driver):
    name = "homeassistant"

    #: Total attempts per request: the initial try plus one retry.
    MAX_ATTEMPTS = 2

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        timeout: float | None = None,
        entity_prefixes: str | tuple[str, ...] | list[str] | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("HA_URL", "")).rstrip("/")
        self.token = token or os.environ.get("HA_TOKEN", "")
        if timeout is None:
            env_timeout = os.environ.get("HA_TIMEOUT", "")
            try:
                timeout = float(env_timeout) if env_timeout else 10.0
            except ValueError:
                timeout = 10.0
        self.timeout = float(timeout)
        if entity_prefixes is None:
            self.entity_prefixes = _parse_prefixes(
                os.environ.get("HA_ENTITY_PREFIXES")
            )
        elif isinstance(entity_prefixes, str):
            self.entity_prefixes = _parse_prefixes(entity_prefixes)
        else:
            self.entity_prefixes = tuple(
                p.strip() for p in entity_prefixes if p and p.strip()
            )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _entity_allowed(self, entity_id: str) -> bool:
        """True when no prefix filter is set or the id matches a prefix."""
        return not self.entity_prefixes or any(
            entity_id.startswith(prefix) for prefix in self.entity_prefixes
        )

    def _require_config(self) -> None:
        if not self.configured:
            raise DriverNotConfiguredError(
                "Home Assistant is not configured. Set HA_URL (e.g. "
                "http://192.168.1.10:8123) and HA_TOKEN (a long-lived access "
                "token from your HA profile) in the environment, then retry."
            )

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        """One HA API call: timeout + a single retry on transient failures.

        Auth failures (401/403) are never retried - the token will not fix
        itself - and raise HomeAssistantAuthError. Timeouts and connection
        errors are retried once, then classified as HomeAssistantTimeoutError
        / HomeAssistantConnectionError. HTTP 5xx is retried once; other HTTP
        errors raise HomeAssistantError immediately.
        """
        self._require_config()
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            last = attempt == self.MAX_ATTEMPTS
            request = urllib.request.Request(url, data=data, method=method)
            request.add_header("Authorization", f"Bearer {self.token}")
            request.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read().decode()
                return json.loads(body) if body else None
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise HomeAssistantAuthError(
                        f"Home Assistant rejected the access token (HTTP "
                        f"{exc.code}) for {method} {path}. Create a new "
                        "long-lived access token in your HA profile and "
                        "update HA_TOKEN, then retry."
                    ) from exc
                if exc.code >= 500 and not last:
                    continue
                raise HomeAssistantError(
                    f"Home Assistant returned HTTP {exc.code} for {method} {path}. "
                    "Check that HA is reachable and the token is valid."
                ) from exc
            except TimeoutError as exc:
                if not last:
                    continue
                raise HomeAssistantTimeoutError(
                    f"Home Assistant at {self.base_url} did not respond "
                    f"within {self.timeout:g}s for {method} {path} "
                    f"(tried {self.MAX_ATTEMPTS} times). Check that HA is "
                    "running and not overloaded."
                ) from exc
            except urllib.error.URLError as exc:
                if not last:
                    continue
                if _is_timeout(exc.reason):
                    raise HomeAssistantTimeoutError(
                        f"Home Assistant at {self.base_url} did not respond "
                        f"within {self.timeout:g}s for {method} {path} "
                        f"(tried {self.MAX_ATTEMPTS} times). Check that HA "
                        "is running and not overloaded."
                    ) from exc
                raise HomeAssistantConnectionError(
                    f"Cannot reach Home Assistant at {self.base_url} "
                    f"({exc.reason}) after {self.MAX_ATTEMPTS} attempts. "
                    "Check HA_URL and that HA is running on the same network."
                ) from exc
            except OSError as exc:  # e.g. connection reset mid-response
                if not last:
                    continue
                raise HomeAssistantConnectionError(
                    f"Connection to Home Assistant at {self.base_url} "
                    f"failed ({exc}) after {self.MAX_ATTEMPTS} attempts. "
                    "Check HA_URL and that HA is running on the same network."
                ) from exc
        raise HomeAssistantError(  # pragma: no cover - loop always returns/raises
            f"Home Assistant request {method} {path} failed unexpectedly."
        )

    # -- mapping helpers -------------------------------------------------
    def _device_from_state(self, item: dict[str, Any]) -> Device | None:
        entity_id = item.get("entity_id", "")
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        specs = DOMAIN_PROPERTIES.get(domain)
        if specs is None:
            return None
        attrs = item.get("attributes", {}) or {}
        device = Device(
            id=entity_id,
            name=attrs.get("friendly_name", entity_id),
            driver=self.name,
            room=str(attrs.get("room", "unknown")),
            brand="Home Assistant",
            model=domain,
            properties=dict(specs),
            actions=["turn_on", "turn_off"] if domain not in {"sensor", "binary_sensor"} else [],
        )
        device.state = self._state_from_item(device, item)
        return device

    def _state_from_item(self, device: Device, item: dict[str, Any]) -> dict[str, Any]:
        raw = item.get("state")
        attrs = item.get("attributes", {}) or {}
        state: dict[str, Any] = {}
        for key, value in attrs.items():
            canonical = ATTRIBUTE_MAP.get(key)
            if canonical and canonical in device.properties:
                if canonical == "brightness" and isinstance(value, (int, float)):
                    value = round(value / 255 * 100)
                if canonical == "color_temp" and key == "color_temp" \
                        and isinstance(value, (int, float)) and value > 0:
                    value = round(1_000_000 / value)  # legacy mireds -> kelvin
                if canonical == "volume" and isinstance(value, (int, float)):
                    value = round(value * 100)
                if canonical == "fan_speed" and isinstance(value, str):
                    continue  # HA fan modes are labels, not percentages
                state[canonical] = value
        if "onoff" in device.properties:
            state["onoff"] = raw not in {"off", "unavailable", "unknown", None}
        if "mode" in device.properties and attrs.get("hvac_mode"):
            state["mode"] = attrs["hvac_mode"]
        if "locked" in device.properties:
            state["locked"] = raw == "locked"
        if "open_close" in device.properties:
            state["open_close"] = raw == "open"
        if device.model == "sensor":
            unit = str(attrs.get("unit_of_measurement", ""))
            mapping = {"\u00b0C": "current_temperature", "%": "humidity",
                       "ug/m3": "pm25", "\u00b5g/m\u00b3": "pm25", "ppm": "co2"}
            canonical = mapping.get(unit)
            try:
                numeric = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                numeric = None
            if canonical and numeric is not None:
                device.properties[canonical] = Property(getattr(Cap, {
                    "current_temperature": "CURRENT_TEMPERATURE", "humidity": "HUMIDITY",
                    "pm25": "PM25", "co2": "CO2",
                }[canonical]))
                state[canonical] = numeric
        return state

    # -- Driver API --------------------------------------------------------
    def discover(self) -> list[Device]:
        return self.list_devices()

    def list_devices(self) -> list[Device]:
        items = self._request("GET", "/api/states") or []
        devices = []
        for item in items:
            if not self._entity_allowed(item.get("entity_id", "")):
                continue
            device = self._device_from_state(item)
            if device is not None:
                devices.append(device)
        return devices

    def get_state(self, device_id: str) -> dict[str, Any]:
        if not self._entity_allowed(device_id):
            raise DeviceNotFoundError(
                f"HA entity {device_id!r} is excluded by the configured "
                f"entity prefix filter {list(self.entity_prefixes)}"
            )
        item = self._request("GET", f"/api/states/{device_id}")
        if not item:
            raise DeviceNotFoundError(f"Home Assistant has no entity {device_id!r}")
        device = self._device_from_state(item)
        if device is None:
            raise DeviceNotFoundError(f"unsupported HA entity {device_id!r}")
        return device.state

    def set_property(self, device_id: str, property_name: str, value: Any) -> dict[str, Any]:
        domain = device_id.split(".", 1)[0]
        service_data: dict[str, Any] = {"entity_id": device_id}
        if property_name == "onoff":
            service = "turn_on" if value else "turn_off"
        elif property_name == "target_temperature":
            if domain != "climate":
                raise OmniButlerError("target_temperature is only supported for climate entities")
            service, service_data["temperature"] = "set_temperature", value
        elif property_name == "mode":
            service_map = {"climate": "set_hvac_mode", "fan": "set_preset_mode"}
            service = service_map.get(domain, "set_mode")
            key = "hvac_mode" if domain == "climate" else "preset_mode"
            service_data[key] = value
        elif property_name == "brightness":
            service = "turn_on"
            service_data["brightness_pct"] = value
        elif property_name == "color_temp":
            service = "turn_on"
            service_data["color_temp_kelvin"] = value
        elif property_name == "volume":
            service, service_data["volume_level"] = "volume_set", float(value) / 100
        elif property_name == "position":
            service, service_data["position"] = "set_cover_position", value
        elif property_name == "open_close":
            service = "open_cover" if value else "close_cover"
        elif property_name == "locked":
            service = "lock" if value else "unlock"
        else:
            service = f"set_{property_name}"
            service_data[property_name] = value
        self._request("POST", f"/api/services/{domain}/{service}", service_data)
        return {property_name: value}

    def call_action(
        self, device_id: str, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        domain = device_id.split(".", 1)[0]
        service_map = {
            "turn_on": "turn_on", "turn_off": "turn_off", "toggle": "toggle",
            "open": "open_cover", "close": "close_cover", "stop": "stop_cover",
            "lock": "lock", "unlock": "unlock",
        }
        service = service_map.get(action, action)
        payload = {"entity_id": device_id}
        payload.update(params)
        self._request("POST", f"/api/services/{domain}/{service}", payload)
        return {"action": action, "service": f"{domain}.{service}"}

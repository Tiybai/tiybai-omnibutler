"""Home Assistant driver: talks to a running HA instance over its REST API.

Configuration comes from the environment, never from code:

    HA_URL             base URL, e.g. http://192.168.1.10:8123
    HA_TOKEN           a Home Assistant long-lived access token
    HA_TIMEOUT         optional per-request timeout in seconds (default 10)
    HA_ENTITY_PREFIXES optional comma-separated entity_id prefixes; when set,
                       only entities whose id starts with one of them are
                       surfaced (e.g. "climate.,light.living")

The REST path uses only the standard library (urllib). On top of it the
driver offers an *optional* WebSocket event subscription
(``start_event_subscription``): connect to ``/api/websocket``, auth with
the same token, subscribe to ``state_changed`` and push each change to a
callback within a second instead of waiting for the daemon's next poll.
It needs the third-party ``websockets`` library and degrades silently
when that is missing - REST plus polling remains the fallback.

Requests time out after
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

import asyncio
import contextlib
import json
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from omnibutler.core.errors import (
    DeviceNotFoundError,
    DriverNotConfiguredError,
    OmniButlerError,
    PlannedDriverError,
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




#: Why the event subscription cannot run without its optional library
#: (mirrors the Matter driver's wording). The subscription is an extra on
#: top of the REST driver: without ``websockets`` the driver keeps
#: working exactly as before and daemon polling stays the state source -
#: only the near-real-time feed is unavailable.
_SUBSCRIBE_NOT_INSTALLED = (
    "websockets is not installed, so the Home Assistant event "
    "subscription is unavailable; the REST driver and daemon polling "
    "are unaffected. Install websockets (BSD-3-Clause; see "
    "docs/license-audit.md) - it also ships with the 'matter' extra - "
    "to enable the event feed."
)


def _load_websockets() -> Any:
    """Import the optional websockets library or raise PlannedDriverError."""
    try:
        import websockets
    except ImportError:
        raise PlannedDriverError(_SUBSCRIBE_NOT_INSTALLED) from None
    return websockets


class _SubscriptionAuthFailed(HomeAssistantAuthError):
    """HA rejected the token during the WebSocket handshake.

    Kept apart from ordinary connection failures inside the subscription
    loop: a refused token will not fix itself, so the loop stops instead
    of reconnecting forever.
    """


class HomeAssistantDriver(Driver):
    name = "homeassistant"

    #: Total attempts per request: the initial try plus one retry.
    MAX_ATTEMPTS = 2

    #: HA answers are JSON documents; a body past this is a broken (or
    #: hostile) endpoint, not data. Reads are bounded so a peer cannot
    #: make the bridge buffer an unbounded response.
    MAX_RESPONSE_BYTES = 4 * 1024 * 1024

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        timeout: float | None = None,
        entity_prefixes: str | tuple[str, ...] | list[str] | None = None,
        subscribe_events: bool | None = None,
        reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 30.0,
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
        # Optional WebSocket event subscription (see
        # start_event_subscription). Resolution order: the
        # OMNIBUTLER_HA_NO_SUBSCRIBE kill switch wins over everything,
        # then the explicit argument (the runtime passes the config
        # file's ha.subscribe_events here), then the default: enabled.
        no_subscribe = os.environ.get(
            "OMNIBUTLER_HA_NO_SUBSCRIBE", "").strip().lower()
        if no_subscribe in {"1", "true", "yes", "on"}:
            self.subscribe_events = False
        elif subscribe_events is not None:
            self.subscribe_events = bool(subscribe_events)
        else:
            self.subscribe_events = True
        self.reconnect_initial_delay = max(0.0, float(reconnect_initial_delay))
        self.reconnect_max_delay = max(
            self.reconnect_initial_delay, float(reconnect_max_delay))
        self._sub_lock = threading.Lock()
        self._sub_stop = threading.Event()
        self._sub_thread: threading.Thread | None = None
        self._sub_loop: asyncio.AbstractEventLoop | None = None
        self._sub_task: asyncio.Task[Any] | None = None
        self._sub_callback: Callable[[str, dict[str, Any]], None] | None = None
        self._sub_active = False
        self._sub_callback_errors = 0

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
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            last = attempt == self.MAX_ATTEMPTS
            request = urllib.request.Request(url, data=data, method=method)
            request.add_header("Authorization", f"Bearer {self.token}")
            request.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    # Read one byte past the cap so an oversize body is
                    # detected and refused, never silently truncated.
                    raw = response.read(self.MAX_RESPONSE_BYTES + 1)
                if len(raw) > self.MAX_RESPONSE_BYTES:
                    raise HomeAssistantError(
                        f"Home Assistant returned a response over "
                        f"{self.MAX_RESPONSE_BYTES // (1024 * 1024)} MiB "
                        f"for {method} {path}; refusing to process it."
                    )
                body = raw.decode()
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
        item = self._request(
            "GET", f"/api/states/{urllib.parse.quote(device_id, safe='')}")
        if not item:
            raise DeviceNotFoundError(f"Home Assistant has no entity {device_id!r}")
        device = self._device_from_state(item)
        if device is None:
            raise DeviceNotFoundError(f"unsupported HA entity {device_id!r}")
        return device.state

    # -- WebSocket event subscription (optional) ---------------------------
    @property
    def websocket_url(self) -> str:
        """The HA WebSocket endpoint corresponding to the REST base URL."""
        if self.base_url.startswith("https://"):
            url = "wss://" + self.base_url[len("https://"):]
        elif self.base_url.startswith("http://"):
            url = "ws://" + self.base_url[len("http://"):]
        else:
            url = self.base_url
        return url + "/api/websocket"

    @property
    def events_available(self) -> bool:
        """True when the near-real-time event feed can run right now: the
        driver is configured, the feed is enabled, and the optional
        websockets library is importable. Never raises."""
        if not self.subscribe_events or not self.configured:
            return False
        try:
            _load_websockets()
        except PlannedDriverError:
            return False
        return True

    @property
    def subscription_active(self) -> bool:
        """True while a subscription session is live (read-only status)."""
        return self._sub_active

    def start_event_subscription(
        self, on_state: Callable[[str, dict[str, Any]], None]
    ) -> bool:
        """Push HA state changes to ``on_state(device_id, state)``.

        ``state`` is the same canonical dict :meth:`get_state` returns,
        computed from each event's ``new_state`` payload with the regular
        REST mapping. The daemon feeds it into the same snapshot diff it
        uses for polling, so a change reported by both paths fires once.

        Returns True when the background thread is running. Returns
        False - silently, by design - when the feed is disabled, the
        driver is unconfigured, or the websockets library is missing:
        REST polling remains the fallback in every one of those cases.
        The callback runs on the subscription thread; exceptions it
        raises are counted and swallowed so it cannot tear the feed down.
        """
        if not self.events_available:
            return False
        with self._sub_lock:
            if self._sub_thread is not None and self._sub_thread.is_alive():
                return True
            self._sub_stop.clear()
            self._sub_callback = on_state
            self._sub_thread = threading.Thread(
                target=self._subscription_thread_main,
                name="omnibutler-ha-events", daemon=True,
            )
            self._sub_thread.start()
            return True

    def stop_event_subscription(self) -> None:
        """Stop the event feed (idempotent; safe to call when not running)."""
        self._sub_stop.set()
        loop, task = self._sub_loop, self._sub_task
        if loop is not None and task is not None and not loop.is_closed():
            # The loop may close between the check and the call.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)
        thread = self._sub_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._sub_thread = None
        self._sub_active = False

    def _subscription_thread_main(self) -> None:
        # The loop classifies and handles its own failures; the thread
        # must never die loudly - polling keeps state fresh regardless
        # of what happens to the feed.
        with contextlib.suppress(Exception):
            asyncio.run(self._subscription_loop())

    async def _subscription_loop(self) -> None:
        self._sub_loop = asyncio.get_running_loop()
        self._sub_task = asyncio.current_task()
        backoff = self.reconnect_initial_delay
        try:
            while not self._sub_stop.is_set():
                try:
                    await self._subscription_session()
                    backoff = self.reconnect_initial_delay
                except _SubscriptionAuthFailed:
                    break  # a refused token will not fix itself
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass  # transport failure: reconnect after a backoff
                if self._sub_stop.is_set():
                    break
                # Interruptible backoff: poll the stop flag in slices so
                # stop_event_subscription never waits out a full delay.
                waited = 0.0
                while waited < backoff and not self._sub_stop.is_set():
                    step = min(0.05, backoff - waited)
                    await asyncio.sleep(step)
                    waited += step
                backoff = min(backoff * 2, self.reconnect_max_delay)
        except asyncio.CancelledError:
            pass  # cancelled from stop_event_subscription: exit quietly
        finally:
            self._sub_active = False

    async def _subscription_session(self) -> None:
        websockets = _load_websockets()
        async with websockets.connect(self.websocket_url) as connection:
            await self._auth_and_subscribe(connection)
            self._sub_active = True
            try:
                while not self._sub_stop.is_set():
                    raw = await connection.recv()
                    self._handle_event_message(raw)
            finally:
                self._sub_active = False

    async def _auth_and_subscribe(self, connection: Any) -> None:
        """The HA WebSocket handshake: auth_required -> auth -> auth_ok,
        then subscribe to state_changed and consume the result."""
        greeting = self._decode_ws_message(await connection.recv())
        if greeting.get("type") != "auth_required":
            raise HomeAssistantConnectionError(
                "Home Assistant WebSocket sent an unexpected first "
                f"message (type={greeting.get('type')!r}); expected "
                "'auth_required'."
            )
        await connection.send(json.dumps(
            {"type": "auth", "access_token": self.token}, ensure_ascii=False))
        reply = self._decode_ws_message(await connection.recv())
        if reply.get("type") == "auth_invalid":
            raise _SubscriptionAuthFailed(
                "Home Assistant rejected the access token during the "
                "WebSocket handshake. Create a new long-lived access "
                "token in your HA profile and update HA_TOKEN, then "
                "restart the bridge."
            )
        if reply.get("type") != "auth_ok":
            raise HomeAssistantConnectionError(
                "Home Assistant WebSocket authentication failed with an "
                f"unexpected reply (type={reply.get('type')!r})."
            )
        await connection.send(json.dumps({
            "id": 1, "type": "subscribe_events",
            "event_type": "state_changed",
        }, ensure_ascii=False))
        result = self._decode_ws_message(await connection.recv())
        if result.get("type") == "result" and result.get("success") is False:
            raise HomeAssistantConnectionError(
                "Home Assistant refused the state_changed event "
                "subscription."
            )

    @staticmethod
    def _decode_ws_message(raw: Any) -> dict[str, Any]:
        """Parse one WebSocket text frame; anything unusable becomes {}."""
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return message if isinstance(message, dict) else {}

    def _handle_event_message(self, raw: Any) -> None:
        """Turn one WebSocket message into a callback call, or ignore it.

        Only state_changed events with a usable ``new_state`` count; the
        payload item has the same shape as a REST /api/states entry, so
        the regular mapping produces the canonical state. Entity removals
        (``new_state`` null) are skipped - polling covers disappearance.
        """
        message = self._decode_ws_message(raw)
        if message.get("type") != "event":
            return
        event = message.get("event")
        if not isinstance(event, dict) or event.get("event_type") != "state_changed":
            return
        data = event.get("data")
        if not isinstance(data, dict):
            return
        entity_id = data.get("entity_id")
        new_state = data.get("new_state")
        if not isinstance(entity_id, str) or not isinstance(new_state, dict):
            return
        if not self._entity_allowed(entity_id):
            return
        device = self._device_from_state(new_state)
        if device is None:
            return
        callback = self._sub_callback
        if callback is None:
            return
        try:
            callback(device.id, device.state)
        except Exception:
            self._sub_callback_errors += 1

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
        self._request(
            "POST",
            f"/api/services/{urllib.parse.quote(domain, safe='')}"
            f"/{urllib.parse.quote(service, safe='')}",
            service_data)
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
        self._request(
            "POST",
            f"/api/services/{urllib.parse.quote(domain, safe='')}"
            f"/{urllib.parse.quote(service, safe='')}",
            payload)
        return {"action": action, "service": f"{domain}.{service}"}

"""Zigbee driver via an external Zigbee2MQTT process, reached over MQTT.

Install with::

    pip install "tiybai-omnibutler[zigbee]"

This driver does not implement a Zigbee protocol stack. Zigbee2MQTT
(GPL-3.0) runs as its own process next to its own Zigbee coordinator; we
only speak its public MQTT interface - the same isolation pattern as the
Home Assistant REST driver (see docs/license-audit.md). The one library
imported here is paho-mqtt (EPL-2.0 / Apache-2.0 dual-licensed, used
under Apache-2.0), imported lazily: without it the driver still
constructs and lists configured devices, but any device operation raises
a clear error - nothing silently pretends to work.

Zigbee2MQTT conventions used (public, documented upstream):

* Device state arrives as JSON on ``<base>/<friendly_name>`` (retained,
  so the cache warms as soon as we subscribe).
* Control is a JSON publish to ``<base>/<friendly_name>/set``.
* The device list is published (retained) on ``<base>/bridge/devices``
  as a JSON array; each entry carries ``friendly_name``, ``ieee_address``
  and, for supported devices, ``definition.exposes`` describing its
  fields.
* Availability arrives on ``<base>/<friendly_name>/availability`` as
  ``online`` / ``offline`` (plain text, or ``{"state": ...}`` JSON on
  newer versions).

Configuration (constructor arguments or environment):

    Zigbee2MqttDriver(
        mqtt_url="mqtt://192.168.1.10:1883",
        devices=[{
            "friendly_name": "living_light",   # Z2M name (required)
            "kind": "light",        # property preset (see KIND_PROPERTIES)
            "id": "living_light",   # OmniButler id (default z2m-<name>)
            "name": "Living light", # display name (optional)
            "room": "living",       # room (optional)
        }],
    )

or the device list as JSON in ``Z2M_DEVICES_JSON`` and the broker URL in
``Z2M_MQTT_URL`` (``mqtt://[user:pass@]host[:port]`` or ``mqtts://...``).
A device entry may instead give ``"properties": ["onoff", "brightness"]``
(canonical names) to override the kind preset. Devices known only to
Zigbee2MQTT are adopted from the bridge/devices message, with their
property set derived from the published exposes.

State semantics, stated plainly: the driver keeps a cache fed by Z2M
state messages and ``get_state`` reads the cache. ``set_property``
publishes the ``/set`` message and updates the cache *optimistically*;
the device's real state message, which Z2M publishes when the device
answers, overwrites the cache shortly after. If the device never
answers, the cache can briefly claim a value the device does not have.

Value mapping follows Z2M's common expose fields. Only fields with an
unambiguous canonical counterpart are ever sent; anything else is
refused with an explicit error instead of being forwarded blindly:

    canonical            Z2M field                  conversion
    onoff                state                      "ON"/"OFF"
    brightness           brightness                 % <-> 0..254
    color_temp           color_temp                 kelvin <-> mired
    target_temperature   current_heating_setpoint   direct
    current_temperature  local_temperature / temperature  (read-only)
    humidity             humidity                   (read-only)
    motion               occupancy                  (read-only)
    contact              contact                    (read-only)
    battery              battery                    (read-only)
    power / energy       power / energy             (read-only)

The MQTT URL may embed a broker password; it is therefore treated as a
secret: never stored on Device objects, never logged, never echoed in
error messages or ``repr``.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from typing import Any
from urllib.parse import unquote, urlparse

from omnibutler.core.errors import (
    DeviceNotFoundError,
    DriverNotConfiguredError,
    OmniButlerError,
    PlannedDriverError,
    PropertyValidationError,
)
from omnibutler.core.models import CAPABILITY_SPECS, Device, Property
from omnibutler.core.models import Capability as Cap
from omnibutler.drivers.base import Driver

_NOT_INSTALLED = (
    "paho-mqtt is not installed, so the Zigbee2MQTT driver cannot talk "
    "to a broker in this environment. Install the optional extra with: "
    'pip install "tiybai-omnibutler[zigbee]" (paho-mqtt is dual-licensed '
    "EPL-2.0 / Apache-2.0 and is used under Apache-2.0; see "
    "docs/license-audit.md), then retry."
)

_DEVICES_ENV_VAR = "Z2M_DEVICES_JSON"
_URL_ENV_VAR = "Z2M_MQTT_URL"
_BASE_TOPIC_ENV_VAR = "Z2M_BASE_TOPIC"
_DEFAULT_BASE_TOPIC = "zigbee2mqtt"

# Property presets per device kind, for statically configured devices
# whose exposes are not (yet) known. Sensor readings are marked
# read-only explicitly: the canonical humidity spec defaults to writable.
KIND_PROPERTIES: dict[str, dict[str, Property]] = {
    "light": {
        "onoff": Property(Cap.ONOFF),
        "brightness": Property(Cap.BRIGHTNESS),
        "color_temp": Property(Cap.COLOR_TEMP, minimum=2700, maximum=6500),
    },
    "plug": {
        "onoff": Property(Cap.ONOFF),
        "power": Property(Cap.POWER),
        "energy": Property(Cap.ENERGY),
    },
    "thermostat": {
        "current_temperature": Property(Cap.CURRENT_TEMPERATURE),
        "target_temperature": Property(Cap.TARGET_TEMPERATURE),
    },
    "sensor": {
        "current_temperature": Property(Cap.CURRENT_TEMPERATURE),
        "humidity": Property(Cap.HUMIDITY, writable=False),
        "battery": Property(Cap.BATTERY),
    },
    "motion": {
        "motion": Property(Cap.MOTION),
        "battery": Property(Cap.BATTERY),
    },
    "contact": {
        "contact": Property(Cap.CONTACT),
        "battery": Property(Cap.BATTERY),
    },
}
KIND_PROPERTIES["switch"] = {"onoff": Property(Cap.ONOFF)}
KIND_PROPERTIES["outlet"] = KIND_PROPERTIES["plug"]
KIND_PROPERTIES["climate"] = KIND_PROPERTIES["thermostat"]
_DEFAULT_PROPERTIES: dict[str, Property] = {"onoff": Property(Cap.ONOFF)}


def _load_paho() -> Any:
    try:
        import paho.mqtt.client as mqtt_client
    except ImportError:
        raise PlannedDriverError(_NOT_INSTALLED) from None
    return mqtt_client


# -- value conversion ------------------------------------------------------

def _onoff_from_z2m(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() == "ON"


def _brightness_from_z2m(value: Any) -> int:
    return max(0, min(100, round(float(value) / 254 * 100)))


def _brightness_to_z2m(percent: float) -> int:
    return max(0, min(254, round(percent / 100 * 254)))


def _color_temp_from_z2m(mired: Any) -> int:
    return round(1_000_000 / float(mired))


def _color_temp_to_z2m(kelvin: float) -> int:
    return round(1_000_000 / kelvin)


def _as_float(value: Any) -> float:
    return float(value)


def _as_bool(value: Any) -> bool:
    return bool(value)


# Z2M state field -> (canonical property, converter). Fields absent from
# this table (linkquality, voltage, ...) have no canonical counterpart
# and are ignored on the way in.
_INBOUND: dict[str, tuple[str, Any]] = {
    "state": ("onoff", _onoff_from_z2m),
    "brightness": ("brightness", _brightness_from_z2m),
    "color_temp": ("color_temp", _color_temp_from_z2m),
    "local_temperature": ("current_temperature", _as_float),
    "temperature": ("current_temperature", _as_float),
    "current_heating_setpoint": ("target_temperature", _as_float),
    "humidity": ("humidity", _as_float),
    "occupancy": ("motion", _as_bool),
    "contact": ("contact", _as_bool),
    "battery": ("battery", _as_float),
    "power": ("power", _as_float),
    "energy": ("energy", _as_float),
}

# Canonical property -> (Z2M /set field, converter). Deliberately short:
# these are the only writes whose wire format is unambiguous.
_OUTBOUND: dict[str, tuple[str, Any]] = {
    "onoff": ("state", lambda value: "ON" if value else "OFF"),
    "brightness": ("brightness", _brightness_to_z2m),
    "color_temp": ("color_temp", _color_temp_to_z2m),
    "target_temperature": ("current_heating_setpoint", _as_float),
}


def _properties_from_exposes(exposes: Any) -> dict[str, Property]:
    """Derive canonical properties from a Z2M exposes tree.

    Composite exposes (light, climate, switch, ...) nest their fields in
    ``features``; walk recursively. A field is writable when its Z2M
    access bitmask includes the "set" bit (2) and the canonical
    capability is writable at all.
    """
    properties: dict[str, Property] = {}

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        for feature in node.get("features") or []:
            visit(feature)
        field = node.get("property")
        if not field or field not in _INBOUND:
            return
        canonical = _INBOUND[field][0]
        if canonical in properties:
            return
        access = node.get("access", 1)
        # The canonical spec decides first (battery, contact, ... are
        # read-only capabilities whatever an expose claims); the Z2M
        # "set" bit (2) can only narrow that further.
        writable = bool(access & 2) and CAPABILITY_SPECS[Cap(canonical)].writable
        kwargs: dict[str, Any] = {"writable": writable}
        if canonical == "color_temp":
            # Expose bounds are mireds; canonical bounds are kelvin, and
            # the axis flips (more mired = fewer kelvin).
            low_mired, high_mired = node.get("value_min"), node.get("value_max")
            if low_mired and high_mired:
                kwargs["minimum"] = round(1_000_000 / float(high_mired))
                kwargs["maximum"] = round(1_000_000 / float(low_mired))
        elif canonical == "target_temperature":
            if node.get("value_min") is not None:
                kwargs["minimum"] = float(node["value_min"])
            if node.get("value_max") is not None:
                kwargs["maximum"] = float(node["value_max"])
        properties[canonical] = Property(Cap(canonical), **kwargs)

    visit(exposes)
    return properties


# -- MQTT transport ----------------------------------------------------------

class _PahoMqttClient:
    """Adapt paho-mqtt to the small client surface the driver uses.

    The driver-facing surface is: ``connect()``, ``disconnect()``,
    ``subscribe(topic)``, ``publish(topic, payload)`` and an assignable
    ``on_message(topic, payload_bytes)`` callback. Tests inject a fake
    implementing the same surface instead of a real broker client.
    """

    def __init__(self, parts: dict[str, Any]) -> None:
        mqtt = _load_paho()
        try:  # paho-mqtt >= 2.0 requires an explicit callback API version
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except (AttributeError, ValueError):
            client = mqtt.Client()  # paho-mqtt 1.x
        self._client = client
        self._parts = parts
        self.on_message: Any = None
        if parts.get("username"):
            client.username_pw_set(parts["username"], parts.get("password"))
        if parts.get("tls"):
            client.tls_set()

        def _forward(_client: Any, _userdata: Any, message: Any) -> None:
            if self.on_message is not None:
                self.on_message(message.topic, message.payload)

        client.on_message = _forward

    def connect(self) -> None:
        self._client.connect(self._parts["host"], self._parts["port"])
        self._client.loop_start()

    def disconnect(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def subscribe(self, topic: str) -> None:
        self._client.subscribe(topic)

    def publish(self, topic: str, payload: str) -> None:
        self._client.publish(topic, payload)


def _parse_mqtt_url(url: str) -> dict[str, Any]:
    """Parse an mqtt(s):// URL into connection parts.

    Error messages never include the URL itself: it may embed a broker
    password in its userinfo.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme in {"mqtt", "tcp"}:
        tls, default_port = False, 1883
    elif scheme in {"mqtts", "ssl", "tls"}:
        tls, default_port = True, 8883
    else:
        raise DriverNotConfiguredError(
            "MQTT URL must use the mqtt:// or mqtts:// scheme "
            f"(got scheme {scheme!r}); expected something like "
            "mqtt://192.168.1.10:1883."
        )
    if not parsed.hostname:
        raise DriverNotConfiguredError(
            "MQTT URL has no host; expected something like "
            "mqtt://192.168.1.10:1883."
        )
    return {
        "host": parsed.hostname,
        "port": parsed.port or default_port,
        "tls": tls,
        "username": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
    }


class Zigbee2MqttDriver(Driver):
    name = "zigbee2mqtt"

    def __init__(
        self,
        devices: Iterable[dict[str, Any]] | None = None,
        *,
        mqtt_url: str | None = None,
        base_topic: str | None = None,
        username: str | None = None,
        password: str | None = None,
        client: Any = None,
    ) -> None:
        if devices is None:
            devices = self._devices_from_env()
        self._mqtt_url = mqtt_url or os.environ.get(_URL_ENV_VAR) or None
        if username is not None or password is not None:
            self._url_override = {"username": username, "password": password}
        else:
            self._url_override = {}
        self._base_topic = (
            base_topic or os.environ.get(_BASE_TOPIC_ENV_VAR) or _DEFAULT_BASE_TOPIC
        ).strip("/")
        self._client = client
        self._connected = False
        self._lock = threading.Lock()
        self._devices: dict[str, Device] = {}
        self._friendly: dict[str, str] = {}  # friendly_name -> device id
        self._from_config_props: set[str] = set()  # ids with explicit props
        for entry in devices:
            self._register_config(entry)

    # -- configuration ---------------------------------------------------
    @staticmethod
    def _devices_from_env() -> list[dict[str, Any]]:
        raw = os.environ.get(_DEVICES_ENV_VAR, "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DriverNotConfiguredError(
                f"{_DEVICES_ENV_VAR} is not valid JSON ({exc.msg} at line "
                f"{exc.lineno}). It must be a JSON list of Zigbee2MQTT "
                "device objects."
            ) from exc
        if not isinstance(parsed, list):
            raise DriverNotConfiguredError(
                f"{_DEVICES_ENV_VAR} must be a JSON list of Zigbee2MQTT "
                "device objects."
            )
        return parsed

    @staticmethod
    def _slug(friendly_name: str) -> str:
        slug = "".join(
            char.lower() if char.isalnum() else "_" for char in friendly_name
        ).strip("_")
        return f"z2m-{slug or 'device'}"

    def _register_config(self, entry: dict[str, Any]) -> Device:
        if not isinstance(entry, dict):
            raise DriverNotConfiguredError(
                "Each Zigbee2MQTT device config must be an object with a "
                "friendly_name."
            )
        friendly = entry.get("friendly_name")
        if not friendly:
            raise DriverNotConfiguredError(
                "Zigbee2MQTT device config is missing the required field: "
                "friendly_name (the name the device has in Zigbee2MQTT)."
            )
        friendly = str(friendly)
        explicit = entry.get("properties")
        if explicit is not None:
            properties = self._properties_from_names(explicit, friendly)
            explicit_props = True
        elif entry.get("kind"):
            kind = str(entry["kind"]).lower()
            if kind not in KIND_PROPERTIES:
                raise DriverNotConfiguredError(
                    f"Unsupported Zigbee2MQTT device kind {kind!r}; "
                    f"expected one of {sorted(KIND_PROPERTIES)} or an "
                    "explicit 'properties' list."
                )
            properties = dict(KIND_PROPERTIES[kind])
            explicit_props = True
        else:
            properties = dict(_DEFAULT_PROPERTIES)
            explicit_props = False
        device = Device(
            id=str(entry.get("id") or self._slug(friendly)),
            name=str(entry.get("name") or friendly),
            driver=self.name,
            room=str(entry.get("room", "unknown")),
            brand=str(entry.get("brand", "Zigbee")),
            model=str(entry.get("model", "")),
            properties=properties,
            actions=["turn_on", "turn_off", "toggle"],
        )
        self._devices[device.id] = device
        self._friendly[friendly] = device.id
        if explicit_props:
            self._from_config_props.add(device.id)
        return device

    @staticmethod
    def _properties_from_names(names: Any, friendly: str) -> dict[str, Property]:
        if not isinstance(names, list) or not names:
            raise DriverNotConfiguredError(
                f"Zigbee2MQTT device {friendly!r}: 'properties' must be a "
                "non-empty list of canonical property names."
            )
        properties: dict[str, Property] = {}
        for name in names:
            try:
                capability = Cap(str(name))
            except ValueError:
                raise DriverNotConfiguredError(
                    f"Zigbee2MQTT device {friendly!r}: unknown canonical "
                    f"property {name!r}."
                ) from None
            properties[capability.value] = Property(capability)
        return properties

    def __repr__(self) -> str:
        # Deliberately excludes the broker URL: it may embed a password.
        return f"Zigbee2MqttDriver(devices={sorted(self._devices)})"

    # -- transport ---------------------------------------------------------
    def _ensure_connected(self) -> Any:
        if self._connected and self._client is not None:
            return self._client
        if self._client is None:
            _load_paho()  # transport gates every operation
            if not self._mqtt_url:
                raise DriverNotConfiguredError(
                    f"No MQTT broker URL configured. Pass mqtt_url=... or "
                    f"set {_URL_ENV_VAR} (e.g. mqtt://192.168.1.10:1883) "
                    "pointing at the broker Zigbee2MQTT uses."
                )
            parts = _parse_mqtt_url(self._mqtt_url)
            parts.update({k: v for k, v in self._url_override.items() if v is not None})
            self._client = _PahoMqttClient(parts)
        self._client.on_message = self._handle_message
        self._client.connect()
        base = self._base_topic
        self._client.subscribe(f"{base}/bridge/devices")
        self._client.subscribe(f"{base}/+")
        self._client.subscribe(f"{base}/+/availability")
        self._connected = True
        return self._client

    def close(self) -> None:
        if self._connected and self._client is not None:
            disconnect = getattr(self._client, "disconnect", None)
            if callable(disconnect):
                disconnect()
            self._connected = False

    # -- inbound messages --------------------------------------------------
    def _handle_message(self, topic: str, payload: Any) -> None:
        prefix = self._base_topic + "/"
        if not topic.startswith(prefix):
            return
        parts = topic[len(prefix):].split("/")
        if isinstance(payload, (bytes, bytearray)):
            text = bytes(payload).decode("utf-8", errors="replace")
        else:
            text = str(payload)
        with self._lock:
            if parts == ["bridge", "devices"]:
                self._handle_bridge_devices(text)
            elif len(parts) == 1:
                self._handle_state(parts[0], text)
            elif len(parts) == 2 and parts[1] == "availability":
                self._handle_availability(parts[0], text)
            # Anything else (bridge/*, */set echoes, /get traffic) is
            # not device state and is ignored by design.

    def _handle_bridge_devices(self, text: str) -> None:
        try:
            entries = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            friendly = entry.get("friendly_name")
            if not friendly or friendly == "Coordinator":
                continue
            friendly = str(friendly)
            definition = entry.get("definition") or {}
            device_id = self._friendly.get(friendly)
            if device_id is None:
                device = Device(
                    id=self._slug(friendly),
                    name=friendly,
                    driver=self.name,
                    brand=str(definition.get("vendor", "Zigbee")),
                    model=str(definition.get("model", "")),
                    properties=dict(_DEFAULT_PROPERTIES),
                    actions=["turn_on", "turn_off", "toggle"],
                )
                self._devices[device.id] = device
                self._friendly[friendly] = device.id
            else:
                device = self._devices[device_id]
                if definition.get("model"):
                    device.model = str(definition["model"])
                if definition.get("vendor"):
                    device.brand = str(definition["vendor"])
            # Adopt the published exposes unless the user pinned the
            # property set in configuration.
            if device.id not in self._from_config_props:
                derived = _properties_from_exposes(definition.get("exposes"))
                if derived:
                    device.properties = derived

    def _handle_state(self, friendly: str, text: str) -> None:
        device_id = self._friendly.get(friendly)
        if device_id is None:
            return  # a device Z2M knows but has not listed yet; wait for it
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        device = self._devices[device_id]
        updates: dict[str, Any] = {}
        for field, raw in payload.items():
            mapping = _INBOUND.get(field)
            if mapping is None:
                continue
            canonical, convert = mapping
            if canonical not in device.properties:
                continue
            try:
                updates[canonical] = convert(raw)
            except (TypeError, ValueError):
                continue
        if updates:
            device.state.update(updates)

    def _handle_availability(self, friendly: str, text: str) -> None:
        device_id = self._friendly.get(friendly)
        if device_id is None:
            return
        state = text.strip()
        if state.startswith("{"):
            try:
                state = str(json.loads(state).get("state", ""))
            except json.JSONDecodeError:
                return
        if state in {"online", "offline"}:
            self._devices[device_id].online = state == "online"

    def _lookup(self, device_id: str) -> Device:
        try:
            return self._devices[device_id]
        except KeyError:
            raise DeviceNotFoundError(
                f"Zigbee2MQTT driver has no device {device_id!r}; known: "
                f"{sorted(self._devices)}"
            ) from None

    def _friendly_of(self, device_id: str) -> str:
        for friendly, known_id in self._friendly.items():
            if known_id == device_id:
                return friendly
        raise DeviceNotFoundError(f"Zigbee2MQTT driver has no device {device_id!r}")

    # -- Driver API --------------------------------------------------------
    def discover(self) -> list[Device]:
        # Ask the broker: connecting subscribes us to the retained
        # bridge/devices list, whose message adopts every device Z2M
        # knows. Delivery is asynchronous, so this returns the devices
        # known *now* - configured ones plus any already adopted - and
        # later arrivals show up in list_devices().
        self._ensure_connected()
        return self.list_devices()

    def list_devices(self) -> list[Device]:
        return list(self._devices.values())

    def get_state(self, device_id: str) -> dict[str, Any]:
        self._ensure_connected()
        device = self._lookup(device_id)
        return dict(device.state)

    def set_property(self, device_id: str, property_name: str, value: Any) -> dict[str, Any]:
        client = self._ensure_connected()
        device = self._lookup(device_id)
        prop = device.property(property_name)
        if not prop.is_writable:
            raise PropertyValidationError(f"{property_name}: property is read-only")
        canonical = prop.validate(value)
        mapping = _OUTBOUND.get(property_name)
        if mapping is None:
            raise OmniButlerError(
                f"Zigbee2MQTT driver does not know how to write "
                f"{property_name!r} safely, so it refuses to forward it. "
                f"Writable mappings: {sorted(_OUTBOUND)}."
            )
        field, convert = mapping
        payload = json.dumps({field: convert(canonical)})
        topic = f"{self._base_topic}/{self._friendly_of(device_id)}/set"
        client.publish(topic, payload)
        # Optimistic cache update: Z2M's answering state message will
        # overwrite this with the device's real values (see module
        # docstring for the exact semantics).
        with self._lock:
            device.state[property_name] = canonical
        return {property_name: canonical}

    def call_action(
        self, device_id: str, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self._ensure_connected()
        self._lookup(device_id)
        if action in {"turn_on", "turn_off"}:
            return self.set_property(device_id, "onoff", action == "turn_on")
        if action == "toggle":
            current = self.get_state(device_id).get("onoff", False)
            return self.set_property(device_id, "onoff", not current)
        raise OmniButlerError(
            f"Zigbee2MQTT driver does not support action {action!r}; "
            "supported: turn_on, turn_off, toggle."
        )

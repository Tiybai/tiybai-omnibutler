"""Matter driver - controller-client mode, via an external Matter
controller service (matterjs-server or python-matter-server).

Install with::

    pip install "tiybai-omnibutler[matter]"

Why a client, not a controller: Matter control needs a commissioned
fabric, a radio-side commissioner and a full controller stack. This
project does not re-implement that stack. Instead this driver talks to
a controller service the user already runs (matterjs-server, or
python-matter-server as used by the Home Assistant Matter integration)
over its WebSocket JSON-RPC API - default ``ws://127.0.0.1:5580/ws`` -
and translates the nodes / endpoints / clusters it reports into the
canonical OmniButler capability model.

``websockets`` (BSD-3-Clause) is imported lazily, only when an
operation actually needs the transport. Without it the driver still
constructs and lists its statically configured devices, but any device
operation raises a clear error - nothing silently pretends to work.

Configuration (constructor arguments or environment):

    MatterDriver(
        server_url="ws://127.0.0.1:5580/ws",
        nodes=[{"node_id": 1, "name": "Ceiling bulb", "room": "living"}],
    )

or ``MATTER_SERVER_URL`` / ``MATTER_NODES_JSON`` (a JSON list like the
``nodes`` argument). The static ``nodes`` entries only carry labels
(node id -> name / room); endpoints and clusters are learned from the
controller by :meth:`discover`. Until then each configured node shows
up as a placeholder device with no properties - the driver does not
invent endpoints it has not seen.

Devices: one OmniButler device per (node, endpoint), id
``matter-<node_id>-<endpoint_id>``, plus a ``matter-controller``
pseudo-device whose only job is the ``commission`` action.

Cluster mapping (only clusters with a confident canonical equivalent
become properties):

    ============  ==================  ===================================
    Cluster       Attribute(s)        Canonical
    ============  ==================  ===================================
    OnOff         OnOff               ``onoff``
    LevelControl  CurrentLevel        ``brightness`` (0-254 <-> 0-100 %)
    ColorControl  ColorTemperature-   ``color_temp`` (mireds <-> kelvin)
                    Mireds
    ColorControl  CurrentHue +        ``color`` (hex; hue has no
                    CurrentSaturation   standalone canonical capability,
                                        so, like the Tuya driver, hue
                                        and saturation fold into the
                                        hex colour)
    Temperature-  MeasuredValue       ``current_temperature``
      Measurement                       (0.01 C units)
    Relative-     MeasuredValue       ``humidity`` (0.01 % units,
      Humidity                            read-only here)
    Thermostat    LocalTemperature    ``current_temperature``
    Thermostat    OccupiedCooling- /  ``target_temperature``
                    HeatingSetpoint     (0.01 C units)
    Thermostat    SystemMode          ``mode`` (off/auto/cool/heat)
    ============  ==================  ===================================

OccupancySensing has no canonical Capability yet (same situation as
miIO filter life): its Occupancy bitmap is reported as a raw,
read-only ``occupancy`` state value. Every other attribute the driver
does not map - unknown clusters included - is likewise passed through
read-only into state under a stable ``matter_c<cluster>_a<attribute>``
key, with the value exactly as the controller reported it. Nothing is
renamed into a canonical property the model cannot back up.

Commissioning: the ``commission`` action on the ``matter-controller``
device forwards a pairing code to the controller's commissioning
interface (``commission_with_code``). The controller service performs
the actual commissioning; this driver never pretends to pair a device
itself, and an unreachable controller is a clear error.

Writes go out as cluster commands where Matter defines them
(OnOff.On/Off, LevelControl.MoveToLevel, ColorControl
.MoveToColorTemperature / MoveToHueAndSaturation via the
``send_device_command`` API command) and as attribute writes for the
Thermostat setpoint / SystemMode (``write_attribute`` API command).

UNVERIFIED SEAMS - read before trusting this driver against a live
server. It has been tested only against an in-process fake of the wire
protocol (tests/test_matter_driver.py), never against a real
matterjs-server or python-matter-server. The API command names
(``get_nodes``, ``commission_with_code``, ``send_device_command``,
``write_attribute``), their argument shapes, the node payload shape
(attributes keyed ``"<endpoint>/<cluster>/<attribute>"``), and the
camelCase cluster-command payload field names follow the
python-matter-server client API as publicly documented; exact details
vary between server implementations and versions (in particular
``write_attribute`` is not available on every server), and each
mismatch surfaces as a classified error from this driver rather than
silent misbehaviour. The controller URL is not a credential, but
server error details are reported truncated and by code, and nothing
from the transport is ever written to the audit log by this driver.
"""

from __future__ import annotations

import asyncio
import colorsys
import inspect
import itertools
import json
import os
import threading
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
    "websockets is not installed, so the Matter driver cannot talk to "
    "the controller service in this environment. Install the optional "
    'extra with: pip install "tiybai-omnibutler[matter]" '
    "(websockets is BSD-3-Clause; see docs/license-audit.md), "
    "then retry."
)

_URL_ENV_VAR = "MATTER_SERVER_URL"
_NODES_ENV_VAR = "MATTER_NODES_JSON"
_DEFAULT_SERVER_URL = "ws://127.0.0.1:5580/ws"
_CONTROLLER_ID = "matter-controller"

# Matter cluster ids (from the Matter cluster specification).
_CLUSTER_ONOFF = 0x0006
_CLUSTER_LEVEL = 0x0008
_CLUSTER_COLOR = 0x0300
_CLUSTER_TEMP_MEASUREMENT = 0x0402
_CLUSTER_HUMIDITY_MEASUREMENT = 0x0405
_CLUSTER_OCCUPANCY = 0x0406
_CLUSTER_THERMOSTAT = 0x0201
_CLUSTER_BASIC_INFO = 0x0028

# Attribute ids used from BasicInformation (endpoint 0 of every node).
_ATTR_VENDOR_NAME = 1
_ATTR_PRODUCT_NAME = 3
_ATTR_NODE_LABEL = 5

_THERMOSTAT_MODE_OPTIONS = ["off", "auto", "cool", "heat"]
_THERMOSTAT_MODE_FROM = {0: "off", 1: "auto", 3: "cool", 4: "heat"}
_THERMOSTAT_MODE_TO = {name: code for code, name in _THERMOSTAT_MODE_FROM.items()}


def _load_websockets() -> Any:
    try:
        import websockets
    except ImportError:
        raise PlannedDriverError(_NOT_INSTALLED) from None
    return websockets


def _await(result: Any) -> Any:
    """Run an awaitable to completion from synchronous driver code."""
    if not inspect.isawaitable(result):
        return result
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(result)
    # The caller already runs an event loop in this thread; asyncio.run
    # would refuse, so drive the coroutine on a helper thread instead.
    holder: dict[str, Any] = {}

    def _runner() -> None:
        try:
            holder["value"] = asyncio.run(result)
        except BaseException as exc:  # relayed to the caller thread
            holder["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in holder:
        raise holder["error"]
    return holder.get("value")


class _ServerReportedError(OmniButlerError):
    """The controller answered, but with an error for our command."""


def _unwrap(value: Any) -> Any:
    """Undo the value wrappers some server versions put around scalars.

    Attribute values sometimes arrive as ``{"value": x, "type": ...}``
    metadata dicts instead of the bare value. Only unwrap when the dict
    carries nothing but metadata keys - a genuine struct value is data
    and passes through untouched.
    """
    if isinstance(value, dict) and "value" in value:
        if set(value) <= {"value", "type", "_type", "subtype"}:
            return value["value"]
    return value


class MatterDriver(Driver):
    name = "matter"

    def __init__(
        self,
        server_url: str | None = None,
        nodes: Iterable[dict[str, Any]] | None = None,
        *,
        timeout: float = 10.0,
    ) -> None:
        self._server_url = (
            server_url
            or os.environ.get(_URL_ENV_VAR, "").strip()
            or _DEFAULT_SERVER_URL
        )
        if nodes is None:
            nodes = self._nodes_from_env()
        self._labels: dict[int, dict[str, str]] = {}
        for entry in nodes:
            node_id, label = self._build_label(entry)
            self._labels[node_id] = label
        self._timeout = timeout
        self._message_ids = itertools.count(1)
        self._devices: dict[str, Device] = {}
        self._snapshots: dict[str, dict[tuple[int, int], Any]] = {}
        self._device_node: dict[str, int] = {}
        self._synced_nodes: set[int] = set()
        self._devices[_CONTROLLER_ID] = Device(
            id=_CONTROLLER_ID,
            name="Matter controller",
            driver="matter",
            room="unknown",
            brand="",
            model="controller service",
            properties={},
            actions=["commission"],
        )
        for node_id, label in sorted(self._labels.items()):
            placeholder_id = f"matter-{node_id}"
            self._devices[placeholder_id] = Device(
                id=placeholder_id,
                name=label.get("name") or f"Matter node {node_id}",
                driver="matter",
                room=label.get("room", "unknown"),
                properties={},
                actions=[],
            )
            self._device_node[placeholder_id] = node_id

    # -- configuration ---------------------------------------------------
    @staticmethod
    def _nodes_from_env() -> list[dict[str, Any]]:
        raw = os.environ.get(_NODES_ENV_VAR, "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DriverNotConfiguredError(
                f"{_NODES_ENV_VAR} is not valid JSON ({exc.msg} at line "
                f"{exc.lineno}). It must be a JSON list of node label "
                "objects like {\"node_id\": 1, \"name\": \"...\", "
                "\"room\": \"...\"}."
            ) from exc
        if not isinstance(parsed, list):
            raise DriverNotConfiguredError(
                f"{_NODES_ENV_VAR} must be a JSON list of node label objects."
            )
        return parsed

    @staticmethod
    def _build_label(entry: dict[str, Any]) -> tuple[int, dict[str, str]]:
        if not isinstance(entry, dict) or entry.get("node_id") is None:
            raise DriverNotConfiguredError(
                "Each Matter node config must be an object with a "
                "node_id (plus optional name / room labels)."
            )
        try:
            node_id = int(entry["node_id"])
        except (TypeError, ValueError):
            raise DriverNotConfiguredError(
                "Matter node config 'node_id' must be an integer."
            ) from None
        label: dict[str, str] = {}
        if entry.get("name"):
            label["name"] = str(entry["name"])
        if entry.get("room"):
            label["room"] = str(entry["room"])
        return node_id, label

    def __repr__(self) -> str:
        return f"MatterDriver(devices={sorted(self._devices)})"

    # -- transport ---------------------------------------------------------
    def _rpc(self, command: str, args: dict[str, Any] | None = None) -> Any:
        return _await(self._rpc_async(command, args or {}))

    async def _rpc_async(self, command: str, args: dict[str, Any]) -> Any:
        websockets = _load_websockets()
        message_id = str(next(self._message_ids))
        request = {"message_id": message_id, "command": command, "args": args}
        try:
            async with websockets.connect(self._server_url) as connection:
                await connection.send(json.dumps(request))
                while True:
                    raw = await asyncio.wait_for(
                        connection.recv(), timeout=self._timeout
                    )
                    try:
                        message = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        raise OmniButlerError(
                            "The Matter controller sent a response that "
                            "is not valid JSON; cannot trust this result."
                        ) from None
                    if not isinstance(message, dict):
                        continue
                    if str(message.get("message_id")) != message_id:
                        continue  # an event or another client's reply
                    if message.get("error_code") is not None or "error" in message:
                        details = message.get("details") or message.get("error")
                        raise _ServerReportedError(
                            f"Matter controller rejected {command!r} "
                            f"(error_code={message.get('error_code')}"
                            f"{': ' + str(details)[:120] if details else ''})."
                        )
                    return message.get("result")
        except OmniButlerError:
            raise
        except Exception as exc:
            # Connection refused, DNS failures, timeouts (TimeoutError is
            # an OSError) and protocol errors all land here; the server
            # URL is not a credential, so it is safe - and useful - to
            # name it. Only the exception type is reported, never a
            # transport message that could echo request data.
            raise OmniButlerError(
                f"Cannot reach the Matter controller at "
                f"{self._server_url} ({type(exc).__name__}). Is "
                "matterjs-server / python-matter-server running and "
                "reachable at that address?"
            ) from exc

    def _fetch_nodes(self) -> list[dict[str, Any]]:
        result = self._rpc("get_nodes")
        if result is None:
            return []
        if not isinstance(result, list):
            raise OmniButlerError(
                "The Matter controller answered get_nodes with an "
                "unexpected shape (expected a list of nodes)."
            )
        return [node for node in result if isinstance(node, dict)]

    # -- node -> device mapping ---------------------------------------------
    @staticmethod
    def _parse_attributes(node: dict[str, Any]) -> dict[tuple[int, int, int], Any]:
        """Flatten a node payload's attributes to (endpoint, cluster,
        attribute) -> value. Keys the driver cannot parse are skipped -
        guessing at a path format would corrupt the mapping."""
        parsed: dict[tuple[int, int, int], Any] = {}
        attributes = node.get("attributes")
        if not isinstance(attributes, dict):
            return parsed
        for key, value in attributes.items():
            parts = str(key).split("/")
            if len(parts) != 3:
                continue
            try:
                endpoint, cluster, attribute = (int(part) for part in parts)
            except ValueError:
                continue
            parsed[(endpoint, cluster, attribute)] = _unwrap(value)
        return parsed

    @staticmethod
    def _properties_for(clusters: set[int]) -> dict[str, Property]:
        properties: dict[str, Property] = {}
        if _CLUSTER_ONOFF in clusters:
            properties["onoff"] = Property(Cap.ONOFF)
        if _CLUSTER_LEVEL in clusters:
            properties["brightness"] = Property(Cap.BRIGHTNESS)
        if _CLUSTER_COLOR in clusters:
            properties["color_temp"] = Property(Cap.COLOR_TEMP)
            properties["color"] = Property(Cap.COLOR)
        if _CLUSTER_TEMP_MEASUREMENT in clusters:
            properties["current_temperature"] = Property(Cap.CURRENT_TEMPERATURE)
        if _CLUSTER_HUMIDITY_MEASUREMENT in clusters:
            # A measurement cluster only reports; the canonical humidity
            # capability is writable (for humidifiers), so pin it down.
            properties["humidity"] = Property(Cap.HUMIDITY, writable=False)
        if _CLUSTER_THERMOSTAT in clusters:
            properties["target_temperature"] = Property(Cap.TARGET_TEMPERATURE)
            properties["current_temperature"] = Property(Cap.CURRENT_TEMPERATURE)
            properties["mode"] = Property(
                Cap.MODE, options=list(_THERMOSTAT_MODE_OPTIONS)
            )
        return properties

    def _state_from_snapshot(
        self, snapshot: dict[tuple[int, int], Any]
    ) -> dict[str, Any]:
        """Translate one endpoint's raw attributes into state values.

        Mapped attributes become canonical values; occupancy and every
        unmapped attribute pass through read-only under their raw keys.
        """
        state: dict[str, Any] = {}
        consumed: set[tuple[int, int]] = set()

        def take(cluster: int, attribute: int) -> Any:
            key = (cluster, attribute)
            if key in snapshot:
                consumed.add(key)
                return snapshot[key]
            return None

        onoff = take(_CLUSTER_ONOFF, 0)
        if onoff is not None:
            state["onoff"] = bool(onoff)
        level = take(_CLUSTER_LEVEL, 0)
        if level is not None:
            state["brightness"] = max(
                0, min(100, round(float(level) / 254 * 100))
            )
        mireds = take(_CLUSTER_COLOR, 7)
        if mireds:
            state["color_temp"] = round(1_000_000 / float(mireds))
        hue = take(_CLUSTER_COLOR, 0)
        saturation = take(_CLUSTER_COLOR, 1)
        if hue is not None and saturation is not None:
            value = float(level) / 254 if level is not None else 1.0
            red, green, blue = colorsys.hsv_to_rgb(
                float(hue) / 254, float(saturation) / 254, value
            )
            state["color"] = "#{:02x}{:02x}{:02x}".format(
                round(red * 255), round(green * 255), round(blue * 255)
            )
        measured = take(_CLUSTER_TEMP_MEASUREMENT, 0)
        if measured is not None:
            state["current_temperature"] = float(measured) / 100
        humidity = take(_CLUSTER_HUMIDITY_MEASUREMENT, 0)
        if humidity is not None:
            state["humidity"] = float(humidity) / 100
        local_temp = take(_CLUSTER_THERMOSTAT, 0)
        if local_temp is not None:
            state["current_temperature"] = float(local_temp) / 100
        setpoint = take(_CLUSTER_THERMOSTAT, 17)
        if setpoint is None:
            setpoint = take(_CLUSTER_THERMOSTAT, 18)
        if setpoint is not None:
            state["target_temperature"] = float(setpoint) / 100
        system_mode = take(_CLUSTER_THERMOSTAT, 28)
        if system_mode is not None and int(system_mode) in _THERMOSTAT_MODE_FROM:
            state["mode"] = _THERMOSTAT_MODE_FROM[int(system_mode)]
        occupancy = take(_CLUSTER_OCCUPANCY, 0)
        if occupancy is not None:
            # Occupancy bitmap: bit 0 is "occupied". No canonical
            # Capability exists yet, so this stays a raw state value.
            state["occupancy"] = (
                bool(occupancy & 1)
                if isinstance(occupancy, int)
                else bool(occupancy)
            )
        for (cluster, attribute), value in sorted(snapshot.items()):
            if (cluster, attribute) not in consumed:
                state[f"matter_c{cluster}_a{attribute}"] = value
        return state

    def _sync_node(self, node: dict[str, Any]) -> list[Device]:
        raw_id = node.get("node_id", node.get("id"))
        if raw_id is None:
            return []
        try:
            node_id = int(raw_id)
        except (TypeError, ValueError):
            return []
        attributes = self._parse_attributes(node)
        label = self._labels.get(node_id, {})
        node_label = attributes.get((0, _CLUSTER_BASIC_INFO, _ATTR_NODE_LABEL))
        base_name = (
            label.get("name")
            or (str(node_label) if node_label else None)
            or f"Matter node {node_id}"
        )
        brand = attributes.get((0, _CLUSTER_BASIC_INFO, _ATTR_VENDOR_NAME))
        model = attributes.get((0, _CLUSTER_BASIC_INFO, _ATTR_PRODUCT_NAME))
        endpoints = sorted({endpoint for endpoint, _, _ in attributes if endpoint > 0})
        devices: list[Device] = []
        for endpoint in endpoints:
            snapshot = {
                (cluster, attribute): value
                for (ep, cluster, attribute), value in attributes.items()
                if ep == endpoint
            }
            if not snapshot:
                continue
            device_id = f"matter-{node_id}-{endpoint}"
            name = base_name
            if len(endpoints) > 1:
                name = f"{base_name} (endpoint {endpoint})"
            device = self._devices.get(device_id)
            if device is None:
                device = Device(
                    id=device_id,
                    name=name,
                    driver="matter",
                    room=label.get("room", "unknown"),
                    brand=str(brand or ""),
                    model=str(model or ""),
                    properties=self._properties_for(
                        {cluster for cluster, _ in snapshot}
                    ),
                    actions=[],
                )
                self._devices[device_id] = device
            else:
                device.name = name
                device.properties = self._properties_for(
                    {cluster for cluster, _ in snapshot}
                )
            self._snapshots[device_id] = snapshot
            self._device_node[device_id] = node_id
            device.state = self._state_from_snapshot(snapshot)
            devices.append(device)
        # The placeholder for this node has done its job.
        placeholder_id = f"matter-{node_id}"
        if devices and placeholder_id in self._devices:
            del self._devices[placeholder_id]
            self._device_node.pop(placeholder_id, None)
        if devices:
            self._synced_nodes.add(node_id)
        return devices

    def _lookup(self, device_id: str) -> Device:
        try:
            return self._devices[device_id]
        except KeyError:
            raise DeviceNotFoundError(
                f"Matter driver has no device {device_id!r}; known: "
                f"{sorted(self._devices)} (run discover() to learn the "
                "controller's nodes)"
            ) from None

    def _sync_device_node(self, device_id: str) -> Device:
        """Refresh one device's snapshot from the controller."""
        device = self._lookup(device_id)
        node_id = self._device_node.get(device_id)
        if node_id is None:
            return device
        for node in self._fetch_nodes():
            raw_id = node.get("node_id", node.get("id"))
            if raw_id is not None and int(raw_id) == node_id:
                self._sync_node(node)
                if device_id in self._devices:
                    return self._devices[device_id]
                # The id was the pre-discovery placeholder for this
                # node; discovery replaced it with endpoint devices.
                known = sorted(
                    known_id
                    for known_id, known_node in self._device_node.items()
                    if known_node == node_id
                )
                raise OmniButlerError(
                    f"{device_id!r} was a placeholder for Matter node "
                    f"{node_id}; its real devices are now: {known}. "
                    "Use one of those ids."
                )
        raise OmniButlerError(
            f"Matter node {node_id} is no longer reported by the "
            "controller; the device may have been removed from the fabric."
        )

    # -- Driver API --------------------------------------------------------
    def discover(self) -> list[Device]:
        for node in self._fetch_nodes():
            self._sync_node(node)
        return self.list_devices()

    def list_devices(self) -> list[Device]:
        return list(self._devices.values())

    def get_state(self, device_id: str) -> dict[str, Any]:
        if device_id == _CONTROLLER_ID:
            self._lookup(device_id)
            return {"server_url": self._server_url}
        _load_websockets()  # the transport dependency gates every operation
        device = self._sync_device_node(device_id)
        return dict(device.state)

    # -- writes --------------------------------------------------------------
    def _send_command(
        self,
        node_id: int,
        endpoint: int,
        cluster: int,
        command_name: str,
        payload: dict[str, Any],
    ) -> None:
        self._rpc(
            "send_device_command",
            {
                "node_id": node_id,
                "endpoint_id": endpoint,
                "cluster_id": cluster,
                "command_name": command_name,
                "payload": payload,
            },
        )

    def _write_attribute(
        self, node_id: int, endpoint: int, cluster: int, attribute: int, value: Any
    ) -> None:
        self._rpc(
            "write_attribute",
            {
                "node_id": node_id,
                "attribute_path": f"{endpoint}/{cluster}/{attribute}",
                "value": value,
            },
        )

    def set_property(
        self, device_id: str, property_name: str, value: Any
    ) -> dict[str, Any]:
        _load_websockets()  # the transport dependency gates every operation
        device = self._lookup(device_id)
        prop = device.property(property_name)
        if not prop.is_writable:
            raise PropertyValidationError(
                f"{property_name}: property is read-only"
            )
        canonical = prop.validate(value)
        device = self._sync_device_node(device_id)
        snapshot = self._snapshots[device_id]
        node_id = self._device_node[device_id]
        endpoint = int(device_id.rsplit("-", 1)[1])

        if property_name == "onoff":
            self._send_command(
                node_id, endpoint, _CLUSTER_ONOFF,
                "On" if canonical else "Off", {},
            )
            snapshot[(_CLUSTER_ONOFF, 0)] = bool(canonical)
        elif property_name == "brightness":
            raw_level = round(float(canonical) * 254 / 100)
            self._send_command(
                node_id, endpoint, _CLUSTER_LEVEL, "MoveToLevel",
                {"level": raw_level, "transitionTime": 0,
                 "optionsMask": 0, "optionsOverride": 0},
            )
            snapshot[(_CLUSTER_LEVEL, 0)] = raw_level
        elif property_name == "color_temp":
            mireds = round(1_000_000 / float(canonical))
            self._send_command(
                node_id, endpoint, _CLUSTER_COLOR, "MoveToColorTemperature",
                {"colorTemperatureMireds": mireds, "transitionTime": 0,
                 "optionsMask": 0, "optionsOverride": 0},
            )
            snapshot[(_CLUSTER_COLOR, 7)] = mireds
        elif property_name == "color":
            text = str(canonical).lstrip("#")
            try:
                red, green, blue = (
                    int(text[i:i + 2], 16) / 255 for i in (0, 2, 4)
                )
            except (ValueError, IndexError):
                raise PropertyValidationError(
                    f"color: expected a hex colour like '#ffcc88', "
                    f"got {canonical!r}"
                ) from None
            hue, saturation, _value = colorsys.rgb_to_hsv(red, green, blue)
            raw_hue = round(hue * 254)
            raw_saturation = round(saturation * 254)
            self._send_command(
                node_id, endpoint, _CLUSTER_COLOR, "MoveToHueAndSaturation",
                {"hue": raw_hue, "saturation": raw_saturation,
                 "transitionTime": 0, "optionsMask": 0, "optionsOverride": 0},
            )
            snapshot[(_CLUSTER_COLOR, 0)] = raw_hue
            snapshot[(_CLUSTER_COLOR, 1)] = raw_saturation
        elif property_name == "target_temperature":
            attribute = 17 if (_CLUSTER_THERMOSTAT, 17) in snapshot else 18
            if (_CLUSTER_THERMOSTAT, attribute) not in snapshot:
                raise OmniButlerError(
                    "This thermostat reports neither a cooling nor a "
                    "heating setpoint; cannot set a target temperature."
                )
            raw_setpoint = int(round(float(canonical) * 100))
            self._write_attribute(
                node_id, endpoint, _CLUSTER_THERMOSTAT, attribute, raw_setpoint
            )
            snapshot[(_CLUSTER_THERMOSTAT, attribute)] = raw_setpoint
        elif property_name == "mode":
            raw_mode = _THERMOSTAT_MODE_TO[str(canonical)]
            self._write_attribute(
                node_id, endpoint, _CLUSTER_THERMOSTAT, 28, raw_mode
            )
            snapshot[(_CLUSTER_THERMOSTAT, 28)] = raw_mode
        else:  # pragma: no cover - validate() already rejects unknown props
            raise OmniButlerError(
                f"Matter driver cannot set {property_name!r}."
            )
        device.state = self._state_from_snapshot(snapshot)
        return {property_name: canonical}

    def call_action(
        self, device_id: str, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        _load_websockets()  # the transport dependency gates every operation
        self._lookup(device_id)
        params = params or {}
        if action == "commission":
            if device_id != _CONTROLLER_ID:
                raise OmniButlerError(
                    "commission is an action of the 'matter-controller' "
                    "device, not of an endpoint device."
                )
            return self._commission(params)
        raise OmniButlerError(
            f"Matter driver does not support action {action!r}; the "
            "controller device supports: commission."
        )

    def _commission(self, params: dict[str, Any]) -> dict[str, Any]:
        code = str(params.get("code") or "").strip()
        if not code:
            raise OmniButlerError(
                "commission needs a 'code' param: the manual pairing "
                "code (or QR payload) printed on the device. The code is "
                "forwarded to the controller service, which performs "
                "the commissioning."
            )
        args: dict[str, Any] = {"code": code}
        if "network_only" in params:
            args["network_only"] = bool(params["network_only"])
        result = self._rpc("commission_with_code", args)
        # Report only what the controller confirmed; this driver did not
        # pair anything itself.
        outcome: dict[str, Any] = {"commission": "forwarded to controller"}
        if isinstance(result, dict) and result.get("node_id") is not None:
            outcome["commissioned_node_id"] = result["node_id"]
        return outcome

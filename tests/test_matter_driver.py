"""Matter driver tests, run against a fake ``websockets`` package and a
fake controller service injected into sys.modules - no real library, no
network, no real matterjs-server / python-matter-server, no hardware.

The fake server mirrors the wire protocol the driver speaks: JSON-RPC
style messages {"message_id", "command", "args"} answered with
{"message_id", "result"} or {"message_id", "error_code", "details"},
and node payloads whose attributes are keyed
"<endpoint>/<cluster>/<attribute>". The exact shapes a real server
uses are an explicitly unverified seam (see the driver docstring);
these tests pin the driver's side of the contract: cluster mapping,
value conversion, write forwarding, commissioning forwarding, and
error classification.
"""

import copy
import json
import sys
import types

import pytest

from omnibutler.core.errors import (
    DeviceNotFoundError,
    DriverNotConfiguredError,
    OmniButlerError,
    PlannedDriverError,
    PropertyValidationError,
)
from omnibutler.drivers.matter import MatterDriver

SERVER_URL = "ws://127.0.0.1:5599/ws"
PAIRING_CODE = "MT:FAKE-PAIRING-PAYLOAD"


def _nodes() -> list[dict]:
    """Two fake nodes: a colour bulb + env sensor, and a thermostat."""
    return [
        {
            "node_id": 1,
            "available": True,
            "attributes": {
                # endpoint 0: BasicInformation
                "0/40/1": "Acme",
                "0/40/3": "Smart Bulb",
                "0/40/5": "Ceiling bulb",
                # endpoint 1: OnOff + LevelControl + ColorControl
                "1/6/0": True,
                "1/8/0": 128,
                "1/768/0": 85,    # hue
                "1/768/1": 128,   # saturation
                "1/768/7": 370,   # colour temperature, mireds
                # endpoint 2: temperature / humidity / occupancy + an
                # unknown cluster (0x801) the driver must pass through
                "2/1026/0": 2350,
                "2/1029/0": 4512,
                "2/1030/0": 1,
                "2/2049/3": "raw-reading",
            },
        },
        {
            "node_id": 2,
            "available": True,
            "attributes": {
                "0/40/1": "ThermoCo",
                "0/40/3": "Wall Thermostat",
                "0/40/5": "Hall thermostat",
                # endpoint 1: Thermostat
                "1/513/0": 2210,   # local temperature 22.10 C
                "1/513/17": 2400,  # cooling setpoint 24.00 C
                "1/513/28": 3,     # SystemMode = cool
            },
        },
    ]


class FakeConnection:
    def __init__(self, server: "FakeMatterServer") -> None:
        self._server = server
        self._replies: list[str] = []

    async def __aenter__(self) -> "FakeConnection":
        if self._server.fail_connect:
            raise ConnectionRefusedError("fake: connection refused")
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        self._replies.append(json.dumps(self._server.handle(message)))

    async def recv(self) -> str:
        return self._replies.pop(0)


class FakeMatterServer:
    """The controller-service side of the wire protocol."""

    def __init__(self) -> None:
        self.nodes = _nodes()
        self.commands: list[dict] = []
        self.fail_connect = False
        self.fail_commands: set[str] = set()
        self.next_commissioned_node = 99

    def handle(self, message: dict) -> dict:
        command = message.get("command")
        args = message.get("args") or {}
        self.commands.append({"command": command, "args": args})
        if command in self.fail_commands:
            return {
                "message_id": message.get("message_id"),
                "error_code": 42,
                "details": "fake server-side failure",
            }
        if command == "get_nodes":
            return {"message_id": message.get("message_id"),
                    "result": copy.deepcopy(self.nodes)}
        if command == "commission_with_code":
            node_id = self.next_commissioned_node
            self.next_commissioned_node += 1
            return {"message_id": message.get("message_id"),
                    "result": {"node_id": node_id}}
        if command == "send_device_command":
            self._apply_device_command(args)
            return {"message_id": message.get("message_id"), "result": {}}
        if command == "write_attribute":
            # Reflect the write into the fake fabric so a later
            # get_nodes shows the new value.
            node_id = args["node_id"]
            endpoint, cluster, attribute = (
                int(part) for part in args["attribute_path"].split("/")
            )
            for node in self.nodes:
                if node["node_id"] == node_id:
                    node["attributes"][
                        f"{endpoint}/{cluster}/{attribute}"
                    ] = args["value"]
            return {"message_id": message.get("message_id"), "result": {}}
        return {"message_id": message.get("message_id"),
                "error_code": 404, "details": f"unknown command {command}"}

    def _apply_device_command(self, args: dict) -> None:
        """Reflect a cluster command into the fake fabric, like a real
        device would, so later get_nodes reads show the new values."""
        for node in self.nodes:
            if node["node_id"] != args["node_id"]:
                continue
            attributes = node["attributes"]
            prefix = f"{args['endpoint_id']}/{args['cluster_id']}"
            name = args["command_name"]
            payload = args.get("payload") or {}
            if name in {"On", "Off"}:
                attributes[f"{prefix}/0"] = name == "On"
            elif name == "MoveToLevel":
                attributes[f"{prefix}/0"] = payload["level"]
            elif name == "MoveToColorTemperature":
                attributes[f"{prefix}/7"] = payload["colorTemperatureMireds"]
            elif name == "MoveToHueAndSaturation":
                attributes[f"{prefix}/0"] = payload["hue"]
                attributes[f"{prefix}/1"] = payload["saturation"]

    def sent(self, command: str) -> list[dict]:
        return [entry["args"] for entry in self.commands
                if entry["command"] == command]


@pytest.fixture()
def fake_server(monkeypatch):
    server = FakeMatterServer()
    module = types.ModuleType("websockets")

    def connect(url):
        server.urls = getattr(server, "urls", []) + [url]
        return FakeConnection(server)

    module.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", module)
    return server


def _driver(**kwargs) -> MatterDriver:
    kwargs.setdefault("server_url", SERVER_URL)
    return MatterDriver(**kwargs)


# -- library / configuration gates ---------------------------------------

def test_missing_websockets_raises_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "websockets", None)  # import fails
    driver = _driver(nodes=[{"node_id": 7, "name": "Porch light",
                             "room": "porch"}])
    # Construction and static listing must not need the library.
    ids = [d.id for d in driver.list_devices()]
    assert ids == ["matter-controller", "matter-7"]
    with pytest.raises(PlannedDriverError) as exc:
        driver.discover()
    assert "websockets" in str(exc.value)
    assert 'pip install "tiybai-omnibutler[matter]"' in str(exc.value)
    with pytest.raises(PlannedDriverError):
        driver.get_state("matter-7")


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("MATTER_SERVER_URL", SERVER_URL)
    monkeypatch.setenv(
        "MATTER_NODES_JSON",
        json.dumps([{"node_id": 3, "name": "Desk lamp", "room": "study"}]),
    )
    driver = MatterDriver()
    placeholder = {d.id: d for d in driver.list_devices()}["matter-3"]
    assert placeholder.name == "Desk lamp"
    assert placeholder.room == "study"
    assert placeholder.properties == {}  # nothing invented pre-discovery


def test_config_bad_env_json():
    with pytest.raises(DriverNotConfiguredError):
        MatterDriver(nodes=[{"name": "no id here"}])
    with pytest.raises(DriverNotConfiguredError):
        MatterDriver(nodes=[{"node_id": "not-a-number"}])


# -- discovery / mapping ----------------------------------------------------

def test_discover_builds_endpoint_devices(fake_server):
    driver = _driver()
    devices = {d.id: d for d in driver.discover()}
    assert set(devices) == {
        "matter-controller", "matter-1-1", "matter-1-2", "matter-2-1",
    }
    bulb = devices["matter-1-1"]
    assert bulb.driver == "matter"
    assert bulb.brand == "Acme" and bulb.model == "Smart Bulb"
    assert set(bulb.properties) == {"onoff", "brightness", "color_temp", "color"}
    sensor = devices["matter-1-2"]
    assert set(sensor.properties) == {"current_temperature", "humidity"}
    assert sensor.property("humidity").is_writable is False
    thermostat = devices["matter-2-1"]
    assert set(thermostat.properties) == {
        "target_temperature", "current_temperature", "mode",
    }
    assert thermostat.property("mode").options == ["off", "auto", "cool", "heat"]
    controller = devices["matter-controller"]
    assert controller.actions == ["commission"]


def test_labels_override_names_and_rooms(fake_server):
    driver = _driver(nodes=[{"node_id": 1, "name": "Main light",
                             "room": "living"}])
    devices = {d.id: d for d in driver.discover()}
    # Node 1 has two device endpoints, so the endpoint suffix appears.
    assert devices["matter-1-1"].name == "Main light (endpoint 1)"
    assert devices["matter-1-1"].room == "living"
    # Node 2 has no static label: the node's own NodeLabel is used.
    assert devices["matter-2-1"].name == "Hall thermostat"


def test_state_mapping(fake_server):
    driver = _driver()
    driver.discover()

    bulb = driver.get_state("matter-1-1")
    assert bulb["onoff"] is True
    assert bulb["brightness"] == round(128 / 254 * 100)
    assert bulb["color_temp"] == round(1_000_000 / 370)
    assert bulb["color"].startswith("#") and len(bulb["color"]) == 7

    sensor = driver.get_state("matter-1-2")
    assert sensor["current_temperature"] == 23.5
    assert sensor["humidity"] == 45.12
    # Occupancy has no canonical Capability: raw state passthrough.
    assert sensor["occupancy"] is True
    # Unknown cluster: passed through untouched, never renamed.
    assert sensor["matter_c2049_a3"] == "raw-reading"

    thermostat = driver.get_state("matter-2-1")
    assert thermostat["current_temperature"] == 22.1
    assert thermostat["target_temperature"] == 24.0
    assert thermostat["mode"] == "cool"


# -- writes -------------------------------------------------------------------

def test_set_onoff_and_brightness_send_commands(fake_server):
    driver = _driver()
    driver.discover()

    assert driver.set_property("matter-1-1", "onoff", False) == {"onoff": False}
    sent = fake_server.sent("send_device_command")
    assert sent[-1] == {
        "node_id": 1, "endpoint_id": 1, "cluster_id": 6,
        "command_name": "Off", "payload": {},
    }

    driver.set_property("matter-1-1", "brightness", 75)
    sent = fake_server.sent("send_device_command")
    assert sent[-1]["command_name"] == "MoveToLevel"
    assert sent[-1]["cluster_id"] == 8
    assert sent[-1]["payload"]["level"] == round(75 * 254 / 100)
    assert driver.get_state("matter-1-1")["brightness"] == 75


def test_set_color_temp_and_color(fake_server):
    driver = _driver()
    driver.discover()

    driver.set_property("matter-1-1", "color_temp", 4000)
    sent = fake_server.sent("send_device_command")
    assert sent[-1]["command_name"] == "MoveToColorTemperature"
    assert sent[-1]["payload"]["colorTemperatureMireds"] == 250

    driver.set_property("matter-1-1", "color", "#ff0000")
    sent = fake_server.sent("send_device_command")
    assert sent[-1]["command_name"] == "MoveToHueAndSaturation"
    assert sent[-1]["payload"]["hue"] == 0
    assert sent[-1]["payload"]["saturation"] == 254
    # Read-back folds in CurrentLevel (128/254), so pure red at that
    # brightness level comes back as #810000, not #ff0000.
    assert driver.get_state("matter-1-1")["color"] == "#810000"


def test_thermostat_writes_are_attribute_writes(fake_server):
    driver = _driver()
    driver.discover()

    assert driver.set_property("matter-2-1", "target_temperature", 26) == {
        "target_temperature": 26
    }
    writes = fake_server.sent("write_attribute")
    assert writes[-1] == {
        "node_id": 2, "attribute_path": "1/513/17", "value": 2600,
    }

    driver.set_property("matter-2-1", "mode", "heat")
    writes = fake_server.sent("write_attribute")
    assert writes[-1] == {
        "node_id": 2, "attribute_path": "1/513/28", "value": 4,
    }
    state = driver.get_state("matter-2-1")
    assert state["target_temperature"] == 26
    assert state["mode"] == "heat"


def test_readonly_and_unknown_properties_rejected(fake_server):
    driver = _driver()
    driver.discover()
    with pytest.raises(PropertyValidationError):
        driver.set_property("matter-1-2", "current_temperature", 20)
    with pytest.raises(PropertyValidationError):
        driver.set_property("matter-1-2", "humidity", 50)
    # occupancy is a raw state value, not a settable property.
    with pytest.raises(PropertyValidationError):
        driver.set_property("matter-1-2", "occupancy", True)
    with pytest.raises(DeviceNotFoundError):
        driver.get_state("matter-9-1")


# -- commissioning --------------------------------------------------------------

def test_commission_forwards_code_to_controller(fake_server):
    driver = _driver()
    result = driver.call_action(
        "matter-controller", "commission", {"code": PAIRING_CODE}
    )
    sent = fake_server.sent("commission_with_code")
    assert sent == [{"code": PAIRING_CODE}]
    assert result["commissioned_node_id"] == 99


def test_commission_needs_a_code_and_the_controller_device(fake_server):
    driver = _driver()
    driver.discover()
    with pytest.raises(OmniButlerError):
        driver.call_action("matter-controller", "commission", {})
    with pytest.raises(OmniButlerError):
        driver.call_action("matter-1-1", "commission", {"code": PAIRING_CODE})
    with pytest.raises(OmniButlerError):
        driver.call_action("matter-controller", "self_destruct", {})
    # Nothing was forwarded for the rejected attempts.
    assert fake_server.sent("commission_with_code") == []


# -- failure classification -------------------------------------------------------

def test_unreachable_controller_is_classified(fake_server):
    fake_server.fail_connect = True
    driver = _driver()
    with pytest.raises(OmniButlerError) as exc:
        driver.discover()
    message = str(exc.value)
    assert "Cannot reach the Matter controller" in message
    assert SERVER_URL in message
    assert "ConnectionRefusedError" in message  # type name only
    with pytest.raises(OmniButlerError):
        driver.call_action(
            "matter-controller", "commission", {"code": PAIRING_CODE}
        )


def test_server_error_is_classified(fake_server):
    fake_server.fail_commands.add("get_nodes")
    driver = _driver()
    with pytest.raises(OmniButlerError) as exc:
        driver.discover()
    assert "error_code=42" in str(exc.value)
    assert "Cannot reach" not in str(exc.value)  # it answered - different failure

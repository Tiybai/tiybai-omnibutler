"""Zigbee2MQTT driver tests, run against a fake MQTT client implementing
the small surface the driver uses (connect / subscribe / publish /
on_message) - no broker, no network, no paho-mqtt needed except in the
one adapter test, which injects a fake paho module into sys.modules.

Zigbee2MQTT message shapes below mirror the documented public ones:
device state JSON on zigbee2mqtt/<name>, retained device list on
zigbee2mqtt/bridge/devices (friendly_name + definition.exposes), plain
online/offline availability topics.
"""

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
from omnibutler.drivers.zigbee2mqtt import Zigbee2MqttDriver

BRIDGE_DEVICES = [
    {
        "ieee_address": "0x00158d0001abcdef",
        "friendly_name": "Coordinator",
        "type": "Coordinator",
    },
    {
        "ieee_address": "0x847127fffe123456",
        "friendly_name": "living_light",
        "type": "Router",
        "definition": {
            "model": "LED1925G6",
            "vendor": "IKEA",
            "exposes": [
                {
                    "type": "light",
                    "features": [
                        {"type": "binary", "name": "state", "property": "state",
                         "access": 7, "value_on": "ON", "value_off": "OFF"},
                        {"type": "numeric", "name": "brightness",
                         "property": "brightness", "access": 7,
                         "value_min": 0, "value_max": 254},
                        {"type": "numeric", "name": "color_temp",
                         "property": "color_temp", "access": 7,
                         "value_min": 150, "value_max": 500},
                    ],
                },
                {"type": "numeric", "name": "linkquality",
                 "property": "linkquality", "access": 1},
            ],
        },
    },
    {
        "ieee_address": "0x00158d0002fedcba",
        "friendly_name": "bedroom_sensor",
        "type": "EndDevice",
        "definition": {
            "model": "WSDCGQ11LM",
            "vendor": "Aqara",
            "exposes": [
                {"type": "numeric", "name": "temperature",
                 "property": "temperature", "access": 1},
                {"type": "numeric", "name": "humidity",
                 "property": "humidity", "access": 1},
                {"type": "numeric", "name": "battery",
                 "property": "battery", "access": 1},
            ],
        },
    },
]


class FakeMqttClient:
    """The driver-facing MQTT surface, with an emit() test helper."""

    def __init__(self):
        self.on_message = None
        self.connected = False
        self.subscriptions: list[str] = []
        self.published: list[tuple[str, str]] = []

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def subscribe(self, topic):
        self.subscriptions.append(topic)

    def publish(self, topic, payload):
        self.published.append((topic, payload))

    def emit(self, topic, payload):
        assert self.on_message is not None, "driver has not connected yet"
        if isinstance(payload, str):
            payload = payload.encode()
        self.on_message(topic, payload)


def _driver(client, **kwargs) -> Zigbee2MqttDriver:
    kwargs.setdefault("devices", [
        {"friendly_name": "living_light", "kind": "light", "room": "living"},
        {"friendly_name": "hall_plug", "kind": "plug"},
        {"friendly_name": "bedroom_thermostat", "kind": "thermostat"},
    ])
    return Zigbee2MqttDriver(client=client, **kwargs)


# -- device list: static config -------------------------------------------

def test_static_config_devices_and_kinds():
    client = FakeMqttClient()
    driver = _driver(client)
    devices = {d.id: d for d in driver.list_devices()}
    assert set(devices) == {
        "z2m-living_light", "z2m-hall_plug", "z2m-bedroom_thermostat"
    }
    light = devices["z2m-living_light"]
    assert light.driver == "zigbee2mqtt"
    assert light.room == "living"
    assert set(light.properties) == {"onoff", "brightness", "color_temp"}
    assert set(devices["z2m-hall_plug"].properties) == {"onoff", "power", "energy"}
    thermostat = devices["z2m-bedroom_thermostat"]
    assert set(thermostat.properties) == {"current_temperature", "target_temperature"}


def test_explicit_properties_override_kind():
    driver = Zigbee2MqttDriver(
        devices=[{"friendly_name": "desk", "properties": ["onoff"]}],
        client=FakeMqttClient(),
    )
    (device,) = driver.list_devices()
    assert device.id == "z2m-desk"
    assert set(device.properties) == {"onoff"}


def test_config_validation_errors():
    with pytest.raises(DriverNotConfiguredError):
        Zigbee2MqttDriver(devices=[{"kind": "light"}], client=FakeMqttClient())
    with pytest.raises(DriverNotConfiguredError):
        Zigbee2MqttDriver(
            devices=[{"friendly_name": "x", "kind": "sauna"}],
            client=FakeMqttClient(),
        )
    with pytest.raises(DriverNotConfiguredError):
        Zigbee2MqttDriver(
            devices=[{"friendly_name": "x", "properties": ["warp_drive"]}],
            client=FakeMqttClient(),
        )


def test_devices_from_env(monkeypatch):
    monkeypatch.setenv(
        "Z2M_DEVICES_JSON",
        json.dumps([{"friendly_name": "porch", "kind": "contact", "room": "hall"}]),
    )
    driver = Zigbee2MqttDriver(client=FakeMqttClient())
    (device,) = driver.list_devices()
    assert device.id == "z2m-porch"
    assert set(device.properties) == {"contact", "battery"}


def test_devices_from_env_invalid_json(monkeypatch):
    monkeypatch.setenv("Z2M_DEVICES_JSON", "[{not json")
    with pytest.raises(DriverNotConfiguredError):
        Zigbee2MqttDriver(client=FakeMqttClient())


# -- device list: bridge/devices message -----------------------------------

def test_discover_subscribes_and_adopts_bridge_devices():
    client = FakeMqttClient()
    driver = Zigbee2MqttDriver(devices=[], client=client)
    assert driver.discover() == []
    assert client.connected
    assert "zigbee2mqtt/bridge/devices" in client.subscriptions
    assert "zigbee2mqtt/+" in client.subscriptions
    assert "zigbee2mqtt/+/availability" in client.subscriptions

    client.emit("zigbee2mqtt/bridge/devices", json.dumps(BRIDGE_DEVICES))
    devices = {d.id: d for d in driver.list_devices()}
    # The coordinator itself is not a controllable device.
    assert set(devices) == {"z2m-living_light", "z2m-bedroom_sensor"}

    light = devices["z2m-living_light"]
    assert light.brand == "IKEA"
    assert light.model == "LED1925G6"
    assert set(light.properties) == {"onoff", "brightness", "color_temp"}
    # Expose bounds are mireds (150..500); canonical bounds are kelvin,
    # axis flipped: 2000..6667 K.
    color_temp = light.properties["color_temp"]
    assert color_temp.effective_minimum == 2000
    assert color_temp.effective_maximum == 6667
    assert color_temp.is_writable

    sensor = devices["z2m-bedroom_sensor"]
    assert set(sensor.properties) == {"current_temperature", "humidity", "battery"}
    assert not sensor.properties["battery"].is_writable
    assert not sensor.properties["humidity"].is_writable


def test_bridge_list_does_not_override_configured_properties():
    client = FakeMqttClient()
    driver = Zigbee2MqttDriver(
        devices=[{"friendly_name": "living_light", "properties": ["onoff"]}],
        client=client,
    )
    driver.discover()
    client.emit("zigbee2mqtt/bridge/devices", json.dumps(BRIDGE_DEVICES))
    (light,) = [d for d in driver.list_devices() if d.id == "z2m-living_light"]
    assert set(light.properties) == {"onoff"}  # user's pinned set wins
    assert light.model == "LED1925G6"  # ... but facts still merge in


# -- inbound state ----------------------------------------------------------

def test_state_message_updates_cache_with_conversions():
    client = FakeMqttClient()
    driver = _driver(client)
    driver.discover()
    client.emit(
        "zigbee2mqtt/living_light",
        json.dumps({
            "state": "ON",
            "brightness": 254,
            "color_temp": 250,      # mired -> 4000 K
            "linkquality": 92,      # no canonical counterpart: ignored
            "power": 9.5,           # not a light property: ignored
        }),
    )
    state = driver.get_state("z2m-living_light")
    assert state == {"onoff": True, "brightness": 100, "color_temp": 4000}


def test_sensor_state_mapping():
    client = FakeMqttClient()
    driver = Zigbee2MqttDriver(
        devices=[
            {"friendly_name": "climate", "kind": "sensor"},
            {"friendly_name": "pir", "kind": "motion"},
            {"friendly_name": "door", "kind": "contact"},
        ],
        client=client,
    )
    driver.discover()
    client.emit("zigbee2mqtt/climate",
                json.dumps({"temperature": 21.4, "humidity": 47, "battery": 86}))
    client.emit("zigbee2mqtt/pir", json.dumps({"occupancy": True, "battery": 55}))
    client.emit("zigbee2mqtt/door", json.dumps({"contact": False, "battery": 90}))
    assert driver.get_state("z2m-climate") == {
        "current_temperature": 21.4, "humidity": 47.0, "battery": 86.0
    }
    assert driver.get_state("z2m-pir") == {"motion": True, "battery": 55.0}
    assert driver.get_state("z2m-door") == {"contact": False, "battery": 90.0}


def test_thermostat_state_mapping():
    client = FakeMqttClient()
    driver = _driver(client)
    driver.discover()
    client.emit(
        "zigbee2mqtt/bedroom_thermostat",
        json.dumps({"local_temperature": 20.5, "current_heating_setpoint": 22}),
    )
    assert driver.get_state("z2m-bedroom_thermostat") == {
        "current_temperature": 20.5, "target_temperature": 22.0
    }


def test_state_for_unknown_friendly_name_is_ignored():
    client = FakeMqttClient()
    driver = _driver(client)
    driver.discover()
    client.emit("zigbee2mqtt/not_a_device", json.dumps({"state": "ON"}))
    assert driver.get_state("z2m-living_light") == {}


def test_availability_updates_online_flag():
    client = FakeMqttClient()
    driver = _driver(client)
    driver.discover()
    device = driver.list_devices()[0]
    client.emit("zigbee2mqtt/living_light/availability", "offline")
    assert device.online is False
    client.emit("zigbee2mqtt/living_light/availability",
                json.dumps({"state": "online"}))
    assert device.online is True


def test_get_state_unknown_device():
    driver = _driver(FakeMqttClient())
    with pytest.raises(DeviceNotFoundError):
        driver.get_state("z2m-nope")


# -- outbound control ---------------------------------------------------------

def test_set_property_publishes_set_topic_and_payload():
    client = FakeMqttClient()
    driver = _driver(client)
    driver.discover()
    client.published.clear()

    assert driver.set_property("z2m-living_light", "onoff", True) == {"onoff": True}
    assert client.published[-1] == (
        "zigbee2mqtt/living_light/set", json.dumps({"state": "ON"})
    )

    driver.set_property("z2m-living_light", "brightness", 50)
    assert client.published[-1] == (
        "zigbee2mqtt/living_light/set", json.dumps({"brightness": 127})
    )

    driver.set_property("z2m-living_light", "color_temp", 4000)
    assert client.published[-1] == (
        "zigbee2mqtt/living_light/set", json.dumps({"color_temp": 250})
    )

    driver.set_property("z2m-bedroom_thermostat", "target_temperature", 21.5)
    assert client.published[-1] == (
        "zigbee2mqtt/bedroom_thermostat/set",
        json.dumps({"current_heating_setpoint": 21.5}),
    )

    # Optimistic cache update (Z2M's state echo would confirm it).
    assert driver.get_state("z2m-living_light")["brightness"] == 50


def test_set_unknown_field_is_refused_not_forwarded():
    client = FakeMqttClient()
    driver = Zigbee2MqttDriver(
        devices=[{"friendly_name": "rgb", "properties": ["onoff", "color"]}],
        client=client,
    )
    driver.discover()
    published_before = list(client.published)
    with pytest.raises(OmniButlerError) as exc:
        driver.set_property("z2m-rgb", "color", "#ffcc88")
    assert "refuses" in str(exc.value)
    assert client.published == published_before  # nothing was sent


def test_set_read_only_property_rejected():
    client = FakeMqttClient()
    driver = Zigbee2MqttDriver(
        devices=[{"friendly_name": "climate", "kind": "sensor"}], client=client
    )
    driver.discover()
    with pytest.raises(PropertyValidationError):
        driver.set_property("z2m-climate", "battery", 50)
    with pytest.raises(PropertyValidationError):
        driver.set_property("z2m-climate", "humidity", 40)


def test_actions_turn_on_off_toggle():
    client = FakeMqttClient()
    driver = _driver(client)
    driver.discover()
    client.published.clear()

    driver.call_action("z2m-hall_plug", "turn_on", {})
    assert client.published[-1] == (
        "zigbee2mqtt/hall_plug/set", json.dumps({"state": "ON"})
    )
    driver.call_action("z2m-hall_plug", "toggle", {})
    assert client.published[-1] == (
        "zigbee2mqtt/hall_plug/set", json.dumps({"state": "OFF"})
    )
    with pytest.raises(OmniButlerError):
        driver.call_action("z2m-hall_plug", "self_destruct", {})


# -- transport gating ---------------------------------------------------------

def test_missing_library_raises_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "paho", None)
    monkeypatch.setitem(sys.modules, "paho.mqtt", None)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", None)
    # Construction and listing must not need the library.
    driver = Zigbee2MqttDriver(
        devices=[{"friendly_name": "living_light", "kind": "light"}],
        mqtt_url="mqtt://192.168.1.10:1883",
    )
    assert [d.id for d in driver.list_devices()] == ["z2m-living_light"]
    with pytest.raises(PlannedDriverError) as exc:
        driver.get_state("z2m-living_light")
    assert "paho-mqtt" in str(exc.value)
    assert "pip install" in str(exc.value)
    assert "tiybai-omnibutler[zigbee]" in str(exc.value)


def test_no_broker_url_and_no_client_is_a_config_error(monkeypatch):
    # Make paho "available" so the flow gets past the library gate and
    # reaches the configuration check.
    module = types.ModuleType("paho.mqtt.client")
    module.Client = object
    monkeypatch.setitem(sys.modules, "paho", types.ModuleType("paho"))
    monkeypatch.setitem(sys.modules, "paho.mqtt", types.ModuleType("paho.mqtt"))
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", module)
    driver = Zigbee2MqttDriver(devices=[{"friendly_name": "x", "kind": "light"}])
    with pytest.raises(DriverNotConfiguredError) as exc:
        driver.discover()
    assert "Z2M_MQTT_URL" in str(exc.value)


# -- paho adapter ---------------------------------------------------------------

class FakePahoClient:
    instances: list = []

    def __init__(self, *args, **kwargs):
        self.on_message = None
        self.subscriptions: list[str] = []
        self.published: list[tuple[str, str]] = []
        self.connected_to = None
        self.auth = None
        self.tls = False
        self.loop_started = False
        type(self).instances.append(self)

    def username_pw_set(self, username, password=None):
        self.auth = (username, password)

    def tls_set(self):
        self.tls = True

    def connect(self, host, port):
        self.connected_to = (host, port)

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_started = False

    def disconnect(self):
        pass

    def subscribe(self, topic):
        self.subscriptions.append(topic)

    def publish(self, topic, payload):
        self.published.append((topic, payload))


@pytest.fixture()
def fake_paho(monkeypatch):
    module = types.ModuleType("paho.mqtt.client")
    module.Client = FakePahoClient
    module.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    monkeypatch.setitem(sys.modules, "paho", types.ModuleType("paho"))
    monkeypatch.setitem(sys.modules, "paho.mqtt", types.ModuleType("paho.mqtt"))
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", module)
    FakePahoClient.instances = []
    return module


def test_paho_adapter_end_to_end(fake_paho):
    driver = Zigbee2MqttDriver(
        devices=[{"friendly_name": "living_light", "kind": "light"}],
        mqtt_url="mqtt://user:secret@broker.local:1884",
    )
    driver.discover()
    (client,) = FakePahoClient.instances
    assert client.connected_to == ("broker.local", 1884)
    assert client.auth == ("user", "secret")
    assert client.loop_started
    assert "zigbee2mqtt/bridge/devices" in client.subscriptions

    # Inbound: the adapter forwards paho messages into the driver cache.
    message = types.SimpleNamespace(
        topic="zigbee2mqtt/living_light",
        payload=json.dumps({"state": "OFF"}).encode(),
    )
    client.on_message(client, None, message)
    assert driver.get_state("z2m-living_light") == {"onoff": False}

    # Outbound goes through the same client.
    driver.set_property("z2m-living_light", "onoff", True)
    assert client.published[-1] == (
        "zigbee2mqtt/living_light/set", json.dumps({"state": "ON"})
    )

    # The broker password from the URL never surfaces in repr.
    assert "secret" not in repr(driver)
    assert "broker.local" not in repr(driver)

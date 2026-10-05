"""Broadlink driver tests, run against a fake python-broadlink module
injected into sys.modules - no real library, no network, no hardware.

The fake mirrors the documented python-broadlink surface the driver uses:
gendevice / hello / discover, auth, enter_learning, check_data (raising
while nothing has been learned), send_data, check_sensors, and the RF
sweep calls of Pro models.
"""

import base64
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
from omnibutler.drivers.broadlink import BroadlinkDriver

MAC = "aa:bb:cc:dd:ee:ff"
PACKET = b"\x26\x00\x01\x02"


class FakeBlaster:
    instances: list = []

    def __init__(self, host=("192.168.1.60", 80), mac=b"\xaa\xbb\xcc\xdd\xee\xff",
                 devtype=0x520B):
        self.host = host
        self.mac = mac
        self.devtype = devtype
        self.name = "Fake RM4 Pro"
        self.calls: list = []
        self.sent: list = []
        self.pending_code = None
        self.sensors = {"temperature": 24.5, "humidity": 55.0}
        self.authenticated = False
        type(self).instances.append(self)

    def get_type(self):
        return "RM4 Pro"

    def auth(self):
        self.authenticated = True
        self.calls.append(("auth",))
        return True

    def enter_learning(self):
        self.calls.append(("enter_learning",))

    def check_data(self):
        if self.pending_code is None:
            raise RuntimeError("nothing learned yet")
        data, self.pending_code = self.pending_code, None
        return data

    def send_data(self, packet):
        self.sent.append(bytes(packet))
        self.calls.append(("send_data", bytes(packet)))

    def check_sensors(self):
        return dict(self.sensors)

    def sweep_frequency(self):
        self.calls.append(("sweep_frequency",))

    def check_frequency(self):
        return True

    def find_rf_packet(self):
        self.calls.append(("find_rf_packet",))


@pytest.fixture()
def fake_broadlink(monkeypatch):
    module = types.ModuleType("broadlink")
    module.hello_calls = []
    module.discover_result = []

    def gendevice(dev_type, host, mac):
        return FakeBlaster(host=host, mac=mac, devtype=dev_type)

    def hello(host, port=80):
        module.hello_calls.append((host, port))
        return FakeBlaster(host=(host, port))

    def discover(timeout=5):
        return list(module.discover_result)

    module.gendevice = gendevice
    module.hello = hello
    module.discover = discover
    FakeBlaster.instances = []
    monkeypatch.setitem(sys.modules, "broadlink", module)
    return module


def _driver(tmp_path, **overrides) -> BroadlinkDriver:
    config = {"id": "blaster", "host": "192.168.1.60", "mac": MAC,
              "type": "0x520b", "sensors": True, "room": "living"}
    config.update(overrides)
    return BroadlinkDriver(
        devices=[config], codes_file=tmp_path / "codes.json"
    )


def test_missing_library_raises_clear_error(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "broadlink", None)  # import now fails
    driver = _driver(tmp_path)  # construction must not need the library
    assert [d.id for d in driver.list_devices()] == ["blaster"]
    with pytest.raises(PlannedDriverError) as exc:
        driver.get_state("blaster")
    assert "python-broadlink" in str(exc.value)
    assert "pip install" in str(exc.value)
    with pytest.raises(PlannedDriverError):
        driver.call_action("blaster", "send_code", {"code": "JgABAg=="})


def test_config_from_env(monkeypatch, fake_broadlink, tmp_path):
    payload = [{"host": "192.168.1.99", "mac": "11:22:33:44:55:66"}]
    monkeypatch.setenv("BROADLINK_DEVICES_JSON", json.dumps(payload))
    driver = BroadlinkDriver(codes_file=tmp_path / "codes.json")
    devices = driver.list_devices()
    assert len(devices) == 1
    assert devices[0].id == "broadlink-112233445566"
    assert devices[0].driver == "broadlink"
    assert devices[0].properties == {}  # no sensors configured, no fake props
    assert devices[0].actions == ["learn_code", "send_code"]


def test_config_missing_field_names_only(tmp_path):
    with pytest.raises(DriverNotConfiguredError) as exc:
        BroadlinkDriver(devices=[{"host": "192.168.1.1"}],
                        codes_file=tmp_path / "codes.json")
    assert "mac" in str(exc.value)


def test_discover_finds_unconfigured_hub(fake_broadlink, tmp_path):
    found = FakeBlaster(host=("192.168.1.61", 80),
                        mac=b"\x11\x22\x33\x44\x55\x66")
    fake_broadlink.discover_result = [found]
    driver = BroadlinkDriver(devices=[], codes_file=tmp_path / "codes.json")
    devices = driver.discover()
    assert [d.id for d in devices] == ["broadlink-112233445566"]
    assert devices[0].model == "RM4 Pro"
    # The discovered unit is addressable: sensor-less, state is empty.
    assert driver.get_state("broadlink-112233445566") == {}
    # ...and actions reach the very object discovery returned (authenticated).
    driver.call_action(
        "broadlink-112233445566", "send_code",
        {"code": base64.b64encode(PACKET).decode()},
    )
    assert found.authenticated
    assert found.sent == [PACKET]


def test_sensor_state_from_hts2(fake_broadlink, tmp_path):
    driver = _driver(tmp_path)
    state = driver.get_state("blaster")
    assert state == {"current_temperature": 24.5, "humidity": 55.0}
    fake = FakeBlaster.instances[-1]
    assert fake.authenticated
    assert fake.devtype == 0x520B  # parsed from the "0x520b" config string


def test_no_fake_properties_and_read_only_sensors(fake_broadlink, tmp_path):
    driver = _driver(tmp_path)
    with pytest.raises(PropertyValidationError):
        driver.set_property("blaster", "onoff", True)  # hub has no switch
    with pytest.raises(PropertyValidationError):
        driver.set_property("blaster", "current_temperature", 20)
    with pytest.raises(DeviceNotFoundError):
        driver.get_state("nope")


def test_connect_via_hello_without_type(fake_broadlink, tmp_path):
    driver = _driver(tmp_path, type=None)
    driver.get_state("blaster")
    assert fake_broadlink.hello_calls == [("192.168.1.60", 80)]


def test_learn_then_send_roundtrip(fake_broadlink, tmp_path):
    driver = _driver(tmp_path)
    driver.get_state("blaster")  # establish the connection
    fake = FakeBlaster.instances[-1]
    fake.pending_code = PACKET

    result = driver.call_action(
        "blaster", "learn_code", {"name": "ac_on", "timeout": 5}
    )
    assert result["bytes"] == len(PACKET)
    assert ("enter_learning",) in fake.calls

    stored = json.loads((tmp_path / "codes.json").read_text())
    assert stored["blaster"]["ac_on"]["code"] == base64.b64encode(PACKET).decode()
    assert stored["blaster"]["ac_on"]["kind"] == "ir"

    result = driver.call_action("blaster", "send_code", {"name": "ac_on"})
    assert result["send_code"] == "ac_on"
    assert fake.sent == [PACKET]

    # Inline codes and repeats work too.
    driver.call_action(
        "blaster", "send_code",
        {"code": base64.b64encode(PACKET).decode(), "repeat": 2},
    )
    assert fake.sent == [PACKET, PACKET, PACKET]


def test_learn_rf_uses_sweep(fake_broadlink, tmp_path):
    driver = _driver(tmp_path)
    driver.get_state("blaster")
    fake = FakeBlaster.instances[-1]
    fake.pending_code = b"\xb2\x00\x03"
    result = driver.call_action(
        "blaster", "learn_code", {"name": "gate", "kind": "rf", "timeout": 5}
    )
    assert result["kind"] == "rf"
    assert ("sweep_frequency",) in fake.calls
    assert ("find_rf_packet",) in fake.calls
    stored = json.loads((tmp_path / "codes.json").read_text())
    assert stored["blaster"]["gate"]["kind"] == "rf"


def test_learn_timeout_and_unknown_code(fake_broadlink, tmp_path):
    driver = _driver(tmp_path)
    driver.get_state("blaster")
    with pytest.raises(OmniButlerError) as exc:
        driver.call_action(
            "blaster", "learn_code", {"name": "never", "timeout": 0.3}
        )
    assert "No ir code received" in str(exc.value)
    with pytest.raises(OmniButlerError) as exc:
        driver.call_action("blaster", "send_code", {"name": "ghost"})
    assert "ghost" in str(exc.value)
    with pytest.raises(OmniButlerError):
        driver.call_action("blaster", "send_code", {"code": "!!!not-b64!!!"})
    with pytest.raises(OmniButlerError):
        driver.call_action("blaster", "explode", {})

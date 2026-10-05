"""Tests for the miIO / MIoT driver (v0.2).

The fake device below is an independent oracle: it implements the wire
format straight from docs/specs/miio-protocol.md (hello packet, MD5 key
derivation, AES-128-CBC, header checksum) without importing any of the
driver's codec helpers, so a driver bug cannot cancel out against itself.
"""

import contextlib
import hashlib
import json
import socket
import struct
import threading
import time

import pytest

from omnibutler.core.errors import (
    DeviceNotFoundError,
    DriverNotConfiguredError,
    OmniButlerError,
    PropertyValidationError,
)
from omnibutler.drivers.miio import MiioDriver, parse_hello

TOKEN = "00112233445566778899aabbccddeeff"
OTHER_TOKEN = "ffeeddccbbaa99887766554433221100"


def _pad(data: bytes) -> bytes:
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def _unpad(data: bytes) -> bytes:
    return data[: -data[-1]]


def _crypt(token: bytes, data: bytes, encrypt: bool) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.md5(token).digest()
    iv = hashlib.md5(key + token).digest()
    ctx = Cipher(algorithms.AES(key), modes.CBC(iv))
    op = ctx.encryptor() if encrypt else ctx.decryptor()
    return op.update(data) + op.finalize()


class FakeMiioDevice(threading.Thread):
    """A miIO device on 127.0.0.1 with an ephemeral port, per the spec.

    Sandbox note: the environment these tests run in refuses unconnected
    UDP sends, so replies cannot leave via ``sendto`` on the listening
    socket. Instead each peer gets a connected reply socket bound to the
    same port (SO_REUSEADDR), which also takes over that peer's later
    packets - the wire behaviour a real device shows is unchanged.
    """

    def __init__(self, token_hex: str, device_id: int, store: dict, model: str):
        super().__init__(daemon=True)
        self.token = bytes.fromhex(token_hex)
        self.device_id = device_id
        self.store = dict(store)
        self.model = model
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self._boot = time.monotonic()
        self._running = True
        self._peers: dict[tuple, socket.socket] = {}

    def _stamp(self) -> int:
        return 4242 + int(time.monotonic() - self._boot)

    def run(self) -> None:
        while self._running:
            try:
                data, addr = self.sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            self._dispatch(data, addr)

    def _peer_socket(self, addr) -> socket.socket:
        peer = self._peers.get(addr)
        if peer is None:
            peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            peer.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            peer.bind(("127.0.0.1", self.port))
            peer.connect(addr)
            self._peers[addr] = peer
            threading.Thread(target=self._peer_loop, args=(peer,), daemon=True).start()
        return peer

    def _peer_loop(self, peer: socket.socket) -> None:
        while self._running:
            try:
                data = peer.recv(4096)
            except OSError:
                return
            reply = self._handle(data)
            if reply is not None:
                try:
                    peer.send(reply)
                except OSError:
                    return

    def _dispatch(self, data: bytes, addr) -> None:
        reply = self._handle(data)
        if reply is None:
            return
        with contextlib.suppress(OSError):
            self._peer_socket(addr).send(reply)

    def stop(self) -> None:
        self._running = False
        for peer in self._peers.values():
            peer.close()
        self.sock.close()

    def _handle(self, data: bytes):
        # Hello probe: 32 bytes, length field 32, everything after the
        # length field 0xFF (spec: docs/specs/miio-protocol.md).
        if len(data) == 32 and data[4:] == b"\xff" * 28:
            header = struct.pack(">HHIII", 0x2131, 32, 0, self.device_id, self._stamp())
            return header + b"\x00" * 16
        if len(data) <= 32:
            return None
        check = data[16:32]
        ciphertext = data[32:]
        if hashlib.md5(data[:16] + self.token + ciphertext).digest() != check:
            return None  # wrong token: a real device drops the packet
        request = json.loads(_unpad(_crypt(self.token, ciphertext, False)))
        method, params = request["method"], request.get("params")
        if method == "miIO.info":
            result: object = {"model": self.model, "fw_ver": "1.0.0"}
        elif method == "get_properties":
            result = [
                {
                    "did": p["did"], "siid": p["siid"], "piid": p["piid"],
                    "value": self.store.get((p["siid"], p["piid"])),
                    "code": 0 if (p["siid"], p["piid"]) in self.store else -4003,
                }
                for p in params
            ]
        elif method == "set_properties":
            result = []
            for p in params:
                self.store[(p["siid"], p["piid"])] = p["value"]
                result.append({"did": p["did"], "siid": p["siid"],
                               "piid": p["piid"], "code": 0})
        else:
            return self._reply(request["id"], error={"code": -32601,
                                                     "message": "method not found"})
        return self._reply(request["id"], result=result)

    def _reply(self, request_id: int, result=None, error=None) -> bytes:
        document = {"id": request_id}
        if error is not None:
            document["error"] = error
        else:
            document["result"] = result
        ciphertext = _crypt(self.token, _pad(json.dumps(document).encode()), True)
        header = struct.pack(
            ">HHIII", 0x2131, 32 + len(ciphertext), 0, self.device_id, self._stamp()
        )
        check = hashlib.md5(header + self.token + ciphertext).digest()
        return header + check + ciphertext


@pytest.fixture()
def fake_ac():
    device = FakeMiioDevice(
        TOKEN, device_id=267512345,
        store={(2, 1): False, (2, 2): 1, (2, 3): 26, (3, 1): 1},
        model="xiaomi.aircondition.test",
    )
    device.start()
    yield device
    device.stop()


@pytest.fixture()
def fake_purifier():
    device = FakeMiioDevice(
        TOKEN, device_id=98765432,
        store={(2, 1): True, (2, 2): 0, (3, 1): 35, (4, 1): 82},
        model="zhimi.airpurifier.test",
    )
    device.start()
    yield device
    device.stop()


def _driver_for(device: FakeMiioDevice, config_id: str, model: str, **extra):
    config = {
        "id": config_id, "host": "127.0.0.1", "token": TOKEN,
        "model": model, "name": config_id, "room": "test_room",
    }
    config.update(extra)
    return MiioDriver(
        devices=[config], timeout=2.0, discover_timeout=1.0,
        port=device.port, broadcast_addr="127.0.0.1",
    )


# -- hello / discovery ---------------------------------------------------------

def test_parse_hello():
    packet = struct.pack(">HHIII", 0x2131, 32, 0, 1234, 999) + b"\x00" * 16
    assert parse_hello(packet) == (1234, 999)
    assert parse_hello(b"\x00" * 32) is None
    assert parse_hello(b"short") is None


def test_discovery_finds_configured_device(fake_ac):
    driver = _driver_for(fake_ac, "living_ac", "xiaomi.aircondition.test")
    found = driver.discover()
    assert [d.id for d in found] == ["living_ac"]
    assert found[0].model == "xiaomi.aircondition.test"
    assert set(found[0].properties) == {
        "onoff", "mode", "target_temperature", "fan_speed"}


# -- air conditioner: full chain ----------------------------------------------

def test_ac_state_and_control_roundtrip(fake_ac):
    driver = _driver_for(fake_ac, "living_ac", "xiaomi.aircondition.test")
    state = driver.get_state("living_ac")
    assert state == {"onoff": False, "mode": "cool",
                     "target_temperature": 26, "fan_speed": 25}

    assert driver.set_property("living_ac", "mode", "heat") == {"mode": "heat"}
    assert driver.set_property("living_ac", "target_temperature", 22) == {
        "target_temperature": 22}
    assert driver.set_property("living_ac", "fan_speed", 100) == {"fan_speed": 100}
    assert driver.call_action("living_ac", "turn_on", {}) == {"onoff": True}

    state = driver.get_state("living_ac")
    assert state == {"onoff": True, "mode": "heat",
                     "target_temperature": 22, "fan_speed": 100}
    # The wire values behind the canonical ones are the MIoT codes.
    assert fake_ac.store[(2, 2)] == 3
    assert fake_ac.store[(3, 1)] == 4


def test_ac_rejects_unknown_mode_and_unknown_device(fake_ac):
    driver = _driver_for(fake_ac, "living_ac", "xiaomi.aircondition.test")
    with pytest.raises(PropertyValidationError):
        driver.set_property("living_ac", "mode", "sauna")
    with pytest.raises(DeviceNotFoundError):
        driver.get_state("no_such_device")
    with pytest.raises(PropertyValidationError):
        driver.call_action("living_ac", "self_destruct", {})


# -- air purifier ----------------------------------------------------------------

def test_purifier_state_includes_pm25_and_filter_life(fake_purifier):
    driver = _driver_for(fake_purifier, "purifier", "zhimi.airpurifier.test")
    state = driver.get_state("purifier")
    assert state == {"onoff": True, "mode": "auto", "pm25": 35, "filter_life": 82}
    assert driver.set_property("purifier", "mode", "silent") == {"mode": "silent"}
    assert driver.get_state("purifier")["mode"] == "silent"
    with pytest.raises(PropertyValidationError):
        driver.set_property("purifier", "pm25", 10)  # read-only sensor


# -- failure behaviour -------------------------------------------------------------

def test_wrong_token_fails_loudly(fake_ac):
    driver = _driver_for(fake_ac, "living_ac", "xiaomi.aircondition.test")
    driver._configs["living_ac"].token = OTHER_TOKEN
    with pytest.raises(OmniButlerError):
        driver.get_state("living_ac")


def test_malformed_token_is_a_config_error_not_a_leak(fake_ac):
    driver = _driver_for(fake_ac, "living_ac", "xiaomi.aircondition.test")
    driver._configs["living_ac"].token = "not-hex-at-all"
    with pytest.raises(DriverNotConfiguredError) as exc:
        driver.get_state("living_ac")
    assert "not-hex-at-all" not in str(exc.value)


def test_token_never_in_repr():
    driver = MiioDriver(devices=[{
        "id": "d1", "host": "192.0.2.1", "token": TOKEN,
        "model": "xiaomi.aircondition.test",
    }])
    assert TOKEN not in repr(driver._configs["d1"])


# -- configuration -------------------------------------------------------------------

def test_env_configuration(monkeypatch):
    monkeypatch.delenv("MIIO_HOST", raising=False)
    monkeypatch.delenv("MIIO_TOKEN", raising=False)
    monkeypatch.setenv("MIIO_DEVICES", json.dumps([{
        "id": "env_ac", "host": "192.0.2.10", "token": TOKEN,
        "model": "xiaomi.aircondition.test", "room": "bedroom",
    }]))
    driver = MiioDriver()
    devices = driver.list_devices()
    assert [d.id for d in devices] == ["env_ac"]
    assert devices[0].room == "bedroom"


def test_mapping_override_per_device(fake_ac):
    driver = _driver_for(
        fake_ac, "living_ac", "xiaomi.aircondition.test",
        mapping={"target_temperature": {"siid": 2, "piid": 3}},
    )
    assert driver.get_state("living_ac")["target_temperature"] == 26


def test_unconfigured_driver_is_empty(monkeypatch):
    monkeypatch.delenv("MIIO_DEVICES", raising=False)
    monkeypatch.delenv("MIIO_HOST", raising=False)
    monkeypatch.delenv("MIIO_TOKEN", raising=False)
    driver = MiioDriver()
    assert driver.configured is False
    assert driver.list_devices() == []

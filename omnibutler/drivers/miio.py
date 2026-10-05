"""Xiaomi miIO / MIoT driver - local control over UDP, implemented in v0.2.

Implemented from the clean-room specification in ``docs/specs/miio-protocol.md``
(see ``docs/provenance/2026-10-05-miio-driver.md``). The driver speaks the
miIO local protocol directly: UDP discovery and hello handshake on port
54321, AES-128-CBC encrypted JSON payloads keyed by the per-device token,
and MIoT ``get_properties`` / ``set_properties`` addressing for device
functions.

Configuration - tokens are secrets and are only ever read from the
operator's own configuration, never hard-coded and never logged:

* Constructor: ``MiioDriver(devices=[{...}, ...])`` where each entry has
  ``id`` (canonical OmniButler device id), ``host`` (device IP), ``token``
  (32 hex characters), and optionally ``model``, ``name``, ``room``,
  ``kind`` (``"air_conditioner"`` / ``"air_purifier"``) and ``mapping``
  (per-property MIoT address overrides).
* Environment: ``MIIO_DEVICES`` holds the same list as a JSON array; or a
  single device via ``MIIO_HOST`` / ``MIIO_TOKEN`` / ``MIIO_MODEL``.

The AES layer uses the ``cryptography`` package when crypto operations are
actually performed; it is an optional install (``pip install cryptography``)
and a clear error is raised if it is missing. Everything else is standard
library only.

Per-model MIoT addresses and value-lists are device facts, not protocol
constants: the tables below are the typical layouts of the supported
families and every entry can be overridden per device through ``mapping``.
Physical-device verification is still outstanding - see the provenance
record. The air purifier's filter life has no canonical Capability in core
yet, so it is reported as a raw ``filter_life`` state reading (percent).
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from omnibutler.core.errors import (
    DeviceNotFoundError,
    DriverNotConfiguredError,
    OmniButlerError,
    PropertyValidationError,
)
from omnibutler.core.models import Capability as Cap
from omnibutler.core.models import Device, Property
from omnibutler.drivers.base import Driver

MIIO_PORT = 54321
_MAGIC = 0x2131
_HEADER = struct.Struct(">HHIII")  # magic, length, reserved, device id, stamp
_HEADER_SIZE = 32
_HELLO_PACKET = struct.pack(">HH", _MAGIC, _HEADER_SIZE) + b"\xff" * 28


# ---------------------------------------------------------------------------
# Packet codec (facts: docs/specs/miio-protocol.md)
# ---------------------------------------------------------------------------

def _load_aes():
    """Import the AES primitives lazily; crypto is an optional install."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise DriverNotConfiguredError(
            "The miIO driver needs the 'cryptography' package to talk to "
            "devices (AES-128-CBC packet encryption). Install it with: "
            "pip install cryptography"
        ) from exc
    return Cipher, algorithms, modes


def _derive_key_iv(token: bytes) -> tuple[bytes, bytes]:
    key = hashlib.md5(token).digest()
    iv = hashlib.md5(key + token).digest()
    return key, iv


def _pkcs7_pad(data: bytes) -> bytes:
    pad_len = 16 - (len(data) % 16)
    return data + bytes([pad_len]) * pad_len


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data or len(data) % 16:
        raise OmniButlerError("miIO response payload is not a whole number of AES blocks")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 16 or data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise OmniButlerError("miIO response payload has invalid PKCS#7 padding")
    return data[:-pad_len]


def _aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    Cipher, algorithms, modes = _load_aes()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    Cipher, algorithms, modes = _load_aes()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def parse_hello(data: bytes) -> tuple[int, int] | None:
    """Return (device id, stamp) from a hello packet, or None if invalid."""
    if len(data) < _HEADER_SIZE:
        return None
    magic, _length, _reserved, device_id, stamp = _HEADER.unpack_from(data)
    if magic != _MAGIC:
        return None
    return device_id, stamp


def build_data_packet(
    token: bytes, device_id: int, stamp: int, payload: bytes
) -> bytes:
    """Encrypt *payload* and frame it as a miIO data packet."""
    key, iv = _derive_key_iv(token)
    ciphertext = _aes_cbc_encrypt(key, iv, _pkcs7_pad(payload))
    header = _HEADER.pack(_MAGIC, _HEADER_SIZE + len(ciphertext), 0, device_id, stamp)
    check = hashlib.md5(header + token + ciphertext).digest()
    return header + check + ciphertext


def parse_data_packet(token: bytes, data: bytes) -> bytes:
    """Verify and decrypt a miIO data packet, returning the JSON payload."""
    if len(data) <= _HEADER_SIZE:
        raise OmniButlerError("miIO response is too short to hold a payload")
    magic, length, _reserved, _device_id, _stamp = _HEADER.unpack_from(data)
    if magic != _MAGIC:
        raise OmniButlerError("miIO response does not start with the miIO magic")
    end = min(length, len(data)) if length >= _HEADER_SIZE else len(data)
    check = data[16:_HEADER_SIZE]
    ciphertext = data[_HEADER_SIZE:end]
    expected = hashlib.md5(data[:16] + token + ciphertext).digest()
    if expected != check:
        raise OmniButlerError(
            "miIO response checksum mismatch - the device token is probably "
            "wrong for this device, or the packet was corrupted"
        )
    key, iv = _derive_key_iv(token)
    return _pkcs7_unpad(_aes_cbc_decrypt(key, iv, ciphertext))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_hosts(
    timeout: float = 2.0,
    broadcast_addr: str = "255.255.255.255",
    port: int = MIIO_PORT,
) -> list[tuple[str, int, int]]:
    """Broadcast a hello probe; return (ip, device id, stamp) per answer."""
    found: dict[str, tuple[int, int]] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        sock.settimeout(0.25)
        try:
            sock.sendto(_HELLO_PACKET, (broadcast_addr, port))
        except PermissionError:
            # Some sandboxed environments forbid unconnected UDP sends;
            # a connected socket reaches the same probe target there.
            sock.connect((broadcast_addr, port))
            sock.send(_HELLO_PACKET)
        except OSError:
            return []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            parsed = parse_hello(data)
            if parsed is not None:
                found[addr[0]] = parsed
    finally:
        sock.close()
    return [(ip, dev_id, stamp) for ip, (dev_id, stamp) in sorted(found.items())]


# ---------------------------------------------------------------------------
# Per-device session
# ---------------------------------------------------------------------------

class _Session:
    """One device's handshake state and request/response channel."""

    def __init__(self, host: str, token: bytes, port: int, timeout: float) -> None:
        self.host = host
        self.token = token
        self.port = port
        self.timeout = timeout
        self.device_id: int | None = None
        self._stamp: int | None = None
        self._stamp_at: float = 0.0
        self._request_id = 0

    def _exchange(self, packet: bytes) -> bytes:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(self.timeout)
            # Connected UDP: replies are filtered to this device, and some
            # sandboxed networks only permit connected sends at all.
            sock.connect((self.host, self.port))
            sock.send(packet)
            return sock.recv(4096)
        except socket.timeout as exc:
            raise OmniButlerError(
                f"miIO device at {self.host} did not answer within "
                f"{self.timeout:.0f}s - check it is powered and on this network"
            ) from exc
        except OSError as exc:
            raise OmniButlerError(
                f"cannot reach miIO device at {self.host}: {exc.strerror or exc}"
            ) from exc
        finally:
            sock.close()

    def handshake(self) -> None:
        data = self._exchange(_HELLO_PACKET)
        parsed = parse_hello(data)
        if parsed is None:
            raise OmniButlerError(
                f"device at {self.host} answered the hello probe with an "
                "unexpected packet - is it a miIO device?"
            )
        self.device_id, self._stamp = parsed
        self._stamp_at = time.monotonic()

    def _current_stamp(self) -> int:
        if self._stamp is None:  # pragma: no cover - guarded by request()
            raise OmniButlerError("miIO session used before its handshake")
        elapsed = int(time.monotonic() - self._stamp_at)
        return (self._stamp + elapsed) & 0xFFFFFFFF

    def request(self, method: str, params: Any) -> Any:
        """Run one JSON-RPC-shaped request; return its ``result`` field."""
        last_error: Exception | None = None
        for _attempt in range(2):
            if self.device_id is None:
                self.handshake()
            assert self.device_id is not None
            self._request_id += 1
            request_id = self._request_id
            document = json.dumps(
                {"id": request_id, "method": method, "params": params}
            ).encode()
            packet = build_data_packet(
                self.token, self.device_id, self._current_stamp(), document
            )
            try:
                raw = self._exchange(packet)
                payload = parse_data_packet(self.token, raw)
            except OmniButlerError as exc:
                # A stale stamp or dropped session is the common cause:
                # forget the handshake and retry once with a fresh one.
                last_error = exc
                self.device_id = None
                self._stamp = None
                continue
            try:
                response = json.loads(payload.decode())
            except (ValueError, UnicodeDecodeError) as exc:
                raise OmniButlerError(
                    f"miIO device at {self.host} returned an undecodable payload"
                ) from exc
            if response.get("id") != request_id:
                raise OmniButlerError(
                    f"miIO device at {self.host} answered request "
                    f"{response.get('id')!r}, expected {request_id}"
                )
            if "error" in response:
                error = response["error"] or {}
                raise OmniButlerError(
                    f"miIO device at {self.host} rejected {method}: "
                    f"{error.get('message', error)}"
                )
            return response.get("result")
        raise OmniButlerError(
            f"miIO request to {self.host} failed after a re-handshake: {last_error}"
        )


# ---------------------------------------------------------------------------
# MIoT mapping tables (per-model device facts; overridable per device)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _MiotRef:
    siid: int
    piid: int
    kind: str  # "bool" | "number" | "enum" | "fan_level"
    values: tuple[str, ...] = ()  # labels indexed by wire code (enum/fan_level)
    writable: bool = True

    def with_overrides(self, override: dict[str, Any]) -> "_MiotRef":
        return _MiotRef(
            siid=int(override.get("siid", self.siid)),
            piid=int(override.get("piid", self.piid)),
            kind=str(override.get("kind", self.kind)),
            values=tuple(override.get("values", self.values)),
            writable=bool(override.get("writable", self.writable)),
        )


_AC_MODES = ("auto", "cool", "dry", "heat", "fan")
_AC_FAN_LEVELS = ("auto", "low", "medium", "high", "turbo")
_AC_FAN_PERCENT = {"auto": 0, "low": 25, "medium": 50, "high": 75, "turbo": 100}
_PURIFIER_MODES = ("auto", "silent", "turbo", "manual")

_FAMILY_MAPS: dict[str, dict[str, _MiotRef]] = {
    "air_conditioner": {
        "onoff": _MiotRef(2, 1, "bool"),
        "mode": _MiotRef(2, 2, "enum", _AC_MODES),
        "target_temperature": _MiotRef(2, 3, "number"),
        "fan_speed": _MiotRef(3, 1, "fan_level", _AC_FAN_LEVELS),
    },
    "air_purifier": {
        "onoff": _MiotRef(2, 1, "bool"),
        "mode": _MiotRef(2, 2, "enum", _PURIFIER_MODES),
        "pm25": _MiotRef(3, 1, "number", writable=False),
        # No canonical Capability for filter life yet: reported as a raw
        # state reading by get_state(), not a settable Property.
        "filter_life": _MiotRef(4, 1, "number", writable=False),
    },
}

# Canonical Property objects surfaced for each mapped family member.
_FAMILY_PROPERTIES: dict[str, dict[str, Property]] = {
    "air_conditioner": {
        "onoff": Property(Cap.ONOFF),
        "mode": Property(Cap.MODE, options=["cool", "heat", "dry", "fan", "auto"]),
        "target_temperature": Property(Cap.TARGET_TEMPERATURE),
        "fan_speed": Property(Cap.FAN_SPEED),
    },
    "air_purifier": {
        "onoff": Property(Cap.ONOFF),
        "mode": Property(Cap.MODE, options=["auto", "silent", "turbo", "manual"]),
        "pm25": Property(Cap.PM25),
    },
}


def _kind_from_model(model: str) -> str:
    text = model.lower()
    if "aircondition" in text or "air-condition" in text:
        return "air_conditioner"
    if "airpurifier" in text or "air-purifier" in text or "purifier" in text:
        return "air_purifier"
    return ""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class MiioDeviceConfig:
    """One configured device. ``token`` is a secret: it is never repr'd."""

    id: str
    host: str
    token: str = field(repr=False)
    model: str = ""
    name: str = ""
    room: str = "unknown"
    kind: str = ""
    mapping: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MiioDeviceConfig":
        return cls(
            id=str(data["id"]),
            host=str(data["host"]),
            token=str(data["token"]),
            model=str(data.get("model", "")),
            name=str(data.get("name", "")),
            room=str(data.get("room", "unknown")),
            kind=str(data.get("kind", "")),
            mapping=data.get("mapping") or None,
        )

    def token_bytes(self) -> bytes:
        text = self.token.strip()
        try:
            raw = bytes.fromhex(text)
        except ValueError:
            raw = b""
        if len(raw) != 16:
            raise DriverNotConfiguredError(
                f"miIO device {self.id!r}: the token must be 16 bytes given "
                "as 32 hex characters (obtain it from your own Xiaomi "
                "account for your own device); the configured value is not"
            )
        return raw

    @property
    def family(self) -> str:
        return self.kind or _kind_from_model(self.model)


def _configs_from_env() -> list[MiioDeviceConfig]:
    raw = os.environ.get("MIIO_DEVICES", "").strip()
    if raw:
        try:
            entries = json.loads(raw)
        except ValueError as exc:
            raise DriverNotConfiguredError(
                "MIIO_DEVICES is not valid JSON; expected an array of "
                "{id, host, token, model} objects"
            ) from exc
        return [MiioDeviceConfig.from_dict(entry) for entry in entries]
    host = os.environ.get("MIIO_HOST", "").strip()
    token = os.environ.get("MIIO_TOKEN", "").strip()
    if host and token:
        return [
            MiioDeviceConfig(
                id=os.environ.get("MIIO_DEVICE_ID", "miio_device"),
                host=host,
                token=token,
                model=os.environ.get("MIIO_MODEL", ""),
                name=os.environ.get("MIIO_NAME", ""),
                room=os.environ.get("MIIO_ROOM", "unknown"),
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

class MiioDriver(Driver):
    name = "miio"

    def __init__(
        self,
        devices: list[MiioDeviceConfig | dict[str, Any]] | None = None,
        *,
        timeout: float = 5.0,
        discover_timeout: float = 2.0,
        port: int = MIIO_PORT,
        broadcast_addr: str = "255.255.255.255",
    ) -> None:
        if devices is None:
            configs = _configs_from_env()
        else:
            configs = [
                d if isinstance(d, MiioDeviceConfig) else MiioDeviceConfig.from_dict(d)
                for d in devices
            ]
        self._configs: dict[str, MiioDeviceConfig] = {c.id: c for c in configs}
        self._timeout = timeout
        self._discover_timeout = discover_timeout
        self._port = port
        self._broadcast_addr = broadcast_addr
        self._sessions: dict[str, _Session] = {}

    # -- configuration / mapping helpers ----------------------------------
    @property
    def configured(self) -> bool:
        return bool(self._configs)

    def _config(self, device_id: str) -> MiioDeviceConfig:
        try:
            return self._configs[device_id]
        except KeyError:
            raise DeviceNotFoundError(
                f"miio driver has no configured device {device_id!r}; "
                f"configured: {sorted(self._configs)}"
            ) from None

    def _session(self, config: MiioDeviceConfig) -> _Session:
        session = self._sessions.get(config.id)
        if session is None:
            session = _Session(config.host, config.token_bytes(), self._port, self.timeout)
            self._sessions[config.id] = session
        return session

    @property
    def timeout(self) -> float:
        return self._timeout

    def _mapping(self, config: MiioDeviceConfig) -> dict[str, _MiotRef]:
        family = config.family
        if family not in _FAMILY_MAPS:
            raise DriverNotConfiguredError(
                f"miIO device {config.id!r}: model {config.model!r} is not a "
                "family this driver has a mapping table for (supported: "
                "air_conditioner, air_purifier). Set 'kind' explicitly or "
                "provide a full 'mapping' in the device configuration."
            )
        mapping = dict(_FAMILY_MAPS[family])
        for name, override in (config.mapping or {}).items():
            if name in mapping:
                mapping[name] = mapping[name].with_overrides(override)
            else:
                mapping[name] = _MiotRef(
                    siid=int(override["siid"]), piid=int(override["piid"]),
                    kind=str(override.get("kind", "number")),
                    values=tuple(override.get("values", ())),
                    writable=bool(override.get("writable", True)),
                )
        return mapping

    def _device_object(self, config: MiioDeviceConfig) -> Device:
        family = config.family
        properties = dict(_FAMILY_PROPERTIES.get(family, {}))
        return Device(
            id=config.id,
            name=config.name or config.id,
            driver=self.name,
            room=config.room,
            brand="Xiaomi",
            model=config.model,
            properties=properties,
            actions=["turn_on", "turn_off"],
        )

    # -- value translation ---------------------------------------------------
    @staticmethod
    def _to_canonical(ref: _MiotRef, wire: Any) -> Any:
        if ref.kind == "bool":
            return bool(wire)
        if ref.kind == "enum":
            try:
                return ref.values[int(wire)]
            except (ValueError, TypeError, IndexError):
                return wire  # unknown code: surface it raw rather than guess
        if ref.kind == "fan_level":
            try:
                label = ref.values[int(wire)]
            except (ValueError, TypeError, IndexError):
                return wire
            return _AC_FAN_PERCENT.get(label, wire)
        return wire

    @staticmethod
    def _to_wire(ref: _MiotRef, name: str, value: Any) -> Any:
        if ref.kind == "bool":
            return bool(value)
        if ref.kind == "enum":
            label = str(value)
            if label not in ref.values:
                raise PropertyValidationError(
                    f"{name}: {label!r} is not one of {list(ref.values)}"
                )
            return ref.values.index(label)
        if ref.kind == "fan_level":
            try:
                percent = float(value)
            except (TypeError, ValueError):
                raise PropertyValidationError(
                    f"{name}: expected a percentage, got {value!r}"
                ) from None
            # Nearest labelled level; 0 selects the automatic level.
            best = min(
                ref.values,
                key=lambda label: abs(_AC_FAN_PERCENT.get(label, 0) - percent),
            )
            return ref.values.index(best)
        return value

    # -- Driver API ---------------------------------------------------------
    def discover(self) -> list[Device]:
        answers = discover_hosts(
            timeout=self._discover_timeout,
            broadcast_addr=self._broadcast_addr,
            port=self._port,
        )
        by_host = {c.host: c for c in self._configs.values()}
        devices = []
        for ip, device_id, _stamp in answers:
            config = by_host.get(ip)
            if config is not None:
                devices.append(self._device_object(config))
            else:
                devices.append(
                    Device(
                        id=f"miio-{device_id}",
                        name=f"Xiaomi device {device_id}",
                        driver=self.name,
                        brand="Xiaomi",
                        model="",
                        properties={},
                        actions=[],
                    )
                )
        return devices

    def list_devices(self) -> list[Device]:
        return [self._device_object(config) for config in self._configs.values()]

    def _miot_call(self, config: MiioDeviceConfig, method: str, items: list[dict]) -> list:
        session = self._session(config)
        if session.device_id is None:
            session.handshake()
        did = str(session.device_id)
        params = [dict(item, did=did) for item in items]
        result = session.request(method, params)
        if not isinstance(result, list):
            raise OmniButlerError(
                f"miIO device at {config.host} returned an unexpected "
                f"{method} result: {result!r}"
            )
        return result

    def get_state(self, device_id: str) -> dict[str, Any]:
        config = self._config(device_id)
        mapping = self._mapping(config)
        addresses = [
            {"siid": ref.siid, "piid": ref.piid} for ref in mapping.values()
        ]
        results = self._miot_call(config, "get_properties", addresses)
        state: dict[str, Any] = {}
        failures = 0
        for (name, ref), item in zip(mapping.items(), results):
            if not isinstance(item, dict) or item.get("code") != 0:
                failures += 1
                continue
            state[name] = self._to_canonical(ref, item.get("value"))
        if failures and not state:
            raise OmniButlerError(
                f"miIO device {device_id!r} could not report any of its "
                "properties (all reads returned a non-zero code)"
            )
        return state

    def set_property(self, device_id: str, property_name: str, value: Any) -> dict[str, Any]:
        config = self._config(device_id)
        mapping = self._mapping(config)
        ref = mapping.get(property_name)
        if ref is None:
            raise PropertyValidationError(
                f"device {device_id!r} has no miIO-mapped property "
                f"{property_name!r}; available: {sorted(mapping)}"
            )
        if not ref.writable:
            raise PropertyValidationError(
                f"property {property_name!r} of device {device_id!r} is read-only"
            )
        wire = self._to_wire(ref, property_name, value)
        results = self._miot_call(
            config,
            "set_properties",
            [{"siid": ref.siid, "piid": ref.piid, "value": wire}],
        )
        item = results[0] if results else {}
        if not isinstance(item, dict) or item.get("code") != 0:
            raise OmniButlerError(
                f"miIO device {device_id!r} refused to set "
                f"{property_name!r} (result code {item.get('code') if isinstance(item, dict) else 'missing'})"
            )
        return {property_name: value}

    def call_action(
        self, device_id: str, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if action in {"turn_on", "turn_off"}:
            return self.set_property(device_id, "onoff", action == "turn_on")
        config = self._config(device_id)
        raise PropertyValidationError(
            f"device {device_id!r} (model {config.model!r}) does not support "
            f"action {action!r} through this driver; supported: turn_on, turn_off"
        )

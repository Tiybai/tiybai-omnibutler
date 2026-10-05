"""Broadlink RM-family driver (IR / RF learning hubs), built on
python-broadlink (MIT) as an optional extra.

Install with::

    pip install "tiybai-omnibutler[broadlink]"

python-broadlink is imported lazily, only when an operation actually needs
the transport. Without it the driver still constructs and lists configured
devices, but any device operation raises a clear error - nothing silently
pretends to work.

What this hub honestly is: a one-way blaster. It can *learn* an IR or RF
code from a physical remote and *replay* that code later. It cannot read
back the state of the appliance the code controls, and a learned code only
does whatever the original remote button did. There are therefore no fake
switch/temperature properties for target appliances. The single exception
is sensing: RM4 models fitted with the hts2 sensor report room temperature
and humidity, exposed here as read-only properties when a device is
configured with ``"sensors": true``.

Configuration (constructor argument or environment):

    BroadlinkDriver(devices=[{
        "host": "192.168.1.60",        # LAN address (required)
        "mac": "aa:bb:cc:dd:ee:ff",    # device MAC (required)
        "type": 21007,                 # Broadlink device type (optional;
                                       # int or "0x520b"; speeds up connect)
        "port": 80,                    # UDP port (default 80)
        "sensors": true,               # RM4 with hts2 sensor (optional)
        "id": "living_blaster",        # OmniButler id (optional)
        "name": "Living room RM4",     # display name (optional)
        "room": "living",              # room (optional)
    }])

or the same list as JSON in the ``BROADLINK_DEVICES_JSON`` environment
variable. Devices can also be found on the LAN via :meth:`discover`, which
uses python-broadlink's broadcast discovery.

Learned codes are kept in a local JSON file (driver argument
``codes_file``, env ``BROADLINK_CODES_FILE``, or
``~/.omnibutler/broadlink-codes.json``), keyed by device id and code name.
They are the user's own remote codes - replaying them is the entire point -
so, unlike credentials, they are data, not secrets.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

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
    "python-broadlink is not installed, so the Broadlink driver cannot "
    "talk to devices in this environment. Install the optional extra "
    'with: pip install "tiybai-omnibutler[broadlink]" '
    "(python-broadlink is MIT-licensed; see docs/license-audit.md), "
    "then retry."
)

_ENV_VAR = "BROADLINK_DEVICES_JSON"
_CODES_ENV_VAR = "BROADLINK_CODES_FILE"

# Read-only room sensing, present only on RM4 units with the hts2 sensor.
_SENSOR_PROPERTIES: dict[str, Property] = {
    "current_temperature": Property(Cap.CURRENT_TEMPERATURE),
    "humidity": Property(Cap.HUMIDITY, writable=False),
}

_ACTIONS = ["learn_code", "send_code"]
_DEFAULT_LEARN_TIMEOUT = 30.0
_POLL_INTERVAL = 0.2


def _load_broadlink() -> Any:
    try:
        import broadlink
    except ImportError:
        raise PlannedDriverError(_NOT_INSTALLED) from None
    return broadlink


def _parse_mac(value: Any) -> tuple[str, bytes]:
    """Normalise a MAC given as 'aa:bb:..', 'aa-bb-..' or plain hex."""
    text = str(value).strip()
    try:
        raw = bytes.fromhex(text.replace(":", "").replace("-", ""))
    except ValueError:
        raise DriverNotConfiguredError(
            "Broadlink device config has an unparsable 'mac' value; "
            "expected hex like 'aa:bb:cc:dd:ee:ff'."
        ) from None
    if len(raw) != 6:
        raise DriverNotConfiguredError(
            "Broadlink device config 'mac' must be 6 bytes of hex."
        )
    return ":".join(f"{byte:02x}" for byte in raw), raw


def _parse_type(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 0)  # accepts "21007" and "0x520b"
    except ValueError:
        raise DriverNotConfiguredError(
            "Broadlink device config 'type' must be an int or hex string."
        ) from None


class BroadlinkDriver(Driver):
    name = "broadlink"

    def __init__(
        self,
        devices: Iterable[dict[str, Any]] | None = None,
        *,
        codes_file: str | os.PathLike[str] | None = None,
        discover_timeout: float = 5.0,
    ) -> None:
        if devices is None:
            devices = self._devices_from_env()
        self._devices: dict[str, Device] = {}
        self._configs: dict[str, dict[str, Any]] = {}
        self._connections: dict[str, Any] = {}
        self._raw_devices: dict[str, Any] = {}  # objects found by discover()
        self._discover_timeout = discover_timeout
        for entry in devices:
            device, config = self._build(entry)
            self._devices[device.id] = device
            self._configs[device.id] = config
        if codes_file is None:
            codes_file = os.environ.get(_CODES_ENV_VAR, "").strip() or None
        self._codes_file = (
            Path(codes_file).expanduser()
            if codes_file is not None
            else Path.home() / ".omnibutler" / "broadlink-codes.json"
        )
        self._codes: dict[str, dict[str, dict[str, str]]] | None = None

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
                "It must be a JSON list of Broadlink device objects."
            ) from exc
        if not isinstance(parsed, list):
            raise DriverNotConfiguredError(
                f"{_ENV_VAR} must be a JSON list of Broadlink device objects."
            )
        return parsed

    @staticmethod
    def _build(entry: dict[str, Any]) -> tuple[Device, dict[str, Any]]:
        if not isinstance(entry, dict):
            raise DriverNotConfiguredError(
                "Each Broadlink device config must be an object with "
                "host and mac."
            )
        missing = [key for key in ("host", "mac") if not entry.get(key)]
        if missing:
            raise DriverNotConfiguredError(
                f"Broadlink device config is missing required field(s): "
                f"{', '.join(missing)}. Each device needs host and mac."
            )
        mac_text, mac_bytes = _parse_mac(entry["mac"])
        sensors = bool(entry.get("sensors", False))
        device = Device(
            id=str(entry.get("id") or f"broadlink-{mac_bytes.hex()}"),
            name=str(entry.get("name") or f"Broadlink {mac_text[-8:]}"),
            driver="broadlink",
            room=str(entry.get("room", "unknown")),
            brand=str(entry.get("brand", "Broadlink")),
            model=str(entry.get("model", "RM")),
            properties=dict(_SENSOR_PROPERTIES) if sensors else {},
            actions=list(_ACTIONS),
        )
        config = {
            "host": str(entry["host"]),
            "port": int(entry.get("port", 80)),
            "mac": mac_text,
            "mac_bytes": mac_bytes,
            "type": _parse_type(entry.get("type")),
            "sensors": sensors,
        }
        return device, config

    def __repr__(self) -> str:
        return f"BroadlinkDriver(devices={sorted(self._devices)})"

    # -- learned-code store ------------------------------------------------
    def _load_codes(self) -> dict[str, dict[str, dict[str, str]]]:
        if self._codes is None:
            self._codes = {}
            if self._codes_file.exists():
                try:
                    parsed = json.loads(self._codes_file.read_text(encoding="utf-8"))
                except OSError as exc:
                    raise DriverNotConfiguredError(
                        f"Cannot read Broadlink codes file "
                        f"{self._codes_file}: {exc}."
                    ) from exc
                except json.JSONDecodeError:
                    parsed = None  # handled below, as corruption
                if isinstance(parsed, dict):
                    self._codes = parsed
                else:
                    # Corrupt (or wrong-shaped) file: quarantine it
                    # and start empty rather than wedging the whole
                    # driver on data a user can simply relearn.
                    self._quarantine_corrupt_codes()
        return self._codes

    def _quarantine_corrupt_codes(self) -> None:
        """Move a corrupt codes file aside; start from an empty table.

        The learned codes are the user's own data, so the bad file is
        kept for inspection - renamed to ``<name>.corrupt`` (numeric
        suffix when that name is already taken) - instead of being
        deleted here or silently overwritten by the next save. The
        loss is announced loudly on stderr with the path and the
        disposition; the driver itself stays usable.
        """
        target = self._codes_file.with_name(self._codes_file.name + ".corrupt")
        n = 1
        while target.exists():
            target = self._codes_file.with_name(
                f"{self._codes_file.name}.corrupt.{n}")
            n += 1
        try:
            self._codes_file.replace(target)
            disposition = f"it was moved to {target} for inspection"
        except OSError as exc:
            disposition = (f"moving it aside failed ({exc}); it will be "
                           "overwritten by the next save")
        print(
            f"omnibutler: Broadlink codes file {self._codes_file} is not "
            f"valid JSON; starting with an empty code table - {disposition}. "
            "Codes can be relearned with the learn_code action.",
            file=sys.stderr,
        )

    def _save_codes(self) -> None:
        codes = self._load_codes()
        self._codes_file.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(codes, ensure_ascii=False, indent=1, sort_keys=True)
        # Write-then-replace (the pattern the confirmation queue uses):
        # a crash mid-write leaves the previous codes file intact
        # instead of a truncated one the loader would then quarantine.
        # The tmp name carries the pid so concurrent processes cannot
        # interleave writes to one shared tmp file.
        tmp = self._codes_file.with_name(
            f"{self._codes_file.name}.tmp-{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self._codes_file)

    # -- transport ---------------------------------------------------------
    def _connect(self, device_id: str) -> Any:
        if device_id not in self._connections:
            broadlink = _load_broadlink()
            config = self._configs[device_id]
            raw = self._raw_devices.get(device_id)
            try:
                if raw is not None:
                    connection = raw
                elif config["type"] is not None:
                    connection = broadlink.gendevice(
                        config["type"],
                        (config["host"], config["port"]),
                        config["mac_bytes"],
                    )
                else:
                    connection = broadlink.hello(
                        config["host"], port=config["port"]
                    )
                if connection.auth() is False:
                    raise OmniButlerError(
                        f"Broadlink device at {config['host']} refused "
                        "authentication."
                    )
            except OmniButlerError:
                raise
            except Exception as exc:
                raise OmniButlerError(
                    f"Cannot reach Broadlink device at {config['host']} "
                    f"({type(exc).__name__}). Check the host is on and on "
                    "the same LAN."
                ) from exc
            self._connections[device_id] = connection
        return self._connections[device_id]

    def _lookup(self, device_id: str) -> Device:
        try:
            return self._devices[device_id]
        except KeyError:
            raise DeviceNotFoundError(
                f"Broadlink driver has no device {device_id!r}; known: "
                f"{sorted(self._devices)}"
            ) from None

    # -- Driver API --------------------------------------------------------
    def discover(self) -> list[Device]:
        broadlink = _load_broadlink()
        try:
            found = broadlink.discover(timeout=self._discover_timeout) or []
        except Exception as exc:
            raise OmniButlerError(
                f"Broadlink discovery failed ({type(exc).__name__})."
            ) from exc
        for raw in found:
            try:
                mac_text, mac_bytes = _parse_mac(bytes(raw.mac).hex())
            except Exception:
                continue  # unidentifiable responder - not ours to manage
            device_id = next(
                (
                    known_id
                    for known_id, cfg in self._configs.items()
                    if cfg["mac"] == mac_text
                ),
                f"broadlink-{mac_bytes.hex()}",
            )
            if device_id not in self._devices:
                get_type = getattr(raw, "get_type", None)
                model = str(get_type()) if callable(get_type) else "RM"
                host = getattr(raw, "host", ("", 80))
                self._devices[device_id] = Device(
                    id=device_id,
                    name=str(getattr(raw, "name", "") or f"Broadlink {model}"),
                    driver="broadlink",
                    room="unknown",
                    brand="Broadlink",
                    model=model,
                    properties={},
                    actions=list(_ACTIONS),
                )
                self._configs[device_id] = {
                    "host": str(host[0]),
                    "port": int(host[1]),
                    "mac": mac_text,
                    "mac_bytes": mac_bytes,
                    "type": _parse_type(getattr(raw, "devtype", None)),
                    "sensors": False,
                }
            self._raw_devices[device_id] = raw
        return self.list_devices()

    def list_devices(self) -> list[Device]:
        return list(self._devices.values())

    def get_state(self, device_id: str) -> dict[str, Any]:
        _load_broadlink()  # the transport dependency gates every operation
        device = self._lookup(device_id)
        state: dict[str, Any] = {}
        if device.properties:
            # Sensor-equipped unit: the only state a blaster can report.
            connection = self._connect(device_id)
            check_sensors = getattr(connection, "check_sensors", None)
            readings = None
            if callable(check_sensors):
                try:
                    readings = check_sensors()
                except Exception as exc:
                    raise OmniButlerError(
                        f"Broadlink sensor read failed ({type(exc).__name__})."
                    ) from exc
            if isinstance(readings, dict):
                temperature = readings.get("temperature")
                if temperature is not None:
                    state["current_temperature"] = float(temperature)
                humidity = readings.get("humidity")
                if humidity is not None:
                    state["humidity"] = float(humidity)
        device.state = state
        return dict(state)

    def set_property(
        self, device_id: str, property_name: str, value: Any
    ) -> dict[str, Any]:
        _load_broadlink()  # the transport dependency gates every operation
        device = self._lookup(device_id)
        device.property(property_name)  # raises if the hub has no such property
        # Every property this hub exposes is a sensor reading.
        raise PropertyValidationError(
            f"{property_name}: property is read-only - a Broadlink hub "
            "only emits codes; use learn_code / send_code actions instead."
        )

    def call_action(
        self, device_id: str, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        _load_broadlink()  # the transport dependency gates every operation
        self._lookup(device_id)
        params = params or {}
        if action == "learn_code":
            return self._learn_code(device_id, params)
        if action == "send_code":
            return self._send_code(device_id, params)
        raise OmniButlerError(
            f"Broadlink driver does not support action {action!r}; "
            f"supported: {', '.join(_ACTIONS)}."
        )

    # -- actions -----------------------------------------------------------
    def _learn_code(self, device_id: str, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "").strip()
        if not name:
            raise OmniButlerError(
                "learn_code needs a 'name' param so the code can be "
                "replayed later with send_code."
            )
        kind = str(params.get("kind", "ir")).lower()
        if kind not in {"ir", "rf"}:
            raise OmniButlerError(
                f"learn_code kind must be 'ir' or 'rf', got {kind!r}."
            )
        timeout = float(params.get("timeout", _DEFAULT_LEARN_TIMEOUT))
        connection = self._connect(device_id)
        if kind == "rf":
            self._prepare_rf_learning(connection, timeout)
        else:
            try:
                connection.enter_learning()
            except Exception as exc:
                raise OmniButlerError(
                    f"Could not enter IR learning mode ({type(exc).__name__})."
                ) from exc
        packet = self._poll_for_code(connection, timeout)
        if packet is None:
            raise OmniButlerError(
                f"No {kind} code received within {timeout:g} seconds; "
                "point the remote at the hub and try again."
            )
        codes = self._load_codes()
        codes.setdefault(device_id, {})[name] = {
            "kind": kind,
            "code": base64.b64encode(bytes(packet)).decode("ascii"),
        }
        self._save_codes()
        return {"learn_code": name, "kind": kind, "bytes": len(packet)}

    def _prepare_rf_learning(self, connection: Any, timeout: float) -> None:
        sweep = getattr(connection, "sweep_frequency", None)
        check = getattr(connection, "check_frequency", None)
        if not callable(sweep) or not callable(check):
            raise OmniButlerError(
                "This Broadlink device/library cannot learn RF codes "
                "(no frequency sweep support; RM Pro models can)."
            )
        try:
            sweep()
        except Exception as exc:
            raise OmniButlerError(
                f"Could not start RF frequency sweep ({type(exc).__name__})."
            ) from exc
        deadline = time.monotonic() + timeout
        locked = False
        while time.monotonic() < deadline:
            try:
                if check():
                    locked = True
                    break
            except Exception:
                pass  # library signals "not locked yet" by raising
            time.sleep(_POLL_INTERVAL)
        if not locked:
            raise OmniButlerError(
                f"No RF signal locked within {timeout:g} seconds; hold the "
                "remote button down near the hub and try again."
            )
        find = getattr(connection, "find_rf_packet", None)
        if callable(find):
            find()

    @staticmethod
    def _poll_for_code(connection: Any, timeout: float) -> bytes | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = connection.check_data()
            except Exception:
                data = None  # library signals "nothing learned yet" by raising
            if data:
                return bytes(data)
            time.sleep(_POLL_INTERVAL)
        return None

    def _send_code(self, device_id: str, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "").strip()
        inline = params.get("code")
        if inline:
            try:
                packet = base64.b64decode(str(inline), validate=True)
            except (binascii.Error, ValueError):
                raise OmniButlerError(
                    "send_code 'code' param must be base64 (as stored by "
                    "learn_code)."
                ) from None
            label = name or "inline"
        elif name:
            stored = self._load_codes().get(device_id, {})
            if name not in stored:
                raise OmniButlerError(
                    f"No learned code named {name!r} for this device; "
                    f"known: {sorted(stored)}. Learn it first with "
                    "learn_code."
                )
            packet = base64.b64decode(stored[name]["code"])
            label = name
        else:
            raise OmniButlerError(
                "send_code needs a 'name' (learned code) or a base64 "
                "'code' param."
            )
        repeat = max(1, int(params.get("repeat", 1)))
        connection = self._connect(device_id)
        try:
            for _ in range(repeat):
                connection.send_data(packet)
        except Exception as exc:
            raise OmniButlerError(
                f"Sending code {label!r} failed ({type(exc).__name__})."
            ) from exc
        return {"send_code": label, "bytes": len(packet), "repeat": repeat}

"""Runtime assembly: build a manager + scene engine from configuration.

Shared by the CLI and the MCP server so both entry points behave identically.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from omnibutler.config import device_entries, ha_settings, load_config
from omnibutler.core.audit import AuditLog
from omnibutler.core.confirmations import ConfirmationQueue
from omnibutler.core.events import EventBus
from omnibutler.core.manager import DeviceManager
from omnibutler.core.sessions import SessionManager
from omnibutler.core.streams import StreamStore
from omnibutler.drivers.base import Driver
from omnibutler.drivers.broadlink import BroadlinkDriver
from omnibutler.drivers.homeassistant import HomeAssistantDriver
from omnibutler.drivers.matter import MatterDriver
from omnibutler.drivers.midea import MideaDriver
from omnibutler.drivers.miio import MiioDriver
from omnibutler.drivers.mock import MockDriver
from omnibutler.drivers.terminal_mock import TerminalMockDriver
from omnibutler.drivers.tuya import TuyaDriver
from omnibutler.drivers.tuya_cloud import TuyaCloudDriver
from omnibutler.drivers.xiaomi_cloud import XiaomiCloudDriver
from omnibutler.drivers.zigbee2mqtt import Zigbee2MqttDriver
from omnibutler.scenes.engine import SceneEngine
from omnibutler.scenes.loader import load_scenes_dir

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_BUNDLED_SCENES = Path(__file__).resolve().parent / "_scenes"
DEFAULT_SCENES_DIR = (_BUNDLED_SCENES if _BUNDLED_SCENES.is_dir()
                      else PACKAGE_ROOT / "examples" / "scenes")


@dataclass
class Runtime:
    manager: DeviceManager
    engine: SceneEngine
    confirmations: ConfirmationQueue
    bus: EventBus
    driver_name: str
    sessions: SessionManager | None = None
    streams: StreamStore | None = None


DRIVER_NAMES = ("mock", "homeassistant", "miio", "tuya", "broadlink", "midea",
                "matter", "zigbee2mqtt", "terminal_mock", "tuya_cloud",
                "xiaomi_cloud")

#: Entry-point group a third-party package uses to register a driver:
#: ``[project.entry-points."omnibutler.drivers"] mybrand = "pkg.mod:Cls"``.
#: See docs/drivers-third-party.md.
ENTRY_POINT_GROUP = "omnibutler.drivers"

#: The methods a driver must provide (see drivers/base.py). Third-party
#: targets are checked against this list, subclassing Driver or not.
_DRIVER_METHODS = ("discover", "list_devices", "get_state", "set_property",
                   "call_action")


@dataclass
class ThirdPartyDriver:
    """The outcome of loading one entry-point driver, good or bad."""

    name: str
    target: str  # "pkg.module:Class", as declared by the package
    factory: Callable[[], Driver] | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.factory is not None


def _missing_methods(obj: Any) -> list[str]:
    return [m for m in _DRIVER_METHODS if not callable(getattr(obj, m, None))]


def _as_factory(obj: Any) -> tuple[Callable[[], Driver] | None, str | None]:
    """Turn a loaded entry-point target into a driver factory.

    Accepts a Driver subclass (or any class with the driver methods), a
    ready Driver instance, or a plain factory callable. Anything else is
    reported, not raised - a broken package must not break the bridge.
    """
    if isinstance(obj, Driver):
        return (lambda: obj), None
    if isinstance(obj, type):
        missing = _missing_methods(obj)
        if missing:
            return None, ("target class does not provide driver method(s): "
                          + ", ".join(missing))
        return obj, None
    if callable(obj):
        return obj, None
    return None, f"target is a {type(obj).__name__}, not a driver class or factory"


def third_party_drivers() -> list[ThirdPartyDriver]:
    """Discover and load every driver registered via entry points.

    Never raises. A package that fails to import, or whose target does
    not look like a driver, comes back with ``error`` set and is skipped
    by every caller. A name that collides with a built-in driver loses:
    the built-in wins and a warning goes to stderr.
    """
    found: list[ThirdPartyDriver] = []
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # metadata itself unreadable: still start up
        print(f"omnibutler: could not scan driver entry points: {exc}",
              file=sys.stderr)
        return found
    seen: set[str] = set()
    for ep in eps:
        name = (ep.name or "").strip()
        target = getattr(ep, "value", "") or str(ep)
        if not name:
            continue
        if name in DRIVER_NAMES:
            print(f"omnibutler: third-party driver {name!r} ({target}) "
                  "ignored - the name belongs to a built-in driver",
                  file=sys.stderr)
            found.append(ThirdPartyDriver(
                name, target,
                error="name conflicts with a built-in driver; built-in wins"))
            continue
        if name in seen:
            found.append(ThirdPartyDriver(
                name, target, error="duplicate entry-point name; first one wins"))
            continue
        seen.add(name)
        try:
            obj = ep.load()
        except Exception as exc:
            found.append(ThirdPartyDriver(name, target,
                                          error=f"failed to load: {exc}"))
            continue
        factory, error = _as_factory(obj)
        found.append(ThirdPartyDriver(name, target, factory=factory, error=error))
    return found


def third_party_factories() -> dict[str, Callable[[], Driver]]:
    """Factories of the third-party drivers that loaded cleanly."""
    return {d.name: d.factory for d in third_party_drivers() if d.factory is not None}


def driver_names() -> tuple[str, ...]:
    """Every selectable driver name: built-ins first, then entry points.

    Names only - nothing is imported here, so listing stays cheap and a
    broken package still shows up (selecting it reports why it failed).
    """
    names = list(DRIVER_NAMES)
    try:
        eps: Iterable[Any] = entry_points(group=ENTRY_POINT_GROUP)
    except Exception:
        eps = []
    for ep in eps:
        if ep.name and ep.name not in names:
            names.append(ep.name)
    return tuple(names)


def _start_third_party(name: str, factory: Callable[[], Driver]) -> Driver:
    """Build one third-party driver, checking the result's shape."""
    try:
        instance = factory()
    except Exception as exc:
        raise ValueError(
            f"third-party driver {name!r} failed to start: {exc}") from exc
    missing = _missing_methods(instance)
    if missing:
        raise ValueError(
            f"third-party driver {name!r} built an object without driver "
            f"method(s): {', '.join(missing)}")
    return instance


def _build_drivers(driver: str) -> dict:
    """Construct the requested driver set.

    Real drivers take their device lists from the local config file
    (~/.omnibutler/config.json, secrets as env: references) when present,
    and fall back to their own environment variables otherwise.
    """
    config = load_config()
    if driver == "mock":
        return {"mock": MockDriver()}

    def ha() -> HomeAssistantDriver:
        settings = ha_settings(config)
        if settings.get("url") or settings.get("token"):
            return HomeAssistantDriver(
                base_url=settings.get("url"),
                token=settings.get("token"),
                subscribe_events=settings.get("subscribe_events"))
        return HomeAssistantDriver(
            subscribe_events=settings.get("subscribe_events"))

    def entries(section: str, secret: str):
        found = device_entries(config, section, secret)
        return found or None

    builders = {
        "homeassistant": ha,
        "miio": lambda: MiioDriver(devices=entries("miio", "token")),
        "tuya": lambda: TuyaDriver(devices=entries("tuya", "local_key")),
        "broadlink": lambda: BroadlinkDriver(
            devices=entries("broadlink", "mac")),
        "midea": lambda: MideaDriver(devices=entries("midea", "key")),
        "matter": lambda: MatterDriver(),
        "zigbee2mqtt": lambda: Zigbee2MqttDriver(
            devices=entries("zigbee2mqtt", "friendly_name")),
        "terminal_mock": lambda: TerminalMockDriver(),
        # Cloud fallbacks: credentials come from their own env/config
        # sections; constructed without arguments on purpose.
        "tuya_cloud": lambda: TuyaCloudDriver(),
        "xiaomi_cloud": lambda: XiaomiCloudDriver(),
    }
    if driver == "all":
        # tuya_cloud and xiaomi_cloud are deliberately NOT part of
        # "all": they route control through a vendor's cloud, so each
        # must be selected explicitly - nobody should end up on a
        # cloud channel without having chosen it.
        built = {name: build() for name, build in builders.items()
                 if name not in ("tuya_cloud", "xiaomi_cloud")}
        # Third-party drivers DO join "all": installing the package was
        # the explicit choice. One that fails to start is skipped with
        # a warning, never allowed to take the whole set down.
        for name, factory in third_party_factories().items():
            try:
                built[name] = _start_third_party(name, factory)
            except ValueError as exc:
                print(f"omnibutler: {exc}; skipped in 'all'", file=sys.stderr)
        return built
    if driver in builders:
        return {driver: builders[driver]()}
    status = {d.name: d for d in third_party_drivers()}.get(driver)
    if status is not None:
        if status.factory is None:
            raise ValueError(
                f"third-party driver {driver!r} ({status.target}) is "
                f"unavailable: {status.error}")
        return {driver: _start_third_party(driver, status.factory)}
    raise ValueError(
        f"unknown driver {driver!r}; expected one of "
        f"{', '.join(driver_names())} or 'all'")


def build_runtime(
    driver: str | None = None,
    audit_path: str | None = None,
    scenes_dir: str | Path | None = None,
    load_default_scenes: bool = True,
) -> Runtime:
    driver = driver or os.environ.get("TOB_DRIVER", "mock")
    bus = EventBus()
    audit = AuditLog(path=audit_path) if audit_path else AuditLog()
    confirmations = ConfirmationQueue()

    drivers = _build_drivers(driver)

    manager = DeviceManager(drivers=drivers, audit=audit, bus=bus)
    engine = SceneEngine(manager, confirmations)
    if load_default_scenes:
        scenes_path = Path(scenes_dir) if scenes_dir else DEFAULT_SCENES_DIR
        if scenes_path.is_dir():
            engine.add_scenes(load_scenes_dir(scenes_path))
    engine.attach(bus)
    sessions = SessionManager(bus=bus, audit=audit)
    streams = StreamStore()
    return Runtime(manager=manager, engine=engine, confirmations=confirmations,
                   bus=bus, driver_name=driver, sessions=sessions,
                   streams=streams)

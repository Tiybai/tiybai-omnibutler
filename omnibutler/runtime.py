"""Runtime assembly: build a manager + scene engine from configuration.

Shared by the CLI and the MCP server so both entry points behave identically.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from omnibutler.config import device_entries, ha_settings, load_config
from omnibutler.core.audit import AuditLog
from omnibutler.core.confirmations import ConfirmationQueue
from omnibutler.core.events import EventBus
from omnibutler.core.manager import DeviceManager
from omnibutler.drivers.broadlink import BroadlinkDriver
from omnibutler.drivers.homeassistant import HomeAssistantDriver
from omnibutler.drivers.midea import MideaDriver
from omnibutler.drivers.miio import MiioDriver
from omnibutler.drivers.mock import MockDriver
from omnibutler.drivers.tuya import TuyaDriver
from omnibutler.scenes.engine import SceneEngine
from omnibutler.scenes.loader import load_scenes_dir

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCENES_DIR = PACKAGE_ROOT / "examples" / "scenes"


@dataclass
class Runtime:
    manager: DeviceManager
    engine: SceneEngine
    confirmations: ConfirmationQueue
    bus: EventBus
    driver_name: str


DRIVER_NAMES = ("mock", "homeassistant", "miio", "tuya", "broadlink", "midea")


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
            return HomeAssistantDriver(base_url=settings.get("url"),
                                       token=settings.get("token"))
        return HomeAssistantDriver()

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
    }
    if driver == "all":
        return {name: build() for name, build in builders.items()}
    if driver in builders:
        return {driver: builders[driver]()}
    raise ValueError(
        f"unknown driver {driver!r}; expected one of "
        f"{', '.join(DRIVER_NAMES)} or 'all'")


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
    return Runtime(manager=manager, engine=engine, confirmations=confirmations,
                   bus=bus, driver_name=driver)

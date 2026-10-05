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
from omnibutler.core.sessions import SessionManager
from omnibutler.core.streams import StreamStore
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
        return {name: build() for name, build in builders.items()
                if name not in ("tuya_cloud", "xiaomi_cloud")}
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
    sessions = SessionManager(bus=bus, audit=audit)
    streams = StreamStore()
    return Runtime(manager=manager, engine=engine, confirmations=confirmations,
                   bus=bus, driver_name=driver, sessions=sessions,
                   streams=streams)

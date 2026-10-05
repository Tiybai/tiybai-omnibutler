"""Runtime assembly: build a manager + scene engine from configuration.

Shared by the CLI and the MCP server so both entry points behave identically.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from omnibutler.core.audit import AuditLog
from omnibutler.core.confirmations import ConfirmationQueue
from omnibutler.core.events import EventBus
from omnibutler.core.manager import DeviceManager
from omnibutler.drivers.homeassistant import HomeAssistantDriver
from omnibutler.drivers.mock import MockDriver
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

    drivers = {}
    if driver == "mock":
        drivers["mock"] = MockDriver()
    elif driver == "homeassistant":
        drivers["homeassistant"] = HomeAssistantDriver()
    else:
        raise ValueError(f"unknown driver {driver!r}; expected 'mock' or 'homeassistant'")

    manager = DeviceManager(drivers=drivers, audit=audit, bus=bus)
    engine = SceneEngine(manager, confirmations)
    if load_default_scenes:
        scenes_path = Path(scenes_dir) if scenes_dir else DEFAULT_SCENES_DIR
        if scenes_path.is_dir():
            engine.add_scenes(load_scenes_dir(scenes_path))
    engine.attach(bus)
    return Runtime(manager=manager, engine=engine, confirmations=confirmations,
                   bus=bus, driver_name=driver)

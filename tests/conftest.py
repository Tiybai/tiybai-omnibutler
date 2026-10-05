from pathlib import Path

import pytest

from omnibutler.core.audit import AuditLog
from omnibutler.core.manager import DeviceManager
from omnibutler.drivers.mock import MockDriver
from omnibutler.scenes.engine import SceneEngine
from omnibutler.scenes.loader import load_scenes_dir

SCENES_DIR = Path(__file__).resolve().parent.parent / "examples" / "scenes"


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Keep the persisted confirmation queue out of the real ~/.omnibutler
    and give every test a fresh queue file."""
    monkeypatch.setenv("OMNIBUTLER_STATE_DIR", str(tmp_path / "omnibutler-state"))


@pytest.fixture()
def manager(tmp_path):
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    return DeviceManager(drivers={"mock": MockDriver()}, audit=audit)


@pytest.fixture()
def engine(manager):
    eng = SceneEngine(manager)
    eng.add_scenes(load_scenes_dir(SCENES_DIR))
    return eng


@pytest.fixture()
def scenes_dir():
    return SCENES_DIR

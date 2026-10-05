"""Validate every device-data profile: parses as JSON, carries the required
fields, and only uses capabilities from the canonical Capability model with
types matching the core specs."""

import json
from pathlib import Path

import pytest

from omnibutler.core.models import CAPABILITY_SPECS, Capability

DATA_DIR = Path(__file__).resolve().parent.parent / "device-data"
FILES = sorted(DATA_DIR.glob("*.json"))

REQUIRED_FIELDS = {
    "device_id", "brand", "model", "category", "protocol", "connection",
    "capabilities", "actions", "risk", "source", "provenance",
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_profiles_exist_and_are_unique():
    assert len(FILES) >= 10  # 2 bundled demos + contributed models
    ids = [_load(path)["device_id"] for path in FILES]
    assert len(ids) == len(set(ids)), f"duplicate device_id in {ids}"


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_profile_is_valid(path: Path):
    data = _load(path)
    assert REQUIRED_FIELDS <= set(data), (
        f"{path.name}: missing fields {REQUIRED_FIELDS - set(data)}"
    )
    assert data["risk"] in {"low", "medium", "high"}
    assert isinstance(data["source"], str) and data["source"].strip()
    provenance = data["provenance"]
    assert isinstance(provenance, dict)
    assert isinstance(provenance.get("verified_on_device"), bool)
    assert provenance.get("contributed_by")
    assert isinstance(data["actions"], list)
    assert all(isinstance(action, str) for action in data["actions"])
    assert isinstance(data["capabilities"], dict)

    for name, spec in data["capabilities"].items():
        capability = Capability(name)  # raises ValueError if not canonical
        assert spec["type"] == CAPABILITY_SPECS[capability].value_type, (
            f"{path.name}: {name} declares type {spec['type']!r}, core spec "
            f"says {CAPABILITY_SPECS[capability].value_type!r}"
        )
        assert isinstance(spec["writable"], bool)
        if "min" in spec or "max" in spec:
            assert isinstance(spec["min"], (int, float))
            assert isinstance(spec["max"], (int, float))
            assert spec["min"] <= spec["max"]
        if "options" in spec:
            assert spec["type"] == "enum"
            assert spec["options"] and all(
                isinstance(option, str) for option in spec["options"]
            )
        if "mapping" in spec:
            assert isinstance(spec["mapping"], dict)

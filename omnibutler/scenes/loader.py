"""Load and validate scene files (YAML).

Validation is deliberately strict and specific: a broken scene file must say
exactly which field is wrong, because scene files are meant to be authored
by humans and by AI agents alike.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnibutler.core.models import RiskLevel
from omnibutler.scenes.model import (
    COMPARISON_OPS,
    TRIGGER_TYPES,
    Scene,
    SceneAction,
    SceneCondition,
    SceneTrigger,
)


class SceneValidationError(ValueError):
    """Raised when a scene file does not match the scene schema."""


def _fail(scene_name: str, message: str) -> None:
    raise SceneValidationError(f"scene {scene_name!r}: {message}")


def parse_scene(raw: Any, source: str | None = None) -> Scene:
    if not isinstance(raw, dict):
        raise SceneValidationError(f"scene document must be a mapping, got {type(raw).__name__}")
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise SceneValidationError("scene is missing a non-empty string field 'name'")

    trigger_raw = raw.get("trigger")
    if not isinstance(trigger_raw, dict):
        _fail(name, "missing mapping field 'trigger'")
    trigger_type = trigger_raw.get("type")
    if trigger_type not in TRIGGER_TYPES:
        _fail(name, f"trigger.type must be one of {sorted(TRIGGER_TYPES)}, got {trigger_type!r}")
    trigger = SceneTrigger(
        type=trigger_type,
        device=trigger_raw.get("device"),
        property=trigger_raw.get("property"),
        at=trigger_raw.get("at"),
        every_minutes=trigger_raw.get("every_minutes"),
        zone=trigger_raw.get("zone"),
        transition=trigger_raw.get("transition"),
    )
    if trigger_type == "geofence":
        if not trigger.zone:
            _fail(name, "geofence trigger requires 'zone'")
        if trigger.transition not in {"enter", "exit"}:
            _fail(name, "geofence trigger requires transition 'enter' or 'exit'")
    if trigger_type == "schedule":
        if not trigger.at and not trigger.every_minutes:
            _fail(name, "schedule trigger requires 'at' (HH:MM) or 'every_minutes'")
        if trigger.at is not None:
            parts = str(trigger.at).split(":")
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                _fail(name, f"schedule 'at' must be HH:MM, got {trigger.at!r}")
    if trigger_type == "state_change" and not trigger.device:
        _fail(name, "state_change trigger requires 'device'")

    conditions: list[SceneCondition] = []
    for idx, cond_raw in enumerate(raw.get("conditions") or []):
        if not isinstance(cond_raw, dict):
            _fail(name, f"conditions[{idx}] must be a mapping")
        for field_name in ("device", "property"):
            if not cond_raw.get(field_name):
                _fail(name, f"conditions[{idx}] is missing {field_name!r}")
        op = cond_raw.get("op", "==")
        if op not in COMPARISON_OPS:
            _fail(name, f"conditions[{idx}].op must be one of {sorted(COMPARISON_OPS)}, got {op!r}")
        if op not in {"truthy", "falsy"} and "value" not in cond_raw:
            _fail(name, f"conditions[{idx}] with op {op!r} requires a 'value'")
        conditions.append(SceneCondition(
            device=cond_raw["device"], property=cond_raw["property"],
            op=op, value=cond_raw.get("value"),
        ))

    actions_raw = raw.get("actions")
    if not isinstance(actions_raw, list) or not actions_raw:
        _fail(name, "'actions' must be a non-empty list")
    actions: list[SceneAction] = []
    for idx, act_raw in enumerate(actions_raw):
        if not isinstance(act_raw, dict):
            _fail(name, f"actions[{idx}] must be a mapping")
        device = act_raw.get("device")
        if not device:
            _fail(name, f"actions[{idx}] is missing 'device'")
        if act_raw.get("action"):
            kind, action_name, prop_name = "action", act_raw["action"], None
            if "set" in act_raw:
                _fail(name, f"actions[{idx}] must not set both 'action' and 'set'")
            value = None
        elif "set" in act_raw:
            kind, action_name = "set", None
            set_raw = act_raw["set"]
            if not isinstance(set_raw, dict) or len(set_raw) != 1:
                _fail(name, f"actions[{idx}].set must be a mapping of exactly one property")
            prop_name, value = next(iter(set_raw.items()))
        else:
            _fail(name, f"actions[{idx}] needs either 'set: {{property: value}}' or 'action: <name>'")
        risk = None
        if act_raw.get("risk") is not None:
            try:
                risk = RiskLevel.parse(act_raw["risk"])
            except Exception:
                _fail(name, f"actions[{idx}].risk must be one of low, medium, high")
        actions.append(SceneAction(
            device=device, kind=kind, property=prop_name, value=value,
            action=action_name, params=dict(act_raw.get("params") or {}), risk=risk,
        ))

    risk = RiskLevel.LOW
    if raw.get("risk") is not None:
        try:
            risk = RiskLevel.parse(raw["risk"])
        except Exception:
            _fail(name, "'risk' must be one of low, medium, high")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        _fail(name, "'enabled' must be true or false")

    return Scene(
        name=name, trigger=trigger, actions=actions, conditions=conditions,
        risk=risk, enabled=enabled,
        description=str(raw.get("description", "")), source=source,
    )


def load_scene_file(path: str | Path) -> Scene:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SceneValidationError(f"{path}: invalid YAML: {exc}") from exc
    scene = parse_scene(raw, source=str(path))
    return scene


def load_scenes_dir(directory: str | Path) -> list[Scene]:
    directory = Path(directory)
    scenes: list[Scene] = []
    for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        scenes.append(load_scene_file(path))
    return scenes

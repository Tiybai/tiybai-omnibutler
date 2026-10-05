"""Load and validate scene files (YAML).

Validation is deliberately strict and specific: a broken scene file must say
exactly which field is wrong, because scene files are meant to be authored
by humans and by AI agents alike.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

import yaml

from omnibutler.core.models import RiskLevel
from omnibutler.scenes.model import (
    COMPARISON_OPS,
    CONDITION_TYPES,
    MAX_DELAY_SECONDS,
    OP_ALIASES,
    TRIGGER_TYPES,
    WEEKDAY_ABBREVIATIONS,
    Scene,
    SceneAction,
    SceneCondition,
    SceneDelay,
    SceneTrigger,
)


class SceneValidationError(ValueError):
    """Raised when a scene file does not match the scene schema."""


#: A scene file is hand-authored YAML; anything larger is a mistake
#: (or a data dump pointed at the wrong loader). Checked before the
#: file is read into memory.
MAX_SCENE_FILE_BYTES = 1024 * 1024


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys.

    Stock YAML silently keeps the *last* value of a repeated key, so a
    scene file with two ``trigger:`` blocks would quietly run only one
    of them. Here a duplicate is a validation error naming the key.
    """


def _construct_unique_mapping(loader: _UniqueKeyLoader,
                              node: yaml.MappingNode) -> dict[Any, Any]:
    loader.flatten_mapping(node)  # resolve merge keys before checking
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in mapping
        except TypeError:
            raise SceneValidationError(
                f"mapping key {key!r} is not a usable scalar") from None
        if duplicate:
            raise SceneValidationError(
                f"duplicate key {key!r} in scene mapping - YAML would "
                f"silently keep only the last value")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _fail(scene_name: str, message: str) -> NoReturn:
    raise SceneValidationError(f"scene {scene_name!r}: {message}")


def _parse_hhmm(value: Any, scene_name: str, field_name: str) -> str:
    """Normalise a time-of-day to "HH:MM" or fail with a specific message.

    Accepts "HH:MM" strings; bare ints are minutes since midnight (which is
    also what YAML 1.1 makes of an unquoted ``22:00`` - a sexagesimal int);
    ``datetime.time`` values come from unquoted ``HH:MM:SS`` scalars.
    """
    import datetime as _dt

    if isinstance(value, _dt.time):
        return f"{value.hour:02d}:{value.minute:02d}"
    if isinstance(value, bool):
        _fail(scene_name, f"{field_name} must be HH:MM, got {value!r}")
    if isinstance(value, int):
        if 0 <= value < 24 * 60:
            return f"{value // 60:02d}:{value % 60:02d}"
        _fail(scene_name, f"{field_name} minutes-since-midnight out of range: {value!r}")
    if isinstance(value, str):
        parts = value.strip().split(":")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            hour, minute = int(parts[0]), int(parts[1])
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{hour:02d}:{minute:02d}"
    _fail(scene_name, f"{field_name} must be HH:MM, got {value!r}")
    raise AssertionError("unreachable")  # _fail always raises


def _parse_days(value: Any, scene_name: str) -> frozenset[int]:
    """Validate a schedule trigger's ``days`` list.

    Accepts a non-empty list of three-letter lowercase weekday
    abbreviations (``mon`` .. ``sun``); returns the matching set of
    ``datetime.date.weekday()`` integers. Anything else fails the whole
    scene - a typo'd day must not silently turn into "every day".
    """
    if not isinstance(value, list) or not value:
        _fail(scene_name, "schedule trigger 'days' must be a non-empty list "
                          "of weekday abbreviations "
                          f"{sorted(WEEKDAY_ABBREVIATIONS)}, got {value!r}")
    days: set[int] = set()
    for item in value:
        if not isinstance(item, str) or item not in WEEKDAY_ABBREVIATIONS:
            _fail(scene_name, "schedule trigger 'days' entries must be one "
                              f"of {sorted(WEEKDAY_ABBREVIATIONS)}, "
                              f"got {item!r}")
        days.add(WEEKDAY_ABBREVIATIONS[item])
    return frozenset(days)


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
        if trigger_raw.get("days") is not None:
            trigger.days = _parse_days(trigger_raw["days"], name)
    if trigger_type == "state_change" and not trigger.device:
        _fail(name, "state_change trigger requires 'device'")

    conditions: list[SceneCondition] = []
    for idx, cond_raw in enumerate(raw.get("conditions") or []):
        if not isinstance(cond_raw, dict):
            _fail(name, f"conditions[{idx}] must be a mapping")
        window_raw = cond_raw.get("time_window")
        cond_type = cond_raw.get("type")
        if cond_type is None and window_raw is not None:
            cond_type = "time_window"
        if cond_type is None:
            cond_type = "state"
        if cond_type not in CONDITION_TYPES:
            _fail(name, f"conditions[{idx}].type must be one of "
                        f"{sorted(CONDITION_TYPES)}, got {cond_type!r}")
        if cond_type == "time_window":
            if cond_raw.get("for_seconds") is not None or (
                isinstance(window_raw, dict)
                and window_raw.get("for_seconds") is not None
            ):
                _fail(name, f"conditions[{idx}].for_seconds is only allowed "
                            "on state conditions")
            # Two spellings: flat {type: time_window, start, end} or nested
            # {time_window: {start, end}}.
            if window_raw is not None and not isinstance(window_raw, dict):
                _fail(name, f"conditions[{idx}].time_window must be a mapping "
                            "with 'start' and 'end'")
            window_source = window_raw if isinstance(window_raw, dict) else cond_raw
            if window_source.get("start") is None or window_source.get("end") is None:
                _fail(name, f"conditions[{idx}] time_window requires both "
                            "'start' and 'end' (HH:MM)")
            conditions.append(SceneCondition(
                type="time_window",
                start=_parse_hhmm(window_source["start"], name,
                                  f"conditions[{idx}].time_window.start"),
                end=_parse_hhmm(window_source["end"], name,
                                f"conditions[{idx}].time_window.end"),
            ))
            continue
        for field_name in ("device", "property"):
            if not cond_raw.get(field_name):
                _fail(name, f"conditions[{idx}] is missing {field_name!r}")
        op = OP_ALIASES.get(cond_raw.get("op", "=="), cond_raw.get("op", "=="))
        if op not in COMPARISON_OPS:
            _fail(name, f"conditions[{idx}].op must be one of "
                        f"{sorted(COMPARISON_OPS)}, got {cond_raw.get('op')!r}")
        if op not in {"truthy", "falsy"} and "value" not in cond_raw:
            _fail(name, f"conditions[{idx}] with op {op!r} requires a 'value'")
        for_seconds = cond_raw.get("for_seconds")
        if for_seconds is not None:
            if (isinstance(for_seconds, bool)
                    or not isinstance(for_seconds, (int, float))
                    or for_seconds <= 0):
                _fail(name, f"conditions[{idx}].for_seconds must be a "
                            f"positive number of seconds, got {for_seconds!r}")
            for_seconds = float(for_seconds)
        conditions.append(SceneCondition(
            device=cond_raw["device"], property=cond_raw["property"],
            op=op, value=cond_raw.get("value"),
            for_seconds=for_seconds,
        ))

    actions_raw = raw.get("actions")
    if not isinstance(actions_raw, list) or not actions_raw:
        _fail(name, "'actions' must be a non-empty list")
    actions: list[SceneAction | SceneDelay] = []
    for idx, act_raw in enumerate(actions_raw):
        if not isinstance(act_raw, dict):
            _fail(name, f"actions[{idx}] must be a mapping")
        if "delay" in act_raw:
            # A pause between actions: ``- delay: <seconds>``. It is the
            # only key allowed on the item - a delay has no device, no
            # value and no risk of its own.
            extra = sorted(set(act_raw) - {"delay"})
            if extra:
                _fail(name, f"actions[{idx}] 'delay' cannot be combined "
                            f"with {extra}")
            seconds = act_raw["delay"]
            if (isinstance(seconds, bool)
                    or not isinstance(seconds, (int, float))
                    or not 0 < seconds <= MAX_DELAY_SECONDS):
                _fail(name, f"actions[{idx}].delay must be a number of "
                            f"seconds greater than 0 and at most "
                            f"{MAX_DELAY_SECONDS:g}, got {seconds!r}")
            if idx == len(actions_raw) - 1:
                _fail(name, f"actions[{idx}] is a delay with nothing "
                            "after it - a delay must be followed by the "
                            "actions it postpones")
            actions.append(SceneDelay(seconds=float(seconds)))
            continue
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
            _fail(name, f"actions[{idx}] needs either 'set: {{property: value}}' "
                        f"or 'action: <name>'")
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
    size = path.stat().st_size
    if size > MAX_SCENE_FILE_BYTES:
        raise SceneValidationError(
            f"{path}: scene file is {size} bytes; the limit is "
            f"{MAX_SCENE_FILE_BYTES} (1 MiB) - a scene is a page of "
            f"YAML, not a data dump"
        )
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"),
                        Loader=_UniqueKeyLoader)
    except SceneValidationError as exc:
        raise SceneValidationError(f"{path}: {exc}") from exc
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

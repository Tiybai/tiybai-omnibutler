"""tob - the Tiybai OmniButler command line interface.

Examples:
    tob devices                 list devices (mock driver by default)
    tob devices --room bedroom  filter by room
    tob state living_ac         show a device's current state
    tob set living_ac onoff true
    tob set living_ac target_temperature 24
    tob scenes                  list and validate the bundled scenes
    tob simulate                fire a geofence 'arrive home' event and show the chain
    tob discover                aggregate driver discovery
    tob mcp                     serve MCP over stdio (for AI agents)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from omnibutler import __version__
from omnibutler.core.errors import OmniButlerError
from omnibutler.core.events import Event
from omnibutler.drivers.homeassistant import HomeAssistantDriver
from omnibutler.drivers.miio import MiioDriver
from omnibutler.drivers.mock import MockDriver
from omnibutler.drivers.tuya import TuyaDriver
from omnibutler.runtime import DEFAULT_SCENES_DIR, build_runtime
from omnibutler.scenes.loader import SceneValidationError, load_scenes_dir


def parse_value(text: str) -> Any:
    lowered = text.lower()
    if lowered in {"true", "on"}:
        return True
    if lowered in {"false", "off"}:
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return text


def _print_devices(devices, show_state: bool = False) -> None:
    if not devices:
        print("(no devices)")
        return
    for device in devices:
        flags = []
        if device.risk.value != "low":
            flags.append(f"risk:{device.risk.value}")
        if not device.online:
            flags.append("offline")
        caps = ", ".join(sorted(device.properties))
        print(f"{device.id:<18} {device.name:<22} room={device.room:<12} "
              f"driver={device.driver:<13} [{caps}] {' '.join(flags)}")
        if show_state:
            print(f"{'':<18} state: {json.dumps(device.state, ensure_ascii=False)}")


def cmd_devices(args) -> int:
    runtime = build_runtime(driver=args.driver, audit_path=args.audit)
    devices = runtime.manager.list_devices(room=args.room)
    _print_devices(devices, show_state=args.state)
    return 0


def cmd_state(args) -> int:
    runtime = build_runtime(driver=args.driver, audit_path=args.audit)
    device = runtime.manager.get_device(args.device_id)
    state = runtime.manager.get_state(args.device_id)
    print(f"{device.name} ({device.id}) room={device.room} risk={device.risk.value}")
    for key in sorted(state):
        print(f"  {key} = {state[key]!r}")
    return 0


def cmd_set(args) -> int:
    runtime = build_runtime(driver=args.driver, audit_path=args.audit)
    value = parse_value(args.value)
    result = runtime.manager.set_property(args.device_id, args.property, value, agent="cli")
    state = runtime.manager.get_state(args.device_id)
    print(f"ok: {args.device_id}.{args.property} = {value!r} -> {result}")
    print(f"state: {json.dumps(state, ensure_ascii=False)}")
    return 0


def cmd_scenes(args) -> int:
    scenes_dir = Path(args.dir) if args.dir else DEFAULT_SCENES_DIR
    try:
        scenes = load_scenes_dir(scenes_dir)
    except SceneValidationError as exc:
        print(f"scene validation failed: {exc}", file=sys.stderr)
        return 1
    if not scenes:
        print(f"(no scenes in {scenes_dir})")
        return 0
    for scene in scenes:
        trigger = scene.trigger
        detail = trigger.type
        if trigger.type == "geofence":
            detail += f" zone={trigger.zone} {trigger.transition}"
        elif trigger.type == "schedule":
            detail += f" at={trigger.at or ('every ' + str(trigger.every_minutes) + 'm')}"
        elif trigger.type == "state_change":
            detail += f" device={trigger.device}"
        status = "enabled" if scene.enabled else "disabled"
        print(f"{scene.name:<22} risk={scene.risk.value:<6} {status:<8} "
              f"trigger={detail:<38} conditions={len(scene.conditions)} "
              f"actions={len(scene.actions)}")
        if scene.description:
            print(f"{'':<22} {scene.description}")
    print(f"\n{len(scenes)} scene(s) valid in {scenes_dir}")
    return 0


def cmd_simulate(args) -> int:
    runtime = build_runtime(driver=args.driver, audit_path=args.audit)
    engine = runtime.engine
    if args.scene and args.scene not in engine.scenes:
        print(f"unknown scene {args.scene!r}; known: {sorted(engine.scenes)}",
              file=sys.stderr)
        return 1

    if args.event == "geofence":
        event = Event(type="geofence", source="simulate",
                      data={"zone": "home", "transition": "enter"})
        headline = "geofence: phone entered zone 'home'"
    elif args.event == "leave":
        event = Event(type="geofence", source="simulate",
                      data={"zone": "home", "transition": "exit"})
        headline = "geofence: phone left zone 'home'"
    elif args.event == "garage":
        event = Event(type="geofence", source="simulate",
                      data={"zone": "garage_gate", "transition": "enter"})
        headline = "geofence: car/phone entered zone 'garage_gate'"
    elif args.event == "pm25":
        runtime.manager.registry.get("air_purifier").state["pm25"] = 86
        event = Event(type="state_change", source="simulate",
                      data={"device": "air_purifier", "property": "pm25", "value": 86})
        headline = "state_change: air_purifier.pm25 = 86"
    else:  # schedule
        event = Event(type="schedule", source="simulate",
                      data={"time": args.time or "22:30", "minute": 30})
        headline = f"schedule: time = {args.time or '22:30'}"

    print(f"event: {headline}")
    print(f"driver: {runtime.driver_name}")
    report = engine.handle_event(event)
    wanted = {args.scene} if args.scene else None
    if report.evaluated:
        for name in report.evaluated:
            if wanted and name not in wanted:
                continue
            print(f"scene evaluated: {name}")
    for name, reason in report.skipped.items():
        if wanted and name not in wanted:
            continue
        if reason == "conditions not met":
            print(f"scene skipped: {name} ({reason})")
    shown = False
    for outcome in report.outcomes:
        if wanted and outcome.scene not in wanted:
            continue
        shown = True
        marker = {"executed": "EXECUTED", "queued": "QUEUED (needs confirmation)",
                  "failed": "FAILED"}[outcome.status]
        print(f"  [{marker}] {outcome.scene}: {outcome.description} -> {outcome.detail}")
    if not shown:
        print("  (no scene actions fired for this event)")
    pending = runtime.confirmations.pending()
    if pending:
        print("pending confirmations:")
        for item in pending:
            print(f"  {item.id}: {item.kind} {item.device_id}.{item.name} "
                  f"(scene={item.scene}, risk={item.risk})")
    print("device state after:")
    _print_devices(runtime.manager.list_devices(), show_state=True)
    return 0


def cmd_discover(args) -> int:
    print("mock driver:")
    mock_devices = MockDriver().discover()
    _print_devices(mock_devices)
    print("\nhomeassistant driver:")
    try:
        ha_devices = HomeAssistantDriver().discover()
        _print_devices(ha_devices)
    except OmniButlerError as exc:
        print(f"  (not available) {exc}")
    print("\nplanned drivers (not implemented in v0.1):")
    for driver in (MiioDriver(), TuyaDriver()):
        found = driver.discover()
        print(f"  {driver.name}: planned - returns {len(found)} devices for now; "
              f"see omnibutler/drivers/{driver.name if driver.name != 'miio' else 'miio'}.py")
    return 0


def cmd_mcp(args) -> int:
    runtime = build_runtime(driver=args.driver, audit_path=args.audit)
    from omnibutler.mcp_server.server import create_server, run_stdio

    run_stdio(create_server(runtime.manager, engine=runtime.engine,
                            confirmations=runtime.confirmations))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tob", description="Tiybai OmniButler CLI")
    parser.add_argument("--version", action="version", version=f"tob {__version__}")
    parser.add_argument("--driver", choices=["mock", "homeassistant"], default=None,
                        help="device driver (default: mock, or $TOB_DRIVER)")
    parser.add_argument("--audit", default=None,
                        help="audit log path (default: $TOB_AUDIT_PATH or ~/.omnibutler/audit.jsonl)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("devices", help="list devices")
    p.add_argument("--room", default=None)
    p.add_argument("--state", action="store_true", help="also print current state")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("state", help="show one device's state")
    p.add_argument("device_id")
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("set", help="set a device property")
    p.add_argument("device_id")
    p.add_argument("property")
    p.add_argument("value")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("scenes", help="list and validate scene files")
    p.add_argument("--dir", default=None, help="scene directory (default: bundled examples)")
    p.set_defaults(func=cmd_scenes)

    p = sub.add_parser("simulate", help="run a simulated event through the scene engine")
    p.add_argument("--event", choices=["geofence", "leave", "schedule", "pm25", "garage"],
                   default="geofence")
    p.add_argument("--scene", default=None, help="only show this scene")
    p.add_argument("--time", default=None, help="HH:MM for schedule events")
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("discover", help="aggregate discovery across drivers")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("mcp", help="run the MCP server on stdio (for AI agents)")
    p.set_defaults(func=cmd_mcp)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except OmniButlerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

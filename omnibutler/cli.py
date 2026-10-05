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
    tob mcp --http              serve MCP over HTTP (token auth, remote agents)
    tob run                     run the butler daemon (schedule + state polling)
    tob doctor                  health-check drivers, credentials and scenes
    tob setup miio              plain-language guide to getting a device key
    tob pending                 list high-risk actions waiting for a human (host only)
    tob approvals               serve the approvals web page (human clicks, host only)
    tob confirm cfm-0001        approve a queued high-risk action (host only)
    tob reject cfm-0001         reject a queued high-risk action (host only)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
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
    from omnibutler.drivers.broadlink import BroadlinkDriver
    from omnibutler.drivers.midea import MideaDriver

    print("mock driver:")
    _print_devices(MockDriver().discover())
    for cls in (HomeAssistantDriver, MiioDriver, TuyaDriver,
                BroadlinkDriver, MideaDriver):
        driver = cls()
        print(f"\n{driver.name} driver:")
        try:
            _print_devices(driver.discover())
        except Exception as exc:  # not configured / dependency missing / offline
            print(f"  (not available) {exc}")
    return 0


def cmd_mcp(args) -> int:
    runtime = build_runtime(driver=args.driver, audit_path=args.audit)
    from omnibutler.mcp_server.server import create_server, run_stdio

    server = create_server(runtime.manager, engine=runtime.engine,
                           confirmations=runtime.confirmations)
    if args.http:
        from omnibutler.mcp_server.http_transport import serve

        print(f"serving MCP over HTTP on {args.host}:{args.port} "
              f"(token from $OMNIBUTLER_HTTP_TOKEN required)")
        serve(server, host=args.host, port=args.port)
    else:
        run_stdio(server)
    return 0


def cmd_run(args) -> int:
    runtime = build_runtime(driver=args.driver, audit_path=args.audit)
    from omnibutler.daemon import run_daemon

    print(f"omnibutler running with driver={runtime.driver_name} "
          f"(poll every {args.poll}s; Ctrl-C to stop)")
    if args.approvals_port > 0:
        print(f"approvals page: http://{args.host}:{args.approvals_port}/ "
              f"(token from $OMNIBUTLER_APPROVALS_TOKEN)")
    if args.notify:
        import sys as _sys

        if _sys.platform == "darwin":
            print("macOS dialog popups enabled for new pending confirmations")
        else:
            print("note: --notify needs macOS; dialogs are unavailable on "
                  f"{_sys.platform}, continuing without popups")
    if args.gateway:
        print(f"phone gateway: http://{args.gateway_host}:{args.gateway_port}/ "
              f"(token from $OMNIBUTLER_GATEWAY_TOKEN; without it the "
              f"gateway stays off and the daemon keeps running)")
    try:
        stats = run_daemon(runtime, poll_interval=args.poll,
                           approvals_port=args.approvals_port,
                           approvals_host=args.host, notify=args.notify,
                           gateway_port=args.gateway_port if args.gateway else 0,
                           gateway_host=args.gateway_host)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"stopped: {json.dumps(stats, ensure_ascii=False, default=str)}")
    return 0


def cmd_approvals(args) -> int:
    runtime = build_runtime(driver=args.driver, audit_path=args.audit)
    from omnibutler.approvals_web import ApprovalsWebError, serve

    print(f"approvals page: http://{args.host}:{args.port}/ "
          f"(open with ?token=<your $OMNIBUTLER_APPROVALS_TOKEN>; "
          f"Ctrl-C to stop)")
    try:
        serve(runtime.engine, runtime.confirmations, runtime.manager,
              host=args.host, port=args.port)
    except ApprovalsWebError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_doctor(args) -> int:
    runtime = build_runtime(driver=args.driver, audit_path=args.audit)
    from omnibutler.doctor import check_all, format_report

    results = check_all(runtime)
    print(format_report(results))
    return 1 if any(r.status == "fail" for r in results) else 0


def cmd_gateway(args) -> int:
    runtime = build_runtime(driver=args.driver, audit_path=args.audit)
    from omnibutler.gateway import serve

    print(f"phone gateway on {args.host}:{args.port} "
          f"(token from $OMNIBUTLER_GATEWAY_TOKEN required)")
    serve(runtime.bus, runtime.streams, host=args.host, port=args.port)
    return 0


def cmd_streams(args) -> int:
    runtime = build_runtime(driver="mock", load_default_scenes=False)
    store = runtime.streams
    if args.stream_id:
        points = store.history(args.stream_id)
        if not points:
            print(f"no data for stream {args.stream_id!r}")
            return 0
        for point in points[-args.limit:]:
            print(f"  {point.ts}  {point.value}")
        return 0
    streams = store.streams()
    if not streams:
        print("no data streams yet (the phone gateway writes them)")
        return 0
    for stream in streams:
        latest = store.latest(stream.id)
        latest_text = (f"latest: {latest.value} @ {latest.ts}"
                       if latest is not None else "no points yet")
        print(f"  {stream.id} ({stream.kind}, source={stream.source}) "
              f"{latest_text}")
    return 0


def cmd_onboard(args) -> int:
    import os

    from omnibutler.onboard import config_draft, format_report, scan
    from omnibutler.runtime import _build_drivers

    drivers = {"mock": MockDriver()}
    drivers.update(_build_drivers("all"))
    found = scan(drivers)
    print(format_report(found))
    if args.write_draft:
        path = Path(os.path.expanduser(args.write_draft))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config_draft(found), ensure_ascii=False,
                                   indent=2), encoding="utf-8")
        print(f"\nconfig draft written to {path} "
              f"(fill in the env: values, then copy what you need into "
              f"~/.omnibutler/config.json)")
    return 0


def _merge_config_devices(section: str, entries: list[dict]) -> Path:
    import os

    config_path = Path(os.environ.get(
        "OMNIBUTLER_CONFIG", str(Path.home() / ".omnibutler" / "config.json")))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
    section_data = data.setdefault(section, {})
    devices = section_data.setdefault("devices", [])
    by_key = {d.get("host") or d.get("device_id"): i
              for i, d in enumerate(devices) if isinstance(d, dict)}
    for entry in entries:
        key = entry.get("host") or entry.get("device_id")
        if key in by_key:
            devices[by_key[key]].update(entry)
        else:
            devices.append(entry)
    # Write through the canonical config writer (version stamp,
    # one-time .bak of a pre-versioning file, atomic 0600).
    from omnibutler.config import save_config

    save_config(config_path, data)
    return config_path


def cmd_fetch_keys(args) -> int:
    import getpass
    import os

    from omnibutler.cloud_keys import CloudKeyError, fetch_tuya_local_keys, \
        fetch_xiaomi_tokens

    try:
        if args.brand == "xiaomi":
            username = os.environ.get("XIAOMI_USERNAME") or input(
                "Xiaomi account (email/phone): ").strip()
            password = os.environ.get("XIAOMI_PASSWORD") or getpass.getpass("Xiaomi password (hidden): ")
            devices = fetch_xiaomi_tokens(username, password)
            if not devices:
                print("no devices with a local token were returned")
                return 0
            for d in devices:
                print(f"  {d['name']} ({d['model']}) ip={d['ip']} "
                      f"token=...{d['token'][-4:]}")
            if args.store:
                entries = [{"id": d["did"], "name": d["name"],
                            "host": d["ip"], "token": d["token"]}
                           for d in devices]
                path = _merge_config_devices("miio", entries)
                print(f"stored {len(entries)} device(s) in {path} (0600)")
            else:
                print("re-run with --store to save them into the local config")
            return 0
        # tuya
        access_id = os.environ.get("TUYA_ACCESS_ID") or \
            input("Tuya IoT access ID: ").strip()
        access_secret = os.environ.get("TUYA_ACCESS_SECRET") or getpass.getpass("Tuya access secret (hidden): ")
        uid = os.environ.get("TUYA_UID") or input("Tuya user UID: ").strip()
        devices = fetch_tuya_local_keys(access_id, access_secret, uid)
        if not devices:
            print("no devices with a local_key were returned")
            return 0
        for d in devices:
            print(f"  {d['name']} id={d['device_id']} "
                  f"local_key=...{d['local_key'][-4:]}")
        if args.store:
            entries = [{"id": d["device_id"], "name": d["name"],
                        "device_id": d["device_id"], "ip": "",
                        "local_key": d["local_key"]} for d in devices]
            path = _merge_config_devices("tuya", entries)
            print(f"stored {len(entries)} device(s) in {path} (0600); "
                  f"fill each device's LAN ip before use")
        else:
            print("re-run with --store to save them into the local config")
        return 0
    except CloudKeyError as exc:
        print(f"error ({exc.kind}): {exc}", file=sys.stderr)
        return 1


def cmd_setup(args) -> int:
    from omnibutler.setup_guide import guide_text

    print(guide_text(args.brand))
    return 0


def cmd_setup_secret(args) -> int:
    import getpass
    import os

    from omnibutler.setup_guide import store_secret

    config_path = os.environ.get(
        "OMNIBUTLER_CONFIG", str(Path.home() / ".omnibutler" / "config.json"))
    value = getpass.getpass(f"value for {args.key_path} (hidden): ")
    if not value:
        print("nothing entered; aborted", file=sys.stderr)
        return 1
    store_secret(config_path, args.key_path, value)
    print(f"stored {args.key_path} in {config_path} (permissions 0600, "
          f"value not shown)")
    return 0


# -- human-only confirmation commands ----------------------------------------
# Approving or rejecting a high-risk action is deliberately *not* available
# through the MCP server (agents can only read the queue). These commands run
# in a terminal on the host and share the persisted queue with every other
# process via OMNIBUTLER_STATE_DIR (default ~/.omnibutler).

def _format_wait(created_at: float) -> str:
    seconds = max(0, int(time.time() - created_at))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h"


def _describe_item(item) -> str:
    if item.kind == "set_property":
        what = f"set {item.device_id}.{item.name} = {item.value!r}"
    else:
        what = f"call {item.device_id}.{item.name}({item.params})"
    origin = item.scene or item.requested_by or "-"
    return f"{item.id}: {what} risk={item.risk} from={origin}"


def cmd_pending(args) -> int:
    runtime = build_runtime(driver=args.driver, audit_path=args.audit)
    pending = runtime.confirmations.pending()
    if not pending:
        print("(no pending confirmations)")
        return 0
    print(f"{len(pending)} pending confirmation(s):")
    for item in pending:
        print(f"  {_describe_item(item)} waiting={_format_wait(item.created_at)}")
    print("approve with: tob confirm <id>   reject with: tob reject <id>")
    return 0


def cmd_confirm(args) -> int:
    runtime = build_runtime(driver=args.driver, audit_path=args.audit)
    item = runtime.confirmations.get(args.confirmation_id)
    if item is None or item.status != "pending":
        print(f"error: no pending confirmation {args.confirmation_id!r}",
              file=sys.stderr)
        return 1
    result = runtime.engine.confirm(item.id, agent="cli:human")
    if result is None:
        print(f"error: confirmation {item.id} could not be executed",
              file=sys.stderr)
        return 1
    runtime.manager.audit.record(
        agent="cli:human", device_id=item.device_id,
        action="confirmation:approved",
        params={"confirmation_id": item.id, "kind": item.kind,
                "name": item.name, "value": item.value,
                "requested_by": item.requested_by, "scene": item.scene},
        result={"confirmation_id": item.id, "decision": "approved"},
    )
    print(f"confirmed and executed: {_describe_item(item)}")
    print(f"result: {json.dumps(result, ensure_ascii=False, default=str)}")
    state = runtime.manager.get_state(item.device_id)
    print(f"state: {json.dumps(state, ensure_ascii=False)}")
    return 0


def cmd_reject(args) -> int:
    runtime = build_runtime(driver=args.driver, audit_path=args.audit)
    item = runtime.confirmations.get(args.confirmation_id)
    if item is None or item.status != "pending":
        print(f"error: no pending confirmation {args.confirmation_id!r}",
              file=sys.stderr)
        return 1
    if not runtime.engine.reject(item.id):
        print(f"error: confirmation {item.id} could not be rejected",
              file=sys.stderr)
        return 1
    runtime.manager.audit.record(
        agent="cli:human", device_id=item.device_id,
        action="confirmation:rejected",
        params={"confirmation_id": item.id, "kind": item.kind,
                "name": item.name, "value": item.value,
                "requested_by": item.requested_by, "scene": item.scene},
        result={"confirmation_id": item.id, "decision": "rejected"},
    )
    print(f"rejected: {_describe_item(item)} (nothing was executed)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tob", description="Tiybai OmniButler CLI")
    parser.add_argument("--version", action="version", version=f"tob {__version__}")
    parser.add_argument("--driver", choices=["mock", "homeassistant", "miio",
                                             "tuya", "tuya_cloud", "broadlink",
                                             "midea", "matter", "zigbee2mqtt",
                                             "terminal_mock", "all"],
                        default=None,
                        help="device driver set (default: mock, or $TOB_DRIVER)")
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

    p = sub.add_parser("mcp", help="run the MCP server (for AI agents)")
    p.add_argument("--http", action="store_true",
                   help="serve over HTTP instead of stdio (needs $OMNIBUTLER_HTTP_TOKEN)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("run", help="run the butler daemon (schedule + state polling)")
    p.add_argument("--poll", type=int, default=30,
                   help="device state poll interval in seconds (default 30)")
    p.add_argument("--approvals-port", type=int, default=0,
                   help="also serve the approvals web page on this port "
                        "(needs $OMNIBUTLER_APPROVALS_TOKEN)")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind host for the approvals page (default 127.0.0.1)")
    p.add_argument("--notify", action="store_true",
                   help="pop a macOS dialog for each new pending confirmation "
                        "(macOS only; noted and skipped elsewhere)")
    p.add_argument("--gateway", action="store_true",
                   help="also serve the phone gateway in this process "
                        "(needs $OMNIBUTLER_GATEWAY_TOKEN; without it the "
                        "gateway stays off, the daemon keeps running)")
    p.add_argument("--gateway-host", default="127.0.0.1",
                   help="bind host for the phone gateway (default 127.0.0.1)")
    p.add_argument("--gateway-port", type=int, default=8767,
                   help="port for the phone gateway (default 8767)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("approvals", help="serve the human approvals web page "
                                         "(needs $OMNIBUTLER_APPROVALS_TOKEN)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)
    p.set_defaults(func=cmd_approvals)

    p = sub.add_parser("doctor", help="health-check drivers, credentials and scenes")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("setup", help="plain-language guide to getting a device key")
    p.add_argument("brand", choices=["miio", "xiaomi", "tuya", "ha", "homeassistant"])
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("setup-secret", help="store a device key in the local config (hidden input)")
    p.add_argument("key_path", help="dotted path, e.g. miio.devices.0.token")
    p.set_defaults(func=cmd_setup_secret)

    p = sub.add_parser("gateway", help="run the phone gateway (data ingest + geofence events)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8767)
    p.set_defaults(func=cmd_gateway)

    p = sub.add_parser("streams", help="show data streams collected via the gateway")
    p.add_argument("stream_id", nargs="?", default=None,
                   help="show recent points for this stream instead of listing")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_streams)

    p = sub.add_parser("onboard", help="scan all drivers and report what each found device still needs")
    p.add_argument("--write-draft", default=None, metavar="PATH",
                   help="also write a config draft JSON to PATH")
    p.set_defaults(func=cmd_onboard)

    p = sub.add_parser("fetch-keys", help="fetch device keys from the vendor cloud once (Xiaomi tokens / Tuya local_keys)")
    p.add_argument("brand", choices=["xiaomi", "tuya"])
    p.add_argument("--store", action="store_true",
                   help="merge the fetched keys into the local config (0600)")
    p.set_defaults(func=cmd_fetch_keys)

    p = sub.add_parser("pending", help="list high-risk actions awaiting a human (host only)")
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("confirm", help="approve and execute a queued high-risk action (host only)")
    p.add_argument("confirmation_id")
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser("reject", help="reject a queued high-risk action (host only)")
    p.add_argument("confirmation_id")
    p.set_defaults(func=cmd_reject)
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

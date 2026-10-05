"""A minimal Model Context Protocol server over stdio.

Implements the JSON-RPC core of MCP (spec revision 2024-11-05) with the
standard library only: initialize, ping, tools/list and tools/call. One JSON
message per line on stdin/stdout, as MCP stdio transports expect.

Safety note: set_device_property / call_device_action refuse devices whose
risk is *high*. Such operations can only be parked in the confirmation
queue (e.g. by a scene), and confirming them is a **human-only, out-of-band
action**: a person runs ``tob confirm <id>`` in a terminal on the host.
This server exposes the queue read-only (get_pending_confirmations) and
refuses confirm_action outright, so an agent can never approve its own
high-risk requests.

Two read/write surfaces sit next to device control:

* **Data streams** (list_data_streams / get_stream_data) - the phone and
  watch time series the gateway ingests. Read-only by design: there is
  no tool that writes or deletes stream data.
* **Terminal sessions** (list/open/close_terminal_session) - a session
  on a pair of glasses or a watch is a conversation, not a device
  control action, so opening one does not go through the confirmation
  queue. Every transition is still audited - by the SessionManager
  itself, which shares this process's audit log.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, TextIO

from omnibutler import __version__
from omnibutler.core.confirmations import ConfirmationQueue
from omnibutler.core.errors import OmniButlerError
from omnibutler.core.manager import DeviceManager
from omnibutler.core.models import RiskLevel
from omnibutler.core.sessions import SessionManager
from omnibutler.core.streams import StreamStore
from omnibutler.scenes.engine import SceneEngine

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "tiybai-omnibutler", "version": __version__}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_devices",
        "description": "List all devices the bridge knows, optionally filtered by room.",
        "inputSchema": {
            "type": "object",
            "properties": {"room": {"type": "string",
                                    "description": "Only return devices in this room."}},
        },
    },
    {
        "name": "get_device_state",
        "description": "Get the current property values of one device.",
        "inputSchema": {
            "type": "object",
            "properties": {"device_id": {"type": "string"}},
            "required": ["device_id"],
        },
    },
    {
        "name": "set_device_property",
        "description": "Set one writable property of a device (high-risk devices "
                       "are refused and must go through human confirmation).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string"},
                "property": {"type": "string"},
                "value": {},
            },
            "required": ["device_id", "property", "value"],
        },
    },
    {
        "name": "call_device_action",
        "description": "Run a named action on a device, e.g. turn_on, open, measure "
                       "(high-risk devices are refused, see set_device_property).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string"},
                "action": {"type": "string"},
                "params": {"type": "object"},
            },
            "required": ["device_id", "action"],
        },
    },
    {
        "name": "list_scenes",
        "description": "List the deterministic scenes loaded in the scene engine.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "enable_scene",
        "description": "Enable or disable a scene by name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "enabled": {"type": "boolean"},
            },
            "required": ["name", "enabled"],
        },
    },
    {
        "name": "get_pending_confirmations",
        "description": "List high-risk actions waiting for human confirmation (read-only: "
                       "only a human on the host can approve them, via `tob confirm <id>`).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_data_streams",
        "description": "List the read-only data streams the bridge has received "
                       "(things a phone or watch reports over time: steps, sleep, heart rate, "
                       "location), each with its latest value.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_stream_data",
        "description": "Read one data stream: its latest value plus the most recent "
                       "history points (oldest first). Use list_data_streams to find stream ids.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "stream_id": {"type": "string",
                              "description": "Stream id, e.g. 'phone-steps'."},
                "limit": {"type": "integer",
                          "description": ("How many history points to return "
                                          "(default 20, max 200).")},
            },
            "required": ["stream_id"],
        },
    },
    {
        "name": "list_terminal_sessions",
        "description": "List terminal sessions that are currently open (a conversation with "
                       "a pair of glasses, a watch or another terminal the AI talks through).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "open_terminal_session",
        "description": "Open a session on a terminal device (glasses, watch, earbuds, phone) "
                       "and mark it active. This starts a conversation; it does not control "
                       "any home device.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string",
                              "description": "Terminal device id, e.g. 'glasses'."},
                "kind": {"type": "string",
                         "description": "Terminal kind: glasses, watch, earbuds or phone."},
            },
            "required": ["device_id", "kind"],
        },
    },
    {
        "name": "close_terminal_session",
        "description": "Close a terminal session opened earlier (get its id from "
                       "open_terminal_session or list_terminal_sessions).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
            },
            "required": ["session_id"],
        },
    },
]

#: History points returned by get_stream_data when no limit is given,
#: and the hard cap when one is.
STREAM_HISTORY_DEFAULT = 20
STREAM_HISTORY_MAX = 200


def _tool_result(payload: Any, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}
        ],
        "isError": is_error,
    }


class McpServer:
    def __init__(
        self,
        manager: DeviceManager,
        engine: SceneEngine | None = None,
        confirmations: ConfirmationQueue | None = None,
        sessions: SessionManager | None = None,
        streams: StreamStore | None = None,
    ) -> None:
        self.manager = manager
        self.confirmations = confirmations or (
            engine.confirmations if engine is not None else ConfirmationQueue()
        )
        self.engine = engine or SceneEngine(manager, self.confirmations)
        # Wiring defaults: assembly points that do not pass these in
        # (the CLI passes only manager/engine/confirmations) still get
        # working tools. The default SessionManager shares the device
        # manager's event bus and audit log - the same bus the runtime
        # attached the scene engine to - so a session opened over MCP
        # announces itself (session_opened) exactly like one opened
        # anywhere else in the process, and its audit trail lands in
        # the same log.
        self.sessions = sessions or SessionManager(
            bus=manager.bus, audit=manager.audit)
        # Streams are different: the gateway usually runs in its own
        # process and keeps appending to the shared streams.jsonl, so a
        # long-lived in-memory snapshot would go stale. Without an
        # injected store, each call re-reads the file (phone-rate data;
        # the file is small). An injected store is used as-is.
        self._streams = streams

    def _stream_store(self) -> StreamStore:
        return self._streams if self._streams is not None else StreamStore()

    # -- JSON-RPC plumbing --------------------------------------------------
    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method", "")
        msg_id = message.get("id")
        params = message.get("params") or {}
        is_notification = msg_id is None

        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            }
            return self._response(msg_id, result)
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return self._response(msg_id, {})
        if method == "tools/list":
            return self._response(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            try:
                result = self._call_tool(params.get("name", ""), params.get("arguments") or {})
            except OmniButlerError as exc:
                result = _tool_result({"error": str(exc)}, is_error=True)
            except Exception as exc:  # never crash the transport on a bad call
                result = _tool_result({"error": f"internal error: {exc}"}, is_error=True)
            return self._response(msg_id, result)
        if is_notification:
            return None
        return self._error(msg_id, -32601, f"method not found: {method}")

    @staticmethod
    def _response(msg_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": code, "message": message}}

    # -- tools ----------------------------------------------------------------
    def _guard_high_risk(self, device_id: str) -> None:
        device = self.manager.get_device(device_id)
        if device.risk is RiskLevel.HIGH:
            raise OmniButlerError(
                f"device {device_id!r} is high-risk; direct control is refused. "
                "High-risk operations must be requested through a scene or a "
                "human, then approved via the confirmation queue."
            )

    def _call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "list_devices":
            devices = self.manager.list_devices(room=args.get("room"))
            return _tool_result([d.to_dict() for d in devices])
        if name == "get_device_state":
            device_id = self._required(args, "device_id")
            device = self.manager.get_device(device_id)
            state = self.manager.get_state(device_id)
            return _tool_result({"device": device.to_dict(include_state=False),
                                 "state": state})
        if name == "set_device_property":
            device_id = self._required(args, "device_id")
            prop = self._required(args, "property")
            if "value" not in args:
                raise OmniButlerError("set_device_property requires 'value'")
            self._guard_high_risk(device_id)
            result = self.manager.set_property(
                device_id, prop, args["value"], agent="mcp"
            )
            return _tool_result({"ok": True, "result": result})
        if name == "call_device_action":
            device_id = self._required(args, "device_id")
            action = self._required(args, "action")
            self._guard_high_risk(device_id)
            result = self.manager.call_action(
                device_id, action, args.get("params") or {}, agent="mcp"
            )
            return _tool_result({"ok": True, "result": result})
        if name == "list_scenes":
            return _tool_result([
                {
                    "name": s.name, "enabled": s.enabled, "risk": s.risk.value,
                    "description": s.description,
                    "trigger": s.trigger.type,
                    "actions": len(s.actions),
                }
                for s in self.engine.list_scenes()
            ])
        if name == "enable_scene":
            scene_name = self._required(args, "name")
            if scene_name not in self.engine.scenes:
                raise OmniButlerError(f"unknown scene {scene_name!r}")
            scene = self.engine.set_enabled(scene_name, bool(args.get("enabled", True)))
            return _tool_result({"name": scene.name, "enabled": scene.enabled})
        if name == "get_pending_confirmations":
            return _tool_result([i.to_dict() for i in self.confirmations.pending()])
        if name == "list_data_streams":
            store = self._stream_store()
            listing = []
            for stream in store.streams():
                entry = stream.to_dict()
                latest = store.latest(stream.id)
                entry["latest"] = (
                    {"ts": latest.ts, "value": latest.value}
                    if latest is not None else None
                )
                listing.append(entry)
            return _tool_result(listing)
        if name == "get_stream_data":
            stream_id = self._required(args, "stream_id")
            limit = self._history_limit(args.get("limit"))
            store = self._stream_store()
            found_stream = store.get_stream(stream_id)
            if found_stream is None:
                raise OmniButlerError(f"unknown data stream {stream_id!r}")
            points = store.history(stream_id)[-limit:]
            latest = store.latest(stream_id)
            return _tool_result({
                "stream": found_stream.to_dict(),
                "latest": (
                    {"ts": latest.ts, "value": latest.value}
                    if latest is not None else None
                ),
                "history": [{"ts": p.ts, "value": p.value} for p in points],
            })
        if name == "list_terminal_sessions":
            now = time.time()
            return _tool_result([
                {
                    "id": s.id,
                    "device_id": s.device_id,
                    "kind": s.kind,
                    "state": s.state.value,
                    "started_at": s.started_at,
                    "idle_seconds": round(s.idle_seconds(now), 1),
                }
                for s in self.sessions.list_active()
            ])
        if name == "open_terminal_session":
            device_id = self._required(args, "device_id")
            kind = self._required(args, "kind")
            # Not a device-control action, so no confirmation queue -
            # and no extra audit here: SessionManager records
            # session:opened / session:activated itself.
            session = self.sessions.open_session(device_id, kind, agent="mcp")
            session = self.sessions.activate(session.id, agent="mcp")
            return _tool_result({"ok": True, "session_id": session.id,
                                 "session": session.to_dict()})
        if name == "close_terminal_session":
            session_id = self._required(args, "session_id")
            session = self.sessions.close(session_id, agent="mcp")
            return _tool_result({"ok": True, "session": session.to_dict()})
        if name == "confirm_action":
            # Human-only, out-of-band: agents must never approve (or reject)
            # high-risk actions. The host terminal command `tob confirm <id>`
            # / `tob reject <id>` is the only approval channel.
            return _tool_result(
                {"error": "confirmation is a human-only action: "
                          "run `tob confirm <id>` on the host"},
                is_error=True,
            )
        raise OmniButlerError(f"unknown tool {name!r}")

    @staticmethod
    def _required(args: dict[str, Any], key: str) -> Any:
        value = args.get(key)
        if value is None or value == "":
            raise OmniButlerError(f"missing required argument {key!r}")
        return value

    @staticmethod
    def _history_limit(raw: Any) -> int:
        if raw is None:
            return STREAM_HISTORY_DEFAULT
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise OmniButlerError("get_stream_data 'limit' must be an integer")
        return max(1, min(raw, STREAM_HISTORY_MAX))


def create_server(
    manager: DeviceManager,
    engine: SceneEngine | None = None,
    confirmations: ConfirmationQueue | None = None,
    sessions: SessionManager | None = None,
    streams: StreamStore | None = None,
) -> McpServer:
    return McpServer(manager, engine=engine, confirmations=confirmations,
                     sessions=sessions, streams=streams)


def run_stdio(server: McpServer, stdin: TextIO | None = None,
              stdout: TextIO | None = None) -> None:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = server.handle(message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()

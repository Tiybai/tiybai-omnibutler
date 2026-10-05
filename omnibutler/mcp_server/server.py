"""A minimal Model Context Protocol server over stdio.

Implements the JSON-RPC core of MCP (spec revision 2024-11-05) with the
standard library only: initialize, ping, tools/list and tools/call. One JSON
message per line on stdin/stdout, as MCP stdio transports expect.

Safety note: set_device_property / call_device_action refuse devices whose
risk is *high* unless a human first parks them through the confirmation flow
(confirm_action works on queued items, e.g. from scenes). Agents get the
guardrail, not a bypass.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from omnibutler import __version__
from omnibutler.core.confirmations import ConfirmationQueue
from omnibutler.core.errors import OmniButlerError
from omnibutler.core.manager import DeviceManager
from omnibutler.core.models import RiskLevel
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
        "description": "Set one writable property of a device (high-risk devices are refused and must go through human confirmation).",
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
        "description": "Run a named action on a device, e.g. turn_on, open, measure (high-risk devices are refused, see set_device_property).",
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
        "description": "List high-risk actions waiting for human confirmation.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "confirm_action",
        "description": "Confirm (execute) or reject a pending high-risk action by its confirmation id. Confirming is a human decision; agents should surface the pending item to the user instead of confirming on their own.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "confirmation_id": {"type": "string"},
                "approve": {"type": "boolean", "default": True},
            },
            "required": ["confirmation_id"],
        },
    },
]


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
    ) -> None:
        self.manager = manager
        self.confirmations = confirmations or (
            engine.confirmations if engine is not None else ConfirmationQueue()
        )
        self.engine = engine or SceneEngine(manager, self.confirmations)

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
        if name == "confirm_action":
            confirmation_id = self._required(args, "confirmation_id")
            if args.get("approve", True):
                result = self.engine.confirm(confirmation_id, agent="mcp-human")
                if result is None:
                    raise OmniButlerError(
                        f"no pending confirmation {confirmation_id!r}")
                return _tool_result({"confirmed": confirmation_id, "result": result})
            if not self.engine.reject(confirmation_id):
                raise OmniButlerError(f"no pending confirmation {confirmation_id!r}")
            return _tool_result({"rejected": confirmation_id})
        raise OmniButlerError(f"unknown tool {name!r}")

    @staticmethod
    def _required(args: dict[str, Any], key: str) -> Any:
        value = args.get(key)
        if value is None or value == "":
            raise OmniButlerError(f"missing required argument {key!r}")
        return value


def create_server(
    manager: DeviceManager,
    engine: SceneEngine | None = None,
    confirmations: ConfirmationQueue | None = None,
) -> McpServer:
    return McpServer(manager, engine=engine, confirmations=confirmations)


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

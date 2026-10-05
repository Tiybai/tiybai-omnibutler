"""Append-only JSONL audit log for every control call.

Every property change and action - whether it came from the CLI, an MCP tool
call, or a scene - is recorded with who asked, what was requested, and what
happened. The log is local-only by design; see docs/security.md.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


def default_audit_path() -> Path:
    override = os.environ.get("TOB_AUDIT_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".omnibutler" / "audit.jsonl"


class AuditLog:
    def __init__(self, path: str | Path | None = None, enabled: bool = True) -> None:
        self.path = Path(path) if path is not None else default_audit_path()
        self.enabled = enabled
        self._lock = threading.Lock()

    def record(
        self,
        agent: str,
        device_id: str,
        action: str,
        params: dict[str, Any] | None = None,
        result: Any = None,
        ok: bool = True,
        error: str | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": time.time(),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "agent": agent,
            "device": device_id,
            "action": action,
            "params": params or {},
            "ok": ok,
            "result": result,
            "error": error,
        }
        if self.enabled:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return entry

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        entries = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
        return entries

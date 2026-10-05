"""Confirmation queue for high-risk actions.

High-risk actions (garage doors, locks, gas valves) are never executed
directly by scenes or agents. They are parked here until a human confirms
them *on the host* (``tob confirm <id>``), which is the bridge's main
safety guardrail. Agents - including the MCP server - can only read the
queue; there is deliberately no programmatic approve path.

The queue is persisted to ``confirmations.json`` in the state directory
(``$OMNIBUTLER_STATE_DIR``, default ``~/.omnibutler``) so pending items
survive restarts and are shared between processes (MCP server, CLI, a
future daemon). Writes are atomic (temp file + rename). Every public
operation re-reads the file first, so one process sees what another
parked or resolved.
"""

from __future__ import annotations

import itertools
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def default_state_dir() -> Path:
    override = os.environ.get("OMNIBUTLER_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".omnibutler"


@dataclass
class PendingConfirmation:
    id: str
    device_id: str
    kind: str  # "set_property" | "call_action"
    name: str  # property or action name
    value: Any = None
    params: dict[str, Any] = field(default_factory=dict)
    requested_by: str = ""
    scene: str | None = None
    risk: str = "high"
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | confirmed | rejected

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device": self.device_id,
            "kind": self.kind,
            "name": self.name,
            "value": self.value,
            "params": dict(self.params),
            "requested_by": self.requested_by,
            "scene": self.scene,
            "risk": self.risk,
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingConfirmation:
        return cls(
            id=str(data["id"]),
            device_id=str(data.get("device", data.get("device_id", ""))),
            kind=str(data["kind"]),
            name=str(data["name"]),
            value=data.get("value"),
            params=data.get("params") or {},
            requested_by=data.get("requested_by", ""),
            scene=data.get("scene"),
            risk=data.get("risk", "high"),
            created_at=float(data.get("created_at", time.time())),
            status=data.get("status", "pending"),
        )


class ConfirmationQueue:
    def __init__(
        self,
        state_dir: str | Path | None = None,
        path: str | Path | None = None,
    ) -> None:
        if path is not None:
            self.path = Path(path)
        elif state_dir is not None:
            self.path = Path(state_dir) / "confirmations.json"
        else:
            self.path = default_state_dir() / "confirmations.json"
        self._items: dict[str, PendingConfirmation] = {}
        self._counter = itertools.count(1)
        self._refresh()

    # -- persistence --------------------------------------------------------
    def _refresh(self) -> None:
        """Reload from disk so other processes' changes become visible."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        entries = raw.get("items", []) if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return
        items: dict[str, PendingConfirmation] = {}
        highest = 0
        for entry in entries:
            try:
                item = PendingConfirmation.from_dict(entry)
            except (KeyError, TypeError, ValueError):
                continue
            items[item.id] = item
            suffix = item.id.rsplit("-", 1)[-1]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        self._items = items
        self._counter = itertools.count(highest + 1)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "items": [item.to_dict() for item in self._items.values()],
        }
        tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- in-memory interface (backed by the file) -----------------------------
    def add(
        self,
        device_id: str,
        kind: str,
        name: str,
        value: Any = None,
        params: dict[str, Any] | None = None,
        requested_by: str = "",
        scene: str | None = None,
        risk: str = "high",
    ) -> PendingConfirmation:
        self._refresh()
        item = PendingConfirmation(
            id=f"cfm-{next(self._counter):04d}",
            device_id=device_id,
            kind=kind,
            name=name,
            value=value,
            params=params or {},
            requested_by=requested_by,
            scene=scene,
            risk=risk,
        )
        self._items[item.id] = item
        self._save()
        return item

    def pending(self) -> list[PendingConfirmation]:
        self._refresh()
        return [i for i in self._items.values() if i.status == "pending"]

    def all(self) -> list[PendingConfirmation]:
        self._refresh()
        return list(self._items.values())

    def get(self, confirmation_id: str) -> PendingConfirmation | None:
        self._refresh()
        return self._items.get(confirmation_id)

    def mark(self, confirmation_id: str, status: str) -> PendingConfirmation | None:
        self._refresh()
        item = self._items.get(confirmation_id)
        if item is not None:
            item.status = status
            self._save()
        return item

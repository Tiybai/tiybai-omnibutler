"""Confirmation queue for high-risk actions.

High-risk actions (garage doors, locks, gas valves) are never executed
directly by scenes or agents. They are parked here until a human confirms
them, which is the bridge's main safety guardrail.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any


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


class ConfirmationQueue:
    def __init__(self) -> None:
        self._items: dict[str, PendingConfirmation] = {}
        self._counter = itertools.count(1)

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
        return item

    def pending(self) -> list[PendingConfirmation]:
        return [i for i in self._items.values() if i.status == "pending"]

    def all(self) -> list[PendingConfirmation]:
        return list(self._items.values())

    def get(self, confirmation_id: str) -> PendingConfirmation | None:
        return self._items.get(confirmation_id)

    def mark(self, confirmation_id: str, status: str) -> PendingConfirmation | None:
        item = self._items.get(confirmation_id)
        if item is not None:
            item.status = status
        return item

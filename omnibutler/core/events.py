"""A tiny synchronous publish/subscribe event bus.

Events are how the outside world talks to the scene engine: a device changed
state, a schedule tick fired, a phone entered a geofence. Drivers and the
device manager publish; the scene engine and any observers subscribe.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    timestamp: float = field(default_factory=time.time)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


EventHandler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}
        self._wildcard: list[EventHandler] = []

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        if event_type == "*":
            self._wildcard.append(handler)
        else:
            self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        handlers = self._wildcard if event_type == "*" else self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    def publish(self, event: Event) -> Event:
        for handler in list(self._handlers.get(event.type, [])):
            handler(event)
        for handler in list(self._wildcard):
            handler(event)
        return event

    def emit(self, event_type: str, source: str = "", **data: Any) -> Event:
        return self.publish(Event(type=event_type, source=source, data=data))

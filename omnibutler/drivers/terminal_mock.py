"""Terminal mock driver: one virtual pair of smart glasses.

Smart glasses are not a switch. They are a terminal the AI talks through:
the bridge can put text in front of the wearer's eyes and speak into
their ear, and it can read back terminal facts like the battery level.
This driver models exactly that - honestly, with no fake ``onoff``:

- actions: ``display_text(text)`` and ``speak(text)`` - both only append
  to an in-memory ``outbox`` so tests and demos can assert what the AI
  "said" through the glasses;
- read-only state: ``battery`` and ``microphone``;
- no writable properties at all: ``set_property`` always refuses.
"""

from __future__ import annotations

import time
from typing import Any

from omnibutler.core.errors import DeviceNotFoundError, PropertyValidationError
from omnibutler.core.models import Capability as Cap
from omnibutler.core.models import Device, Property
from omnibutler.drivers.base import Driver

DEVICE_ID = "glasses"


def _device() -> Device:
    return Device(
        id=DEVICE_ID,
        name="Smart Glasses (Demo)",
        driver="terminal_mock",
        room="wearable",
        brand="Demo",
        model="Virtual Glasses G1",
        properties={
            "battery": Property(Cap.BATTERY),
            "microphone": Property(Cap.MICROPHONE),
        },
        state={"battery": 82, "microphone": True},
        actions=["display_text", "speak"],
    )


class TerminalMockDriver(Driver):
    name = "terminal_mock"

    def __init__(self) -> None:
        self._device_obj = _device()
        #: Everything the AI has output through this terminal, in order:
        #: {"action", "text", "device_id", "at"} entries.
        self.outbox: list[dict[str, Any]] = []

    def _device(self, device_id: str) -> Device:
        if device_id != self._device_obj.id:
            raise DeviceNotFoundError(
                f"terminal_mock driver has no device {device_id!r}"
            )
        return self._device_obj

    def discover(self) -> list[Device]:
        return [self._device_obj]

    def list_devices(self) -> list[Device]:
        return [self._device_obj]

    def get_state(self, device_id: str) -> dict[str, Any]:
        return dict(self._device(device_id).state)

    def set_property(self, device_id: str, property_name: str, value: Any) -> dict[str, Any]:
        self._device(device_id)
        raise PropertyValidationError(
            f"{device_id!r} is a terminal, not a switch: it has no writable "
            f"properties ({property_name!r} included); use its actions "
            f"(display_text / speak) to output through it"
        )

    def call_action(
        self, device_id: str, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        device = self._device(device_id)
        if action not in device.actions:
            raise PropertyValidationError(
                f"device {device_id!r} does not support action {action!r}; "
                f"supported: {device.actions}"
            )
        text = (params or {}).get("text")
        if not isinstance(text, str) or not text.strip():
            raise PropertyValidationError(
                f"action {action!r} needs a non-empty 'text' parameter"
            )
        entry = {
            "action": action,
            "text": text,
            "device_id": device_id,
            "at": time.time(),
        }
        self.outbox.append(entry)
        return {"action": action, "text": text, "delivered": True}

    def clear_outbox(self) -> None:
        self.outbox.clear()

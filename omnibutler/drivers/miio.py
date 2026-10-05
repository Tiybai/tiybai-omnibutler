"""Xiaomi miIO / MIoT driver - PLANNED, not implemented in this release.

Integration points (to be filled in behind this exact interface):

1. Key acquisition: the user obtains their own device tokens from their own
   Xiaomi account with the companion tooling in ``tools/`` (planned). Tokens
   are secrets: they live in the user's local secret store, never in code,
   never in this repository.
2. Discovery: miIO devices answer a UDP hello on port 54321 and via mDNS.
   Implement ``discover()`` to collect (ip, device id, model) tuples.
3. Capability modelling: fetch the model's MIoT-Spec document and translate
   services/properties into ``omnibutler.core.models.Property`` entries -
   see device-data/ for the data format and docs/specs/ for the clean-room
   specification workflow that MUST precede any implementation here.
4. Transport: encrypted UDP to the device using the per-device token.

Per the project's clean-room rules (docs/clean-room.md), this driver will be
implemented from protocol specifications and our own packet captures, not by
porting any existing library. Until then, every operation raises
PlannedDriverError so nothing can mistake it for a working driver.
"""

from __future__ import annotations

from typing import Any

from omnibutler.core.errors import PlannedDriverError
from omnibutler.core.models import Device
from omnibutler.drivers.base import Driver

_MESSAGE = (
    "The Xiaomi miIO/MIoT driver is planned but not implemented in v0.1. "
    "For Xiaomi devices today, use the Home Assistant driver with the "
    "official Xiaomi Home integration. See drivers/miio.py for the "
    "integration points and docs/clean-room.md for how it will be built."
)


class MiioDriver(Driver):
    name = "miio"
    planned = True

    def discover(self) -> list[Device]:
        return []

    def list_devices(self) -> list[Device]:
        return []

    def get_state(self, device_id: str) -> dict[str, Any]:
        raise PlannedDriverError(_MESSAGE)

    def set_property(self, device_id: str, property_name: str, value: Any) -> dict[str, Any]:
        raise PlannedDriverError(_MESSAGE)

    def call_action(
        self, device_id: str, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        raise PlannedDriverError(_MESSAGE)

"""Tuya local-protocol driver - PLANNED, not implemented in this release.

Integration points (to be filled in behind this exact interface):

1. Key acquisition: each device has a per-device ``local_key`` which the user
   obtains from their own Tuya account (IoT platform or Smart Life pairing
   flow). Keys are secrets and stay in the user's local secret store.
2. Discovery: Tuya devices broadcast on UDP 6666/6667/7000; the broadcast
   payload yields (ip, device id, protocol version).
3. Capability modelling: data points (DPs) are numeric and carry no semantics
   on the wire; translate them with the per-model mappings in device-data/
   (format: docs in device-data/README.md).
4. Transport: AES-encrypted TCP on port 6668, protocol versions 3.1-3.5.

Per the project's clean-room rules (docs/clean-room.md), the protocol
implementation will be written from specifications and our own captures, not
ported from existing libraries. Until then, every operation raises
PlannedDriverError so nothing can mistake it for a working driver.
"""

from __future__ import annotations

from typing import Any

from omnibutler.core.errors import PlannedDriverError
from omnibutler.core.models import Device
from omnibutler.drivers.base import Driver

_MESSAGE = (
    "The Tuya local driver is planned but not implemented in v0.1. "
    "For Tuya devices today, use the Home Assistant driver with a Tuya "
    "integration. See drivers/tuya.py for the integration points and "
    "docs/clean-room.md for how it will be built."
)


class TuyaDriver(Driver):
    name = "tuya"
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

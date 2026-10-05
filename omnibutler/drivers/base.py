"""Driver abstract base class.

A driver adapts one access channel (a vendor protocol, a hub, or a test
double) to the canonical capability model. Drivers never talk to agents or
scenes directly; the DeviceManager routes calls to them.
"""

from __future__ import annotations

import abc
from typing import Any

from omnibutler.core.models import Device


class Driver(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def discover(self) -> list[Device]:
        """Find devices reachable through this channel right now."""

    @abc.abstractmethod
    def list_devices(self) -> list[Device]:
        """Return the devices this driver currently manages."""

    @abc.abstractmethod
    def get_state(self, device_id: str) -> dict[str, Any]:
        """Return the current property values of one device."""

    @abc.abstractmethod
    def set_property(self, device_id: str, property_name: str, value: Any) -> dict[str, Any]:
        """Set one validated property; return the resulting values."""

    @abc.abstractmethod
    def call_action(
        self, device_id: str, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Run a named action beyond plain property writes."""

"""In-memory index of all known devices."""

from __future__ import annotations

from typing import Iterable

from .errors import DeviceNotFoundError
from .models import Capability, Device


class DeviceRegistry:
    def __init__(self) -> None:
        self._devices: dict[str, Device] = {}

    def register(self, device: Device) -> Device:
        self._devices[device.id] = device
        return device

    def register_many(self, devices: Iterable[Device]) -> None:
        for device in devices:
            self.register(device)

    def unregister(self, device_id: str) -> None:
        self._devices.pop(device_id, None)

    def clear(self) -> None:
        self._devices.clear()

    def get(self, device_id: str) -> Device:
        try:
            return self._devices[device_id]
        except KeyError:
            raise DeviceNotFoundError(
                f"unknown device {device_id!r}; known: {sorted(self._devices)}"
            ) from None

    def find(self, device_id: str) -> Device | None:
        return self._devices.get(device_id)

    def all(self) -> list[Device]:
        return list(self._devices.values())

    def by_room(self, room: str) -> list[Device]:
        return [d for d in self._devices.values() if d.room == room]

    def rooms(self) -> list[str]:
        return sorted({d.room for d in self._devices.values()})

    def by_capability(self, capability: str | Capability) -> list[Device]:
        return [d for d in self._devices.values() if d.has_capability(capability)]

    def by_driver(self, driver: str) -> list[Device]:
        return [d for d in self._devices.values() if d.driver == driver]

    def __len__(self) -> int:
        return len(self._devices)

    def __contains__(self, device_id: str) -> bool:
        return device_id in self._devices

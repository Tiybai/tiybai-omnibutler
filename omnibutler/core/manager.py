"""DeviceManager: the single routing point for all device operations.

Drivers own the hardware protocols; everything above them (CLI, MCP server,
scene engine) talks to this manager. It validates values against the
capability model, routes calls to the owning driver, writes the audit log,
and publishes state-change events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .audit import AuditLog
from .errors import PropertyValidationError
from .events import EventBus
from .models import Device
from .registry import DeviceRegistry

if TYPE_CHECKING:  # avoid a circular import at runtime
    from omnibutler.drivers.base import Driver


class DeviceManager:
    def __init__(
        self,
        drivers: dict[str, "Driver"] | None = None,
        audit: AuditLog | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.drivers: dict[str, Driver] = dict(drivers or {})
        self.registry = DeviceRegistry()
        self.audit = audit if audit is not None else AuditLog(enabled=False)
        self.bus = bus if bus is not None else EventBus()
        for driver in self.drivers.values():
            self.registry.register_many(driver.list_devices())

    def add_driver(self, driver: "Driver") -> None:
        self.drivers[driver.name] = driver
        self.registry.register_many(driver.list_devices())

    def refresh(self) -> None:
        self.registry.clear()
        for driver in self.drivers.values():
            self.registry.register_many(driver.list_devices())

    # -- reads ---------------------------------------------------------
    def list_devices(self, room: str | None = None) -> list[Device]:
        devices = self.registry.all()
        if room:
            devices = [d for d in devices if d.room == room]
        return devices

    def get_device(self, device_id: str) -> Device:
        return self.registry.get(device_id)

    def get_state(self, device_id: str) -> dict[str, Any]:
        device = self.registry.get(device_id)
        driver = self.drivers[device.driver]
        state = driver.get_state(device_id)
        device.state.update(state)
        return dict(device.state)

    # -- writes --------------------------------------------------------
    def set_property(
        self,
        device_id: str,
        property_name: str,
        value: Any,
        agent: str = "local",
    ) -> dict[str, Any]:
        device = self.registry.get(device_id)
        prop = device.property(property_name)
        if not prop.is_writable:
            raise PropertyValidationError(
                f"property {property_name!r} of device {device_id!r} is read-only"
            )
        canonical = prop.validate(value)
        driver = self.drivers[device.driver]
        try:
            result = driver.set_property(device_id, property_name, canonical)
        except Exception as exc:  # audit failures as well as successes
            self.audit.record(agent, device_id, f"set:{property_name}",
                              {"value": canonical}, ok=False, error=str(exc))
            raise
        device.state[property_name] = canonical
        self.audit.record(agent, device_id, f"set:{property_name}",
                          {"value": canonical}, result=result, ok=True)
        self.bus.emit("state_change", source="manager", device=device_id,
                      property=property_name, value=canonical, agent=agent)
        return result if isinstance(result, dict) else {"value": canonical}

    def call_action(
        self,
        device_id: str,
        action: str,
        params: dict[str, Any] | None = None,
        agent: str = "local",
    ) -> dict[str, Any]:
        device = self.registry.get(device_id)
        params = dict(params or {})
        driver = self.drivers[device.driver]
        try:
            result = driver.call_action(device_id, action, params)
        except Exception as exc:
            self.audit.record(agent, device_id, f"action:{action}",
                              params, ok=False, error=str(exc))
            raise
        if isinstance(result, dict):
            for key, val in result.items():
                if key in device.properties:
                    device.state[key] = val
        self.audit.record(agent, device_id, f"action:{action}", params,
                          result=result, ok=True)
        self.bus.emit("state_change", source="manager", device=device_id,
                      property=action, value=result, agent=agent, action=action)
        return result if isinstance(result, dict) else {"result": result}

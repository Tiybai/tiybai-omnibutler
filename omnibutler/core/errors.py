"""Shared exception types."""


class OmniButlerError(Exception):
    """Base error for all OmniButler failures."""


class DeviceNotFoundError(OmniButlerError):
    """Raised when a device id is not present in the registry."""


class PropertyValidationError(OmniButlerError):
    """Raised when a property value fails validation against its spec."""


class DriverNotConfiguredError(OmniButlerError):
    """Raised when a driver is used before its configuration is present."""


class PlannedDriverError(OmniButlerError):
    """Raised by drivers that are documented integration points, not yet usable."""

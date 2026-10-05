"""Onboarding scan: fault tolerance, needs classification, draft, report.

The drivers here are fakes shaped exactly like the real ones' discovery
results: a bare miIO sighting (no properties, no actions), a configured
miIO device (family properties + actions), a bare Tuya sighting, a
Broadlink hub, an HA entity, and one driver that only raises.
"""

from __future__ import annotations

import json

import pytest

from omnibutler.core.errors import PlannedDriverError
from omnibutler.core.models import Capability, Device, Property
from omnibutler.drivers.base import Driver
from omnibutler.drivers.mock import MockDriver
from omnibutler.onboard import (
    FoundDevice,
    ScanResult,
    config_draft,
    format_report,
    scan,
)


class FakeDriver(Driver):
    def __init__(self, name, devices=(), error=None):
        self.name = name
        self._devices = list(devices)
        self._error = error

    def discover(self):
        if self._error is not None:
            raise self._error
        return list(self._devices)

    def list_devices(self):
        return list(self._devices)

    def get_state(self, device_id):
        return {}

    def set_property(self, device_id, property_name, value):
        return {}

    def call_action(self, device_id, action, params):
        return {}


def bare(driver, device_id, name, **kwargs):
    """A sighting with no capability model, like an unconfigured miIO box."""
    return Device(id=device_id, name=name, driver=driver,
                  properties={}, actions=[], **kwargs)


def modeled(driver, device_id, name, **kwargs):
    """A configured device: capability model present."""
    return Device(id=device_id, name=name, driver=driver,
                  properties={"onoff": Property(Capability.ONOFF)},
                  actions=["turn_on", "turn_off"], **kwargs)


def mixed_drivers():
    return {
        "miio": FakeDriver("miio", [
            bare("miio", "miio-123456", "Xiaomi device 123456", brand="Xiaomi"),
            modeled("miio", "living_ac", "Living room AC",
                    room="living", brand="Xiaomi", model="zhimi.aircondition.v1"),
        ]),
        "tuya": FakeDriver("tuya", [
            bare("tuya", "tuya-bf00112233", " mystery plug ", room="kitchen"),
        ]),
        "midea": FakeDriver("midea", [
            bare("midea", "midea-7788", "Bedroom AC", room="bedroom"),
        ]),
        "broadlink": FakeDriver("broadlink", [
            Device(id="broadlink-aabbccddeeff", name="Broadlink RM4",
                   driver="broadlink", brand="Broadlink", model="RM4",
                   properties={}, actions=["learn_code", "send_code"]),
        ]),
        "homeassistant": FakeDriver("homeassistant", [
            modeled("homeassistant", "light.hall", "Hall light", room="hall"),
        ]),
        "tinytuya-broken": FakeDriver(
            "tinytuya-broken",
            error=PlannedDriverError("tinytuya is not installed (fake)"),
        ),
    }


def _by_id(found):
    return {item.id: item for item in found}


# -- scan: fault tolerance -------------------------------------------------

def test_scan_survives_a_broken_driver_and_records_a_note():
    result = scan(mixed_drivers())
    assert isinstance(result, ScanResult)
    # Every healthy driver's devices came back despite the broken one.
    assert set(_by_id(result)) == {
        "miio-123456", "living_ac", "tuya-bf00112233", "midea-7788",
        "broadlink-aabbccddeeff", "light.hall",
    }
    assert len(result.notes) == 1
    note = result.notes[0]
    assert "tinytuya-broken" in note
    assert "tinytuya is not installed" in note
    assert "没有数进来" in note  # plain language: not counted, not "none exist"


def test_scan_with_no_drivers_is_empty_not_an_error():
    result = scan({})
    assert result == []
    assert result.notes == []


def test_scan_with_real_mock_driver():
    result = scan({"mock": MockDriver()})
    assert len(result) >= 5
    assert all(isinstance(item, FoundDevice) for item in result)
    assert all(item.needs is None for item in result)
    assert result.notes == []


# -- needs classification ---------------------------------------------------

def test_needs_classification_from_driver_and_shape():
    items = _by_id(scan(mixed_drivers()))
    assert items["miio-123456"].needs == "token"          # bare miio sighting
    assert items["living_ac"].needs is None               # configured miio
    assert items["tuya-bf00112233"].needs == "local_key"  # bare tuya sighting
    assert items["midea-7788"].needs == "token+key"       # bare midea sighting
    assert items["broadlink-aabbccddeeff"].needs is None  # keyless pairing
    assert items["light.hall"].needs is None              # HA: already authed


def test_modeled_tuya_and_midea_devices_need_nothing():
    # The real Tuya/Midea drivers only build devices from configs that
    # already carry their keys, so a modeled device is controllable.
    drivers = {
        "tuya": FakeDriver("tuya", [modeled("tuya", "plug1", "Plug")]),
        "midea": FakeDriver("midea", [modeled("midea", "ac1", "AC")]),
    }
    items = _by_id(scan(drivers))
    assert items["plug1"].needs is None
    assert items["ac1"].needs is None


def test_unknown_driver_is_reported_unknown_not_invented():
    drivers = {"some_future_driver": FakeDriver(
        "some_future_driver", [modeled("some_future_driver", "fd1", "Future gadget")])}
    (item,) = scan(drivers)
    assert item.needs == "unknown"


# -- config draft -------------------------------------------------------------

def test_config_draft_for_key_needing_devices():
    draft = config_draft(scan(mixed_drivers()))
    json.dumps(draft)  # must stay plain JSON data

    (miio_entry,) = draft["miio"]["devices"]
    assert miio_entry["id"] == "miio-123456"
    assert miio_entry["host"] == ""  # not invented - the operator fills it
    assert miio_entry["token"] == "env:MIIO_123456_TOKEN"
    assert "tob setup miio" in miio_entry["_note"]
    assert "MIIO_123456_TOKEN" in miio_entry["_note"]

    (tuya_entry,) = draft["tuya"]["devices"]
    assert tuya_entry["local_key"] == "env:TUYA_BF00112233_LOCAL_KEY"
    assert tuya_entry["device_id"] == ""
    assert "tob setup tuya" in tuya_entry["_note"]

    (midea_entry,) = draft["midea"]["devices"]
    assert midea_entry["token"] == "env:MIDEA_7788_TOKEN"
    assert midea_entry["key"] == "env:MIDEA_7788_KEY"
    assert "_note" in midea_entry

    # No real-looking secret value anywhere in the draft.
    assert "bf00112233".upper() not in json.dumps(draft).replace(
        "TUYA_BF00112233_LOCAL_KEY", "")


def test_config_draft_includes_broadlink_for_persistence_only():
    draft = config_draft(scan(mixed_drivers()))
    (entry,) = draft["broadlink"]["devices"]
    assert entry["mac"] == "aa:bb:cc:dd:ee:ff"  # normalised from the id
    assert entry["host"] == ""
    assert "不用钥匙" in entry["_note"]


def test_config_draft_leaves_out_ready_and_unknown_devices():
    drivers = {
        "miio": FakeDriver("miio", [modeled("miio", "ac", "AC")]),
        "homeassistant": FakeDriver(
            "homeassistant", [modeled("homeassistant", "l1", "Light")]),
        "zigbee2mqtt": FakeDriver(
            "zigbee2mqtt", [modeled("zigbee2mqtt", "zb1", "Bulb")]),
    }
    draft = config_draft(scan(drivers))
    assert set(draft) == {"_about"}  # nothing to onboard


def test_config_draft_empty_scan():
    draft = config_draft([])
    assert draft == {"_about": draft["_about"]}
    assert "devices" not in json.dumps(draft)


# -- report -------------------------------------------------------------------

def test_format_report_tells_the_whole_story():
    result = scan(mixed_drivers())
    report = format_report(result)  # notes picked up from the ScanResult
    assert "发现 6 台" in report
    assert "可以直接控制的（3 台）" in report
    assert "还差钥匙的（3 台）" in report
    assert "Xiaomi device 123456" in report
    assert "token" in report and "local_key" in report
    assert "tob setup miio" in report and "tob setup tuya" in report
    assert "美的的 token 和 key" in report
    # the broken driver shows up as a scan note, not as silence
    assert "tinytuya-broken" in report
    assert "下一步" in report and "tob doctor" in report
    assert "config.json" in report


def test_format_report_accepts_explicit_notes():
    report = format_report([], notes=["miio：这一路这次没扫成（假的）。"])
    assert "一台设备都没发现" in report
    assert "miio：这一路这次没扫成" in report


def test_format_report_all_ready_needs_no_keys():
    report = format_report(scan({"mock": MockDriver()}))
    assert "可以直接控制" in report
    assert "不用补任何钥匙" in report
    assert "还差钥匙" not in report

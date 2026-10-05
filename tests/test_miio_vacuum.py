"""Tests for the miio driver's vacuum family (MIoT robot vacuums).

The local fake subclasses the miio tests' wire-format oracle: the
base fake only speaks get/set_properties, so this one peeks at the
decrypted request (with the oracle's own crypto helpers) and answers
the MIoT "action" method itself, recording every request so the
tests can assert exactly what the driver put on the wire. Status
codes in the fake are one model's flavour (Roborock-flavoured:
5 sweeping, 8 charging); the driver's contract is to pass status
codes through untouched, never reinterpret them.

The cloud tests at the bottom prove the family is inherited by the
xiaomi_cloud driver from the shared miio tables, not re-implemented.
"""

import hashlib
import json

import pytest
import test_xiaomi_cloud_driver as _cloud_tests
from test_miio_driver import TOKEN, FakeMiioDevice, _crypt, _driver_for, _unpad
from test_xiaomi_cloud_driver import FakeHttp as _CloudFakeHttp
from test_xiaomi_cloud_driver import _form

from omnibutler.cloud_keys import HttpResponse
from omnibutler.core.errors import OmniButlerError, PropertyValidationError
from omnibutler.drivers.miio import _kind_from_model
from omnibutler.drivers.xiaomi_cloud import XiaomiCloudDriver

DID = "13572468"


class FakeVacuum(FakeMiioDevice):
    """Fake robot vacuum: the base fake plus the MIoT action method."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []  # (method, params) of every decrypted request
        self.action_code = 0  # per-item result code for action calls

    def _handle(self, data):
        if len(data) > 32:
            header, check, ciphertext = data[:16], data[16:32], data[32:]
            if hashlib.md5(header + self.token + ciphertext).digest() == check:
                request = json.loads(
                    _unpad(_crypt(self.token, ciphertext, False)))
                self.requests.append(
                    (request.get("method"), request.get("params")))
                if request.get("method") == "action":
                    return self._handle_action(request)
        return super()._handle(data)

    def _handle_action(self, request):
        result = []
        for p in request["params"]:
            if self.action_code == 0:
                if (p["siid"], p["aiid"]) == (2, 1):    # start-sweep
                    self.store[(2, 1)] = 5
                elif (p["siid"], p["aiid"]) == (2, 2):  # stop-sweeping
                    self.store[(2, 1)] = 3
                elif (p["siid"], p["aiid"]) == (3, 1):  # start-charge
                    self.store[(2, 1)] = 8
            result.append({
                "did": p["did"], "siid": p["siid"], "aiid": p["aiid"],
                "code": self.action_code, "out": [],
            })
        return self._reply(request["id"], result=result)

    def calls(self, method):
        return [params for m, params in self.requests if m == method]

    def action_calls(self):
        return self.calls("action")


@pytest.fixture()
def fake_vacuum():
    device = FakeVacuum(
        TOKEN,
        device_id=13572468,
        store={(2, 1): 8, (3, 1): 87},  # charging, 87% battery
        model="roborock.vacuum.test",
    )
    device.start()
    yield device
    device.stop()


def _vacuum_driver(fake, model="roborock.vacuum.test", **kwargs):
    return _driver_for(fake, "vacuum", model, **kwargs)


# -- model classification ---------------------------------------------------

@pytest.mark.parametrize("model,family", [
    ("roborock.vacuum.a27", "vacuum"),
    ("roborock.vacuum.s5", "vacuum"),
    ("dreame.vacuum.p2008", "vacuum"),
    ("dreame.vacuum.r2211o", "vacuum"),
    ("viomi.vacuum.v7", "vacuum"),
    ("ijai.vacuum.v17", "vacuum"),
    ("xiaomi.vacuum.test", "vacuum"),
    # Regression: the five existing families keep their models -
    # vacuum is classified last, after all of them.
    ("zhimi.aircondition.v1", "air_conditioner"),
    ("zhimi.airpurifier.ma4", "air_purifier"),
    ("yeelink.light.lamp1", "light"),
    ("dmaker.fan.p5", "fan"),
    ("zhimi.humidifier.cb1", "humidifier"),
    ("unknown.gadget.v9", ""),
])
def test_kind_from_model_vacuum(model, family):
    assert _kind_from_model(model) == family


# -- device shape and state ---------------------------------------------------

def test_vacuum_device_shape(fake_vacuum):
    driver = _vacuum_driver(fake_vacuum)
    device = driver.list_devices()[0]
    assert set(device.properties) == {"onoff", "battery"}
    assert device.properties["onoff"].is_writable is True
    assert device.properties["battery"].is_writable is False
    assert device.actions == [
        "turn_on", "turn_off",
        "start_sweep", "stop_sweeping", "start_charge",
    ]


def test_vacuum_state_reports_status_code_and_battery(fake_vacuum):
    driver = _vacuum_driver(fake_vacuum)
    assert driver.get_state("vacuum") == {"status": 8, "battery": 87}
    # Only the two real properties were read - the action entries
    # (whose piid slot is an aiid) never go out as property reads.
    reads = fake_vacuum.calls("get_properties")
    assert len(reads) == 1
    assert [(p["siid"], p["piid"]) for p in reads[0]] == [(2, 1), (3, 1)]


def test_vacuum_status_code_is_never_relabelled(fake_vacuum):
    driver = _vacuum_driver(fake_vacuum)
    # 29 means "Sweeping" only in Roborock's own value-list; the
    # driver reports codes raw whatever they are.
    fake_vacuum.store[(2, 1)] = 29
    assert driver.get_state("vacuum")["status"] == 29
    fake_vacuum.store[(2, 1)] = 200  # defined by no model's list
    assert driver.get_state("vacuum")["status"] == 200
    # Battery boundaries pass through untouched.
    fake_vacuum.store[(3, 1)] = 0
    assert driver.get_state("vacuum")["battery"] == 0
    fake_vacuum.store[(3, 1)] = 100
    assert driver.get_state("vacuum")["battery"] == 100


# -- actions on the wire --------------------------------------------------------

def test_turn_on_starts_sweep(fake_vacuum):
    driver = _vacuum_driver(fake_vacuum)
    assert driver.call_action("vacuum", "turn_on", {}) == {"onoff": True}
    assert fake_vacuum.action_calls() == [
        [{"did": DID, "siid": 2, "aiid": 1, "in": []}]
    ]
    assert driver.get_state("vacuum")["status"] == 5


def test_turn_off_stops_then_docks(fake_vacuum):
    driver = _vacuum_driver(fake_vacuum)
    driver.call_action("vacuum", "turn_on", {})
    fake_vacuum.requests.clear()
    assert driver.call_action("vacuum", "turn_off", {}) == {"onoff": False}
    # Stop first, then start-charge (Battery service, siid 3 aiid 1).
    assert fake_vacuum.action_calls() == [
        [{"did": DID, "siid": 2, "aiid": 2, "in": []}],
        [{"did": DID, "siid": 3, "aiid": 1, "in": []}],
    ]
    assert driver.get_state("vacuum")["status"] == 8


def test_named_vacuum_actions(fake_vacuum):
    driver = _vacuum_driver(fake_vacuum)
    for name in ("start_sweep", "stop_sweeping", "start_charge"):
        assert driver.call_action("vacuum", name, {}) == {
            "action": name, "out": []}
    sent = [(p[0]["siid"], p[0]["aiid"]) for p in fake_vacuum.action_calls()]
    assert sent == [(2, 1), (2, 2), (3, 1)]


def test_set_property_onoff_uses_actions(fake_vacuum):
    driver = _vacuum_driver(fake_vacuum)
    assert driver.set_property("vacuum", "onoff", True) == {"onoff": True}
    assert driver.set_property("vacuum", "onoff", False) == {"onoff": False}
    sent = [(p[0]["siid"], p[0]["aiid"]) for p in fake_vacuum.action_calls()]
    assert sent == [(2, 1), (2, 2), (3, 1)]
    # Power was never attempted as a property write.
    assert fake_vacuum.calls("set_properties") == []


def test_vacuum_property_guards(fake_vacuum):
    driver = _vacuum_driver(fake_vacuum)
    with pytest.raises(PropertyValidationError):
        driver.set_property("vacuum", "battery", 50)       # read-only
    with pytest.raises(PropertyValidationError):
        driver.set_property("vacuum", "status", 1)         # read-only
    with pytest.raises(PropertyValidationError):
        driver.set_property("vacuum", "start_sweep", True)  # an action
    with pytest.raises(PropertyValidationError):
        driver.call_action("vacuum", "self_destruct", {})


def test_vacuum_action_refusal_raises(fake_vacuum):
    driver = _vacuum_driver(fake_vacuum)
    fake_vacuum.action_code = -4003
    with pytest.raises(OmniButlerError):
        driver.call_action("vacuum", "start_sweep", {})


def test_pause_is_a_per_model_override_not_a_guess(fake_vacuum):
    # The standard Vacuum service has no pause action, so the generic
    # table does not invent one...
    driver = _vacuum_driver(fake_vacuum)
    with pytest.raises(PropertyValidationError):
        driver.call_action("vacuum", "pause_sweeping", {})
    # ...but a model that does grow one (some Viomi / Dreame units)
    # wires it through the per-device mapping, aiid and all.
    driver = _vacuum_driver(
        fake_vacuum, "viomi.vacuum.test",
        mapping={"pause_sweeping": {"siid": 2, "piid": 3, "kind": "action"}},
    )
    assert driver.call_action("vacuum", "pause_sweeping", {}) == {
        "action": "pause_sweeping", "out": []}
    assert fake_vacuum.action_calls()[-1] == [
        {"did": DID, "siid": 2, "aiid": 3, "in": []}]


# -- cloud fallback: inherited from the shared tables -------------------------

_VACUUM_DID = "800000006"
_CLOUD_VACUUM = {
    "did": _VACUUM_DID, "name": "扫地机", "model": "roborock.vacuum.a27",
    "uid": "42424242", "isOnline": True,
}
_CLOUD_ID = "xmc-800000006"


class _VacuumCloudHttp(_CloudFakeHttp):
    """The cloud fake plus a vacuum in the account and the MIoT
    action endpoint the base fake does not implement."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prop_values[_VACUUM_DID] = {(2, 1): 8, (3, 1): 87}
        self.action_params = []

    def __call__(self, method, url, *, headers=None, data=None, timeout=None):
        if "/app/miotspec/action" in url:
            params = json.loads(_form({"data": data or b""})["data"])["params"]
            self.action_params.append(params)
            return HttpResponse(200, json.dumps(
                {"code": 0, "result": {"code": 0, "out": []}}))
        return super().__call__(method, url, headers=headers,
                                data=data, timeout=timeout)


@pytest.fixture()
def cloud_vacuum(monkeypatch):
    monkeypatch.setattr(
        _cloud_tests, "DEVICE_LIST",
        [*_cloud_tests.DEVICE_LIST, _CLOUD_VACUUM])
    http = _VacuumCloudHttp()
    driver = XiaomiCloudDriver(
        username="mi-user@example.com", password="hunter2",
        config={}, http=http)
    return driver, http


def test_cloud_driver_inherits_vacuum_family(cloud_vacuum):
    driver, _http = cloud_vacuum
    devices = {d.id: d for d in driver.discover()}
    vacuum = devices[_CLOUD_ID]
    assert set(vacuum.properties) == {"onoff", "battery"}
    assert vacuum.actions == [
        "turn_on", "turn_off", "toggle",
        "start_sweep", "stop_sweeping", "start_charge",
    ]
    assert driver.get_state(_CLOUD_ID) == {"status": 8, "battery": 87}


def test_cloud_vacuum_actions_and_power(cloud_vacuum):
    driver, http = cloud_vacuum
    assert driver.call_action(_CLOUD_ID, "start_charge", {}) == {
        "action": "start_charge", "out": []}
    assert http.action_params == [
        [{"did": _VACUUM_DID, "siid": 3, "aiid": 1, "in": []}]]
    # turn_off routes through onoff, which is action-backed in the
    # cloud driver too: stop, then dock.
    http.action_params.clear()
    assert driver.call_action(_CLOUD_ID, "turn_off", {}) == {"onoff": False}
    assert [(p[0]["siid"], p[0]["aiid"]) for p in http.action_params] == [
        (2, 2), (3, 1)]

"""Xiaomi cloud fallback driver: control via the mi cloud (vendor cloud).

This is the *fallback* channel, not the main road. The local driver
(:mod:`omnibutler.drivers.miio`, miIO over the LAN) is preferred
whenever a device is reachable at home: it works with the internet
down and keeps traffic off Xiaomi's servers. This driver exists for
the cases where the local path cannot run - the device's miIO token
could not be fetched, the device sits on a network the bridge cannot
reach, or its local protocol is unusable - and the user would rather
have cloud control than no control.

Be honest about the trade: every call here goes to Xiaomi's cloud
(``api.io.mi.com`` for a China account). No internet, no control;
Xiaomi's servers see every command; and the account password below
can act as the account holder, so it is handled with the same
discipline as every other secret in this project - memory only,
never logged, never in an error message, never on a Device object.

Configuration (constructor arguments, environment, or the local
config file's ``xiaomi_cloud`` section, in that order)::

    XIAOMI_CLOUD_USERNAME  Xiaomi account name (email / phone / Mi ID)
    XIAOMI_CLOUD_PASSWORD  account password (secret)
    XIAOMI_CLOUD_COUNTRY   account region, default "cn" ("de", "us", ...)

    {"xiaomi_cloud": {"username": "...",
                      "password": "env:XIAOMI_CLOUD_PASSWORD",
                      "country": "cn"}}

``env:`` references in the config section resolve exactly like the
rest of the config file (see :mod:`omnibutler.config`). With neither
source providing credentials, every operation raises a plain-language
error saying how to configure them - nothing silently pretends.

Device ids are ``xmc-<did>``, deliberately distinct from any local
id, so a device reachable both ways can exist once per channel;
which channel an agent actually drives is a runtime/configuration
decision, not something this driver arbitrates.

Reuse, not re-implementation - two existing modules already solved
the hard parts, and this driver is deliberately a thin composition
of them:

* Login and request signing come from :mod:`omnibutler.cloud_keys`
  (the one-time key-fetch module): ``_xiaomi_login_fields``,
  ``_xiaomi_authenticate`` (which hashes the password in memory -
  only the hash goes on the wire), ``_xiaomi_service_token``,
  ``_xiaomi_device_list``, and the ``_xiaomi_nonce`` /
  ``_xiaomi_signed_nonce`` / ``_xiaomi_signature`` helpers plus the
  ``_send`` transport wrapper. Its error kinds are mirrored here so
  a captcha / two-step wall surfaces as
  ``needs_human_verification`` - this driver never tries to get
  around one either.
* The per-model MIoT property tables and the value conversions come
  from :mod:`omnibutler.drivers.miio` (``_FAMILY_MAPS``,
  ``_FAMILY_PROPERTIES``, ``_kind_from_model`` and the
  ``MiioDriver._to_canonical`` / ``._to_wire`` translators), so a
  device behaves exactly the same whichever channel drives it: the
  cloud MIoT-spec endpoint (``/app/miotspec/prop``) addresses the
  same siid/piid properties the local protocol does.

Honesty note - what is *not* verified: this driver has never run
against the real Xiaomi cloud (the development sandbox has no route
to it). Requests are built strictly to the public mi cloud shapes -
the login flow is the one cloud_keys documents, and the miotspec
calls sign a ``{"params": [...]}`` data payload exactly the way the
device_list call signs its own - but live details may differ,
notably the miotspec result item shape and which business codes a
dead session produces (the re-login retry keys off HTTP 401, the
documented signal). Any mismatch surfaces as a classified
:class:`XiaomiCloudError`, never as a silent wrong action.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from typing import Any, Callable, Mapping

# Reuse points in omnibutler.cloud_keys (login flow, signing,
# transport, error taxonomy) - imported private helpers on purpose:
# day-to-day control must speak byte-for-byte the same login and
# signing language as the one-time key fetch, and keeping two copies
# of a vendor login flow is how they drift apart.
from omnibutler.cloud_keys import (
    KIND_AUTH_FAILED as _CK_AUTH_FAILED,
)
from omnibutler.cloud_keys import (
    KIND_BAD_RESPONSE as _CK_BAD_RESPONSE,
)
from omnibutler.cloud_keys import (
    KIND_NEEDS_HUMAN as _CK_NEEDS_HUMAN,
)
from omnibutler.cloud_keys import (
    KIND_NETWORK as _CK_NETWORK,
)
from omnibutler.cloud_keys import (
    CloudKeyError,
    HttpResponse,
    _parse_json,
    _send,
    _xiaomi_authenticate,
    _xiaomi_device_list,
    _xiaomi_login_fields,
    _xiaomi_nonce,
    _xiaomi_service_token,
    _xiaomi_signature,
    _xiaomi_signed_nonce,
    _urllib_http,
)
from omnibutler.config import get_section, load_config, resolve_secret
from omnibutler.core.errors import (
    DeviceNotFoundError,
    DriverNotConfiguredError,
    OmniButlerError,
    PropertyValidationError,
)
from omnibutler.core.models import Device
from omnibutler.drivers.base import Driver
# Reuse points in omnibutler.drivers.miio: the per-model MIoT tables
# and value translators, shared with the local driver so both
# channels expose identical properties with identical conversions.
from omnibutler.drivers.miio import (
    _FAMILY_MAPS,
    _FAMILY_PROPERTIES,
    MiioDriver,
    _MiotRef,
    _kind_from_model,
)

logger = logging.getLogger(__name__)

KIND_AUTH_FAILED = _CK_AUTH_FAILED
KIND_NEEDS_HUMAN = _CK_NEEDS_HUMAN
KIND_NETWORK = _CK_NETWORK
KIND_BAD_RESPONSE = _CK_BAD_RESPONSE
_KINDS = (KIND_AUTH_FAILED, KIND_NEEDS_HUMAN, KIND_NETWORK, KIND_BAD_RESPONSE)

_ENV_USERNAME = "XIAOMI_CLOUD_USERNAME"
_ENV_PASSWORD = "XIAOMI_CLOUD_PASSWORD"
_ENV_COUNTRY = "XIAOMI_CLOUD_COUNTRY"
_DEFAULT_COUNTRY = "cn"

_NOT_CONFIGURED = (
    "The Xiaomi cloud driver has no account credentials. It logs in "
    "with your own Xiaomi account, so either set the environment "
    f"variables {_ENV_USERNAME} and {_ENV_PASSWORD} (plus "
    f"{_ENV_COUNTRY} if your account is not a China-region one), or "
    "add a \"xiaomi_cloud\" section with username / password / "
    "country to the local config file (~/.omnibutler/config.json; "
    "the password may be an env: reference). Run `tob setup "
    "xiaomi_cloud` for the walkthrough. Without credentials the "
    "cloud channel cannot run - the local miio driver does not need "
    "your password at all, only each device's token."
)


class XiaomiCloudError(OmniButlerError):
    """A Xiaomi cloud call failed, classified by ``kind``.

    Kinds mirror :class:`omnibutler.cloud_keys.CloudKeyError`:
    ``auth_failed`` (credentials or session rejected),
    ``needs_human_verification`` (captcha / two-step verification -
    a human must finish it in a browser; this code never tries to
    bypass it), ``network`` (unreachable or 5xx) and
    ``bad_response`` (an answer this client cannot make sense of, or
    a business-level non-zero code). Messages are built from fixed
    wording plus numeric codes only - the password and session
    tokens never appear in them.
    """

    def __init__(self, kind: str, message: str) -> None:
        if kind not in _KINDS:
            raise ValueError(f"unknown XiaomiCloudError kind {kind!r}")
        super().__init__(message)
        self.kind = kind


def _translate(exc: CloudKeyError) -> XiaomiCloudError:
    """Re-raise a cloud_keys failure in this driver's error type.

    The kind and the (secret-free) message carry over unchanged, so
    the login flow's careful wording - including the
    needs-human-verification guidance - reaches the user verbatim.
    """
    return XiaomiCloudError(exc.kind, str(exc))


class _XiaomiCloudClient:
    """Logged-in mi cloud session: login once, sign every call.

    The session material (userId / ssecurity / serviceToken) is held
    in memory only and never repr'd or logged.
    """

    def __init__(self, username: str, password: str, country: str,
                 http: Callable[..., HttpResponse]) -> None:
        self._username = username
        self._password = password
        self._country = country
        self._http = http
        self._user_id: Any = None
        self._ssecurity: str | None = None
        self._service_token: str | None = None

    def __repr__(self) -> str:
        # Credentials and session tokens are never part of any repr.
        return (f"_XiaomiCloudClient(country={self._country!r}, "
                f"logged_in={self._service_token is not None})")

    @property
    def _host(self) -> str:
        if self._country == "cn":
            return "api.io.mi.com"
        return f"{self._country}.api.io.mi.com"

    # -- session ------------------------------------------------------
    def _drop_session(self) -> None:
        self._user_id = None
        self._ssecurity = None
        self._service_token = None

    def _login(self) -> None:
        """The cloud_keys login flow: serviceLogin -> auth2 -> STS."""
        try:
            fields = _xiaomi_login_fields(self._http)
            auth = _xiaomi_authenticate(
                self._http, self._username, self._password, fields)
            token = _xiaomi_service_token(self._http, str(auth["location"]))
        except CloudKeyError as exc:
            raise _translate(exc) from exc
        self._user_id = auth.get("userId")
        self._ssecurity = str(auth["ssecurity"])
        self._service_token = token

    def _ensure_session(self) -> None:
        if self._service_token is None:
            self._login()

    # -- device list (cloud_keys' signed call, with one re-login) -------
    def device_list(self) -> list[dict[str, Any]]:
        """Account device list; a rejected session re-logs in once."""
        for attempt in (0, 1):
            self._ensure_session()
            try:
                return _xiaomi_device_list(
                    self._http,
                    country=self._country,
                    user_id=self._user_id,
                    ssecurity=str(self._ssecurity),
                    service_token=str(self._service_token),
                )
            except CloudKeyError as exc:
                if exc.kind == _CK_AUTH_FAILED and attempt == 0:
                    # Session material went stale: drop it, log in
                    # fresh and retry exactly once.
                    self._drop_session()
                    continue
                raise _translate(exc) from exc
        raise AssertionError("unreachable")  # pragma: no cover

    # -- signed miotspec calls -------------------------------------------
    def _signed_post(self, path: str, params: list[dict[str, Any]], *,
                     what: str) -> Any:
        """One signed POST to a miotspec endpoint; returns ``result``.

        The signature construction is identical to the device_list
        call's: ``params={"data": <json string>}`` is what gets
        signed, and the form carries signature / _nonce / ssecurity
        alongside it. An HTTP 401 (the documented dead-session
        signal) drops the session, re-logs in and retries once.
        """
        data_str = json.dumps({"params": params})
        for attempt in (0, 1):
            self._ensure_session()
            nonce = _xiaomi_nonce()
            try:
                signed_nonce = _xiaomi_signed_nonce(
                    str(self._ssecurity), nonce)
            except CloudKeyError as exc:
                raise _translate(exc) from exc
            signed_params = {"data": data_str}
            form = {
                **signed_params,
                "signature": _xiaomi_signature(
                    path, signed_nonce, nonce, signed_params),
                "_nonce": nonce,
                "ssecurity": str(self._ssecurity),
            }
            try:
                response = _send(
                    self._http, "POST", f"https://{self._host}{path}",
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
                        "Cookie": (f"userId={self._user_id}; "
                                   f"serviceToken={self._service_token}"),
                    },
                    data=urllib.parse.urlencode(form).encode("utf-8"),
                    what=what,
                )
            except CloudKeyError as exc:
                raise _translate(exc) from exc
            if response.status == 401:
                if attempt == 0:
                    self._drop_session()
                    continue
                raise XiaomiCloudError(
                    KIND_AUTH_FAILED,
                    f"{what}: Xiaomi rejected the session even after "
                    "a fresh login (HTTP 401). The account may need "
                    "attention in the Mi Home app.",
                )
            if response.status in (403,):
                raise XiaomiCloudError(
                    KIND_AUTH_FAILED,
                    f"{what}: Xiaomi rejected the request (HTTP 403). "
                    "Check the account credentials.",
                )
            if response.status >= 500:
                raise XiaomiCloudError(
                    KIND_NETWORK,
                    f"{what}: Xiaomi answered HTTP {response.status}. "
                    "This looks like a problem on Xiaomi's side; try "
                    "again later.",
                )
            if response.status != 200:
                raise XiaomiCloudError(
                    KIND_BAD_RESPONSE,
                    f"{what}: unexpected HTTP {response.status} from "
                    "Xiaomi.",
                )
            try:
                payload = _parse_json(response.text, what=what)
            except CloudKeyError as exc:
                raise _translate(exc) from exc
            if not isinstance(payload, dict):
                raise XiaomiCloudError(
                    KIND_BAD_RESPONSE,
                    f"{what}: Xiaomi's answer is not an object.",
                )
            code = payload.get("code")
            if code != 0:
                raise XiaomiCloudError(
                    KIND_BAD_RESPONSE,
                    f"{what}: Xiaomi answered with code {code}.",
                )
            return payload.get("result")
        raise AssertionError("unreachable")  # pragma: no cover

    def miot_get(self, did: str,
                 refs: list[tuple[int, int]]) -> list[dict[str, Any]]:
        """Read MIoT properties via /app/miotspec/prop."""
        params = [{"did": did, "siid": siid, "piid": piid}
                  for siid, piid in refs]
        result = self._signed_post(
            "/app/miotspec/prop", params,
            what=f"Xiaomi cloud device {did} property read")
        if not isinstance(result, list):
            raise XiaomiCloudError(
                KIND_BAD_RESPONSE,
                f"Xiaomi cloud device {did} property read: the "
                "server's answer has no result list.",
            )
        return [item for item in result if isinstance(item, dict)]

    def miot_set(self, did: str, siid: int, piid: int,
                 value: Any) -> dict[str, Any]:
        """Write one MIoT property via /app/miotspec/prop."""
        params = [{"did": did, "siid": siid, "piid": piid,
                   "value": value}]
        result = self._signed_post(
            "/app/miotspec/prop", params,
            what=f"Xiaomi cloud device {did} property write")
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return result[0]
        raise XiaomiCloudError(
            KIND_BAD_RESPONSE,
            f"Xiaomi cloud device {did} property write: the server's "
            "answer has no result item.",
        )

    def miot_action(self, did: str, siid: int, aiid: int,
                    inputs: list[Any]) -> dict[str, Any]:
        """Run one MIoT action via /app/miotspec/action."""
        params = [{"did": did, "siid": siid, "aiid": aiid,
                   "in": list(inputs)}]
        result = self._signed_post(
            "/app/miotspec/action", params,
            what=f"Xiaomi cloud device {did} action")
        if isinstance(result, dict):
            return result
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return result[0]
        raise XiaomiCloudError(
            KIND_BAD_RESPONSE,
            f"Xiaomi cloud device {did} action: the server's answer "
            "has no result item.",
        )


class XiaomiCloudDriver(Driver):
    name = "xiaomi_cloud"

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        *,
        country: str | None = None,
        http: Callable[..., HttpResponse] | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._country = country
        self._http = http or _urllib_http
        self._config = config
        self._client: _XiaomiCloudClient | None = None
        self._devices: dict[str, Device] = {}
        self._dids: dict[str, str] = {}
        self._mappings: dict[str, dict[str, _MiotRef]] = {}
        #: Devices the account listed but this driver has no miio
        #: mapping for (skipped by discover(), counted here).
        self.skipped_unmapped = 0

    def __repr__(self) -> str:
        # Deliberately excludes credentials and session material.
        return f"XiaomiCloudDriver(devices={sorted(self._devices)})"

    # -- configuration ----------------------------------------------------
    def _resolve_client(self) -> _XiaomiCloudClient:
        if self._client is not None:
            return self._client
        config = self._config if self._config is not None else load_config()
        section = get_section(config, "xiaomi_cloud")
        username = (
            self._username
            or os.environ.get(_ENV_USERNAME, "").strip()
            or resolve_secret(section.get("username"))
        )
        password = (
            self._password
            or os.environ.get(_ENV_PASSWORD, "").strip()
            or resolve_secret(section.get("password"))
        )
        if not username or not password:
            raise DriverNotConfiguredError(_NOT_CONFIGURED)
        country = (
            self._country
            or os.environ.get(_ENV_COUNTRY, "").strip()
            or (str(section["country"]) if section.get("country") else None)
            or _DEFAULT_COUNTRY
        )
        self._client = _XiaomiCloudClient(
            str(username), str(password), str(country).strip().lower(),
            self._http)
        return self._client

    # -- device modelling ---------------------------------------------------
    def _build_device(self, entry: Mapping[str, Any], family: str) -> Device:
        did = str(entry.get("did") or "")
        device = Device(
            id=f"xmc-{did}",
            name=str(entry.get("name") or f"Xiaomi {family} {did[-4:]}"),
            driver="xiaomi_cloud",
            room=str(entry.get("room") or "unknown"),
            brand="Xiaomi",
            model=str(entry.get("model") or ""),
            properties=dict(_FAMILY_PROPERTIES[family]),
            actions=["turn_on", "turn_off", "toggle"],
            online=bool(entry.get("isOnline", True)),
        )
        self._dids[device.id] = did
        self._mappings[device.id] = dict(_FAMILY_MAPS[family])
        return device

    def _lookup(self, device_id: str) -> Device:
        try:
            return self._devices[device_id]
        except KeyError:
            raise DeviceNotFoundError(
                f"Xiaomi cloud driver has no device {device_id!r}; "
                f"discovered: {sorted(self._devices)}"
            ) from None

    # -- state translation (miio's translators, shared verbatim) ------------
    def _read_state(self, client: _XiaomiCloudClient,
                    device: Device) -> dict[str, Any]:
        mapping = self._mappings[device.id]
        refs = [(ref.siid, ref.piid) for ref in mapping.values()]
        items = client.miot_get(self._dids[device.id], refs)
        by_address = {
            (item.get("siid"), item.get("piid")): item for item in items
        }
        state: dict[str, Any] = {}
        failures = 0
        for name, ref in mapping.items():
            item = by_address.get((ref.siid, ref.piid))
            if item is None or item.get("code") != 0:
                failures += 1
                continue
            state[name] = MiioDriver._to_canonical(ref, item.get("value"))
        if failures and not state:
            raise XiaomiCloudError(
                KIND_BAD_RESPONSE,
                f"Xiaomi cloud device {device.id!r} could not report "
                "any of its properties (all reads returned a "
                "non-zero code).",
            )
        return state

    # -- Driver API -----------------------------------------------------------
    def discover(self) -> list[Device]:
        client = self._resolve_client()
        devices: dict[str, Device] = {}
        skipped = 0
        for entry in client.device_list():
            if not isinstance(entry, dict):
                continue
            did = str(entry.get("did") or "")
            family = _kind_from_model(str(entry.get("model") or ""))
            if not did or family not in _FAMILY_MAPS:
                # No miio mapping for this model: nothing honest to
                # expose, so skip it (and count it) rather than guess.
                skipped += 1
                continue
            device = self._build_device(entry, family)
            try:
                device.state = self._read_state(client, device)
            except XiaomiCloudError as exc:
                # One sick device must not hide the rest of the home;
                # it is listed with empty state instead.
                logger.warning(
                    "xiaomi_cloud: state for device %s failed (%s: "
                    "%s); listing it without state", did, exc.kind, exc)
            devices[device.id] = device
        self.skipped_unmapped = skipped
        if skipped:
            logger.info(
                "xiaomi_cloud: skipped %d account device(s) with no "
                "miio mapping", skipped)
        self._devices = devices
        return list(devices.values())

    def list_devices(self) -> list[Device]:
        if not self._devices:
            return self.discover()
        return list(self._devices.values())

    def get_state(self, device_id: str) -> dict[str, Any]:
        client = self._resolve_client()
        if device_id not in self._devices:
            self.discover()
        device = self._lookup(device_id)
        device.state = self._read_state(client, device)
        return dict(device.state)

    def set_property(self, device_id: str, property_name: str,
                     value: Any) -> dict[str, Any]:
        client = self._resolve_client()
        if device_id not in self._devices:
            self.discover()
        device = self._lookup(device_id)
        mapping = self._mappings[device_id]
        ref = mapping.get(property_name)
        if ref is None:
            raise PropertyValidationError(
                f"device {device_id!r} has no miIO-mapped property "
                f"{property_name!r}; available: {sorted(mapping)}"
            )
        if not ref.writable:
            raise PropertyValidationError(
                f"property {property_name!r} of device {device_id!r} "
                "is read-only"
            )
        wire = MiioDriver._to_wire(ref, property_name, value)
        item = client.miot_set(
            self._dids[device_id], ref.siid, ref.piid, wire)
        if item.get("code") != 0:
            raise XiaomiCloudError(
                KIND_BAD_RESPONSE,
                f"Xiaomi cloud device {device_id!r} refused to set "
                f"{property_name!r} (result code {item.get('code')}).",
            )
        return {property_name: value}

    def call_action(self, device_id: str, action: str,
                    params: dict[str, Any]) -> dict[str, Any]:
        if action in {"turn_on", "turn_off"}:
            return self.set_property(device_id, "onoff", action == "turn_on")
        if action == "toggle":
            current = self.get_state(device_id).get("onoff", False)
            return self.set_property(device_id, "onoff", not current)
        # MIoT actions: if a model's mapping ever grows an entry with
        # kind "action" (its piid slot carries the aiid), it runs
        # through /app/miotspec/action. Today's miio tables define
        # properties only, so nothing routes here yet.
        client = self._resolve_client()
        if device_id not in self._devices:
            self.discover()
        self._lookup(device_id)
        ref = self._mappings[device_id].get(action)
        if ref is not None and ref.kind == "action":
            item = client.miot_action(
                self._dids[device_id], ref.siid, ref.piid,
                list(params.get("in") or []))
            if item.get("code") != 0:
                raise XiaomiCloudError(
                    KIND_BAD_RESPONSE,
                    f"Xiaomi cloud device {device_id!r} refused "
                    f"action {action!r} (result code "
                    f"{item.get('code')}).",
                )
            return {"action": action, "out": item.get("out")}
        raise OmniButlerError(
            f"Xiaomi cloud driver does not support action {action!r} "
            f"on device {device_id!r}; supported: turn_on, turn_off, "
            "toggle."
        )

"""Tuya cloud fallback driver: control via the Tuya Open API (vendor cloud).

This is the *fallback* channel, not the main road. The local driver
(:mod:`omnibutler.drivers.tuya`, tinytuya on the LAN) is preferred
whenever a device is reachable at home: it works with the internet
down and keeps traffic off the vendor's servers. This driver exists
for the cases where the local path cannot run - the device is on a
network the bridge cannot reach, or its local protocol is unusable -
and the user would rather have cloud control than no control.

Be honest about the trade: every call here goes to Tuya's cloud
(``openapi.tuyacn.com`` by default). No internet, no control; Tuya's
servers see every command; and the account credentials below are a
project-level key to the user's whole Tuya project, so they are
handled with the same discipline as every other secret in this
project - memory only, never logged, never in an error message,
never on a Device object.

Configuration (constructor arguments, environment, or the local
config file's ``tuya_cloud`` section, in that order)::

    TUYA_CLOUD_ACCESS_ID      IoT project Access ID (iot.tuya.com)
    TUYA_CLOUD_ACCESS_SECRET  IoT project Access Secret (secret)
    TUYA_CLOUD_UID            uid of the linked Tuya App account
                              (optional: the token answer carries one)
    TUYA_CLOUD_BASE_URL       data center (default China:
                              https://openapi.tuyacn.com)

    {"tuya_cloud": {"access_id": "...", "access_secret": "env:MY_SECRET",
                    "uid": "...", "base_url": "https://openapi.tuyacn.com"}}

``env:`` references in the config section resolve exactly like the
rest of the config file (see :mod:`omnibutler.config`). With neither
source providing credentials, every operation raises a plain-language
error saying how to configure them - nothing silently pretends.

Device ids are ``tuyac-<tuya device id>``, deliberately distinct from
the local driver's ``tuya-...``, so a device reachable both ways can
exist once per channel; which channel an agent actually drives is a
runtime/configuration decision, not something this driver arbitrates.

Signing follows Tuya's published Open API algorithm - the same one
:mod:`omnibutler.cloud_keys` implements for one-time key fetching
(stringToSign = method + sha256(body) + signed-headers + path, then
upper-hex HMAC-SHA256 over client_id [+ access_token] + t +
stringToSign). It is re-implemented here, rather than calling the
key-fetch module, because day-to-day control must not depend on a
one-time provisioning tool; both implementations trace back to the
same public documentation. DP code -> canonical capability mapping
and the value conversions mirror :mod:`omnibutler.drivers.tuya` (the
same vendor-published DP facts: bulb brightness raw 10-1000, colour
temperature raw 0-1000 across the bulb's kelvin range, outlet power in
0.1 W units, energy in 0.01 kWh units), so a device behaves the same
whichever channel drives it.

Honesty note - what is *not* verified: this driver has never run
against the real Tuya cloud (the development sandbox has no route to
it). Requests are built strictly to the published API shapes and the
signing is self-checked against test vectors via the fake-HTTP tests,
but live details may differ - notably the ``colour_data`` value form
(some products expect a JSON ``{"h","s","v"}`` string, others the
12-hex form) and the exact device-list payload per data center. Any
mismatch surfaces as a classified :class:`TuyaCloudError`, never as a
silent wrong action.
"""

from __future__ import annotations

import colorsys
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from omnibutler.cloud_keys import HttpResponse
from omnibutler.config import get_section, load_config, resolve_secret
from omnibutler.core.errors import (
    DeviceNotFoundError,
    DriverNotConfiguredError,
    OmniButlerError,
    PropertyValidationError,
)
from omnibutler.core.models import Device
from omnibutler.drivers.base import Driver
from omnibutler.drivers.tuya import KIND_PROPERTIES

logger = logging.getLogger(__name__)

KIND_AUTH_FAILED = "auth_failed"
KIND_NETWORK = "network"
KIND_BAD_RESPONSE = "bad_response"
_KINDS = (KIND_AUTH_FAILED, KIND_NETWORK, KIND_BAD_RESPONSE)

_DEFAULT_BASE_URL = "https://openapi.tuyacn.com"
_HTTP_TIMEOUT = 15.0
#: Cloud answers are small JSON documents; a body past this is a
#: broken (or hostile) endpoint, not data. Reads are bounded so a
#: peer cannot make the bridge buffer an unbounded response.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_TOKEN_SAFETY_MARGIN = 30.0  # refresh this many seconds before expiry
# Tuya business code for "token invalid / expired" (vendor-published).
_CODE_TOKEN_INVALID = 1010

_ENV_ACCESS_ID = "TUYA_CLOUD_ACCESS_ID"
_ENV_ACCESS_SECRET = "TUYA_CLOUD_ACCESS_SECRET"
_ENV_UID = "TUYA_CLOUD_UID"
_ENV_BASE_URL = "TUYA_CLOUD_BASE_URL"

_NOT_CONFIGURED = (
    "The Tuya cloud driver has no project credentials. Create a "
    "project at iot.tuya.com, link your Tuya App account to it, then "
    f"either set the environment variables {_ENV_ACCESS_ID} and "
    f"{_ENV_ACCESS_SECRET} (plus {_ENV_UID} for the linked account), "
    "or add a \"tuya_cloud\" section with access_id / access_secret / "
    "uid to the local config file (~/.omnibutler/config.json; the "
    "secret may be an env: reference). Without credentials the cloud "
    "channel cannot run - the local Tuya driver does not need them "
    "once each device's local_key is fetched."
)


class TuyaCloudError(OmniButlerError):
    """A Tuya cloud call failed, classified by ``kind``.

    Kinds mirror :class:`omnibutler.cloud_keys.CloudKeyError`:
    ``auth_failed`` (credentials or token rejected), ``network``
    (unreachable or 5xx) and ``bad_response`` (an answer this client
    cannot make sense of, or a business-level ``success=false``).
    Messages are built from fixed wording plus numeric codes only -
    the Access Secret and access tokens never appear in them.
    """

    def __init__(self, kind: str, message: str) -> None:
        if kind not in _KINDS:
            raise ValueError(f"unknown TuyaCloudError kind {kind!r}")
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------------
# Injectable HTTP layer (same contract as omnibutler.cloud_keys)
# ---------------------------------------------------------------------------


def _headers_to_dict(message: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if message is None:
        return out
    for key in message:
        values = message.get_all(key) or []
        out[key] = "\n".join(values) if len(values) > 1 else (values[0] if values else "")
    return out


def _read_limited(stream: Any) -> str:
    """Read a response body with a hard cap; oversize is bad_response.

    Reads one byte past the cap so an oversize body is *detected* and
    refused rather than silently truncated (a truncated JSON document
    could parse into wrong data downstream).
    """
    data = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise TuyaCloudError(
            KIND_BAD_RESPONSE,
            "Tuya cloud: the server's answer is over "
            f"{MAX_RESPONSE_BYTES // (1024 * 1024)} MiB; refusing to "
            "process it.",
        )
    return data.decode("utf-8", errors="replace")


def _urllib_http(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = _HTTP_TIMEOUT,
) -> HttpResponse:
    """Default HTTP implementation (stdlib urllib).

    HTTP error statuses are returned as responses, not raised, so the
    caller can classify them.
    """
    request = urllib.request.Request(url, data=data, method=method)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                status=response.status,
                text=_read_limited(response),
                headers=_headers_to_dict(response.headers),
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            status=exc.code,
            text=_read_limited(exc),
            headers=_headers_to_dict(exc.headers),
        )


# ---------------------------------------------------------------------------
# Signing (Tuya's published algorithm; same construction as cloud_keys)
# ---------------------------------------------------------------------------

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _string_to_sign(method: str, url_path: str, body: bytes = b"") -> str:
    content_hash = _EMPTY_SHA256 if not body else hashlib.sha256(body).hexdigest()
    return "\n".join([method.upper(), content_hash, "", url_path])


def _sign(access_id: str, access_secret: str, access_token: str | None,
          t: str, string_to_sign: str) -> str:
    source = access_id + (access_token or "") + t + string_to_sign
    return hmac.new(
        access_secret.encode("utf-8"), source.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().upper()


class _TuyaCloudClient:
    """Signed Tuya Open API client with a cached access token."""

    def __init__(self, access_id: str, access_secret: str,
                 http: Callable[..., HttpResponse], base_url: str) -> None:
        self._access_id = access_id
        self._access_secret = access_secret
        self._http = http
        self._base = base_url.rstrip("/")
        self._token: str | None = None
        self._token_expires_at = 0.0
        #: uid reported by the token answer (fallback when unconfigured).
        self.token_uid: str | None = None

    def __repr__(self) -> str:
        # Credentials and tokens are never part of any representation.
        return f"_TuyaCloudClient(base={self._base!r}, token_cached={self._token is not None})"

    # -- transport ------------------------------------------------------
    def _send(self, method: str, path: str, *,
              body: bytes | None = None,
              token: str | None = None, what: str) -> HttpResponse:
        t = str(int(time.time() * 1000))
        sign = _sign(self._access_id, self._access_secret, token, t,
                     _string_to_sign(method, path, body or b""))
        headers = {
            "client_id": self._access_id,
            "sign": sign,
            "t": t,
            "sign_method": "HMAC-SHA256",
        }
        if token:
            headers["access_token"] = token
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            return self._http(method, f"{self._base}{path}",
                              headers=headers, data=body,
                              timeout=_HTTP_TIMEOUT)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise TuyaCloudError(
                KIND_NETWORK,
                f"{what}: could not reach the Tuya cloud "
                f"({type(exc).__name__}). Check the network connection "
                "and try again.",
            ) from exc

    @staticmethod
    def _parse(response: HttpResponse, *, what: str,
               credential_step: bool = False) -> Any:
        """Validate one response and return its ``result`` field."""
        if response.status in (401, 403):
            raise TuyaCloudError(
                KIND_AUTH_FAILED,
                f"{what}: Tuya rejected the request (HTTP "
                f"{response.status}). Check the Access ID / Access "
                "Secret and that the project is linked to the App "
                "account.",
            )
        if response.status >= 500:
            raise TuyaCloudError(
                KIND_NETWORK,
                f"{what}: Tuya answered HTTP {response.status}. This "
                "looks like a problem on Tuya's side; try again later.",
            )
        if response.status != 200:
            raise TuyaCloudError(
                KIND_BAD_RESPONSE,
                f"{what}: unexpected HTTP {response.status} from Tuya.",
            )
        try:
            payload = json.loads(response.text)
        except (ValueError, TypeError):
            raise TuyaCloudError(
                KIND_BAD_RESPONSE,
                f"{what}: Tuya's answer is not JSON.",
            ) from None
        if not isinstance(payload, dict) or "success" not in payload:
            raise TuyaCloudError(
                KIND_BAD_RESPONSE,
                f"{what}: Tuya's answer is not the expected object.",
            )
        if not payload.get("success"):
            code = payload.get("code")
            if credential_step:
                raise TuyaCloudError(
                    KIND_AUTH_FAILED,
                    f"Tuya refused the project credentials (code "
                    f"{code}). Check the Access ID and Access Secret "
                    "of your IoT project and try again.",
                )
            raise TuyaCloudError(
                KIND_BAD_RESPONSE,
                f"{what}: Tuya answered with an error (code {code}).",
            )
        return payload.get("result")

    # -- token ------------------------------------------------------------
    def access_token(self) -> str:
        """The cached token, fetched/refreshed when stale."""
        now = time.time()
        if self._token is not None and now < self._token_expires_at:
            return self._token
        what = "Tuya cloud token"
        response = self._send("GET", "/v1.0/token?grant_type=1", what=what)
        result = self._parse(response, what=what, credential_step=True)
        if not isinstance(result, dict) or not result.get("access_token"):
            raise TuyaCloudError(
                KIND_BAD_RESPONSE,
                "Tuya cloud token: the server's answer carries no "
                "access token.",
            )
        self._token = str(result["access_token"])
        try:
            expire = float(result.get("expire_time") or 7200)
        except (TypeError, ValueError):
            expire = 7200.0
        self._token_expires_at = time.time() + max(0.0, expire - _TOKEN_SAFETY_MARGIN)
        if result.get("uid"):
            self.token_uid = str(result["uid"])
        return self._token

    def _drop_token(self) -> None:
        self._token = None
        self._token_expires_at = 0.0

    # -- signed business calls --------------------------------------------
    def request(self, method: str, path: str, *, what: str,
                body_obj: Any = None) -> Any:
        """One signed business call; returns the ``result`` field.

        A ``success=false`` answer with the token-invalid code drops
        the cached token and retries exactly once with a fresh one -
        tokens expire mid-flight in real deployments.
        """
        body = (json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
                if body_obj is not None else None)
        for attempt in (0, 1):
            token = self.access_token()
            response = self._send(method, path, body=body, token=token,
                                  what=what)
            try:
                return self._parse(response, what=what)
            except TuyaCloudError as exc:
                if (attempt == 0 and exc.kind == KIND_BAD_RESPONSE
                        and f"(code {_CODE_TOKEN_INVALID})" in str(exc)):
                    self._drop_token()
                    continue
                raise
        raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# DP code mapping - mirrors omnibutler.drivers.tuya (same vendor DP facts)
# ---------------------------------------------------------------------------

# Cloud status entries are {"code", "value"} pairs; the codes are the
# standard instruction-set codes of the device category.
_BULB_CODES = {
    "switch_led": "onoff",
    "bright_value": "brightness",
    "temp_value": "color_temp",
    "colour_data": "color",
}
_OUTLET_CODES = {
    "switch": "onoff",
    "switch_1": "onoff",
    "cur_power": "power",
    "add_ele": "energy",
}
# Curtain codes mirror the local DP facts: control is DP 1,
# percent_control DP 2, percent_state DP 3. percent_state is listed
# after percent_control so a reported position wins over the last
# commanded one when state is assembled.
_CURTAIN_CODES = {
    "control": "open_close",
    "percent_control": "position",
    "percent_state": "position",
}
_KIND_CODES = {"bulb": _BULB_CODES, "outlet": _OUTLET_CODES,
               "curtain": _CURTAIN_CODES}
_CANONICAL_TO_CODE = {
    "bulb": {"onoff": "switch_led", "brightness": "bright_value",
             "color_temp": "temp_value", "color": "colour_data"},
    "outlet": {"onoff": "switch"},
    "curtain": {"open_close": "control", "position": "percent_control"},
}
_CATEGORY_KINDS = {"dj": "bulb", "cz": "outlet", "pc": "outlet",
                   "cl": "curtain"}

_BRIGHTNESS_RAW_MIN, _BRIGHTNESS_RAW_MAX = 10, 1000


def _brightness_to_raw(percent: float) -> int:
    span = _BRIGHTNESS_RAW_MAX - _BRIGHTNESS_RAW_MIN
    return round(_BRIGHTNESS_RAW_MIN + percent / 100 * span)


def _brightness_from_raw(raw: float) -> int:
    span = _BRIGHTNESS_RAW_MAX - _BRIGHTNESS_RAW_MIN
    percent = (raw - _BRIGHTNESS_RAW_MIN) / span * 100
    return max(0, min(100, round(percent)))


def _color_temp_to_raw(kelvin: float, low: float, high: float) -> int:
    return round((kelvin - low) / (high - low) * 1000)


def _color_temp_from_raw(raw: float, low: float, high: float) -> int:
    return round(low + raw / 1000 * (high - low))


def _color_from_cloud(value: Any) -> str:
    """Cloud colour_data -> canonical ``#rrggbb``.

    Accepts the JSON ``{"h","s","v"}`` form (h 0-360, s/v 0-1000) and
    the 12-hex ``hhhhssssvvvv`` form the local protocol uses.
    """
    hue = sat = val = None
    if isinstance(value, dict):
        hue, sat, val = value.get("h"), value.get("s"), value.get("v")
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                hue, sat, val = parsed.get("h"), parsed.get("s"), parsed.get("v")
        elif len(text) == 12:
            try:
                hue = int(text[0:4], 16)
                sat = int(text[4:8], 16)
                val = int(text[8:12], 16)
            except ValueError:
                return text
    if hue is None or sat is None or val is None:
        return value if isinstance(value, str) else str(value)
    red, green, blue = colorsys.hsv_to_rgb(
        float(hue) / 360, float(sat) / 1000, float(val) / 1000)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"


def _color_to_cloud(hex_color: str) -> str:
    """Canonical ``#rrggbb`` -> cloud colour_data JSON string."""
    text = hex_color.lstrip("#")
    red, green, blue = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    hue, sat, val = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
    return json.dumps({
        "h": round(hue * 360), "s": round(sat * 1000), "v": round(val * 1000),
    }, ensure_ascii=False)


class TuyaCloudDriver(Driver):
    name = "tuya_cloud"

    def __init__(
        self,
        access_id: str | None = None,
        access_secret: str | None = None,
        *,
        uid: str | None = None,
        base_url: str | None = None,
        http: Callable[..., HttpResponse] | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self._access_id = access_id
        self._access_secret = access_secret
        self._uid = uid
        self._base_url = base_url
        self._http = http or _urllib_http
        self._config = config
        self._client: _TuyaCloudClient | None = None
        self._devices: dict[str, Device] = {}
        self._cloud_ids: dict[str, str] = {}
        self._kinds: dict[str, str] = {}

    def __repr__(self) -> str:
        # Deliberately excludes credentials and tokens.
        return f"TuyaCloudDriver(devices={sorted(self._devices)})"

    # -- configuration ----------------------------------------------------
    def _resolve_client(self) -> _TuyaCloudClient:
        if self._client is not None:
            return self._client
        config = self._config if self._config is not None else load_config()
        section = get_section(config, "tuya_cloud")
        access_id = (
            self._access_id
            or os.environ.get(_ENV_ACCESS_ID, "").strip()
            or resolve_secret(section.get("access_id"))
        )
        access_secret = (
            self._access_secret
            or os.environ.get(_ENV_ACCESS_SECRET, "").strip()
            or resolve_secret(section.get("access_secret"))
        )
        if not access_id or not access_secret:
            raise DriverNotConfiguredError(_NOT_CONFIGURED)
        self._configured_uid = (
            self._uid
            or os.environ.get(_ENV_UID, "").strip()
            or (str(section["uid"]) if section.get("uid") else None)
        )
        base_url = (
            self._base_url
            or os.environ.get(_ENV_BASE_URL, "").strip()
            or (str(section["base_url"]) if section.get("base_url") else None)
            or _DEFAULT_BASE_URL
        )
        self._client = _TuyaCloudClient(
            str(access_id), str(access_secret), self._http, base_url)
        return self._client

    def _effective_uid(self, client: _TuyaCloudClient) -> str | None:
        return getattr(self, "_configured_uid", None) or client.token_uid

    # -- cloud calls --------------------------------------------------------
    def _list_cloud_devices(self, client: _TuyaCloudClient) -> list[dict[str, Any]]:
        client.access_token()  # also harvests the token's uid
        uid = self._effective_uid(client)
        if uid:
            listed = client.request(
                "GET",
                f"/v1.0/users/{urllib.parse.quote(str(uid), safe='')}"
                "/devices",
                what="Tuya cloud device list")
        else:
            listed = client.request(
                "GET", "/v1.0/devices?page_no=1&page_size=100",
                what="Tuya cloud device list")
        if isinstance(listed, dict):
            listed = listed.get("devices") or listed.get("list")
        if not isinstance(listed, list):
            raise TuyaCloudError(
                KIND_BAD_RESPONSE,
                "Tuya cloud device list: the server's answer has no "
                "device list.",
            )
        return [entry for entry in listed if isinstance(entry, dict)]

    def _device_status(self, client: _TuyaCloudClient,
                       cloud_id: str) -> dict[str, Any]:
        status = client.request(
            "GET",
            f"/v1.0/devices/{urllib.parse.quote(str(cloud_id), safe='')}"
            "/status",
            what=f"Tuya cloud device {cloud_id} status")
        if isinstance(status, dict):  # some shapes wrap the list
            status = status.get("status") or status.get("list")
        if not isinstance(status, list):
            raise TuyaCloudError(
                KIND_BAD_RESPONSE,
                f"Tuya cloud device {cloud_id} status: the server's "
                "answer is not a status list.",
            )
        out: dict[str, Any] = {}
        for item in status:
            if isinstance(item, dict) and item.get("code") is not None:
                out[str(item["code"])] = item.get("value")
        return out

    # -- device modelling ---------------------------------------------------
    @staticmethod
    def _kind_for(entry: Mapping[str, Any], status: Mapping[str, Any]) -> str:
        if any(code in status for code in
               ("bright_value", "temp_value", "colour_data", "switch_led")):
            return "bulb"
        if any(code in status for code in
               ("percent_control", "percent_state")):
            return "curtain"
        category = str(entry.get("category", "")).lower()
        return _CATEGORY_KINDS.get(category, "outlet")

    def _state_from_status(self, device: Device, kind: str,
                           status: Mapping[str, Any]) -> dict[str, Any]:
        codes = _KIND_CODES[kind]
        state: dict[str, Any] = {}
        for code, canonical in codes.items():
            if code not in status:
                continue
            value = status[code]
            if canonical == "onoff":
                state["onoff"] = bool(value)
            elif canonical == "brightness":
                state["brightness"] = _brightness_from_raw(float(value))
            elif canonical == "color_temp":
                prop = device.properties["color_temp"]
                low = prop.effective_minimum or 2700
                high = prop.effective_maximum or 6500
                state["color_temp"] = _color_temp_from_raw(
                    float(value), low, high)
            elif canonical == "color":
                state["color"] = _color_from_cloud(value)
            elif canonical == "power":
                state["power"] = float(value) / 10  # 0.1 W units
            elif canonical == "energy":
                state["energy"] = float(value) / 100  # 0.01 kWh units
            elif canonical == "open_close":
                text = str(value).lower()
                if text == "open":
                    state["open_close"] = True
                elif text == "close":
                    state["open_close"] = False
                elif text == "stop":
                    # Mirror the local driver: "stop" only says the
                    # motor halted; infer from the reported position
                    # rather than inventing a direction.
                    reported = status.get(
                        "percent_state", status.get("percent_control"))
                    if reported is not None:
                        state["open_close"] = float(reported) > 0
            elif canonical == "position":
                state["position"] = max(
                    0, min(100, round(float(value))))
        return {k: v for k, v in state.items() if k in device.properties}

    def _build_device(self, entry: Mapping[str, Any],
                      status: Mapping[str, Any]) -> Device:
        cloud_id = str(entry.get("id") or entry.get("device_id") or "")
        kind = self._kind_for(entry, status)
        device = Device(
            id=f"tuyac-{cloud_id}",
            name=str(entry.get("name") or f"Tuya {kind} {cloud_id[-4:]}"),
            driver="tuya_cloud",
            room=str(entry.get("room") or "unknown"),
            brand="Tuya",
            model=str(entry.get("product_id") or entry.get("product_name")
                      or kind),
            properties=dict(KIND_PROPERTIES[kind]),
            actions=["turn_on", "turn_off", "toggle"],
            online=bool(entry.get("online", True)),
        )
        device.state = self._state_from_status(device, kind, status)
        self._cloud_ids[device.id] = cloud_id
        self._kinds[device.id] = kind
        return device

    def _lookup(self, device_id: str) -> Device:
        try:
            return self._devices[device_id]
        except KeyError:
            raise DeviceNotFoundError(
                f"Tuya cloud driver has no device {device_id!r}; "
                f"discovered: {sorted(self._devices)}"
            ) from None

    # -- Driver API -----------------------------------------------------------
    def discover(self) -> list[Device]:
        client = self._resolve_client()
        devices: dict[str, Device] = {}
        for entry in self._list_cloud_devices(client):
            cloud_id = str(entry.get("id") or entry.get("device_id") or "")
            if not cloud_id:
                continue
            try:
                status = self._device_status(client, cloud_id)
            except TuyaCloudError as exc:
                # One sick device must not hide the rest of the home;
                # it is listed with empty state instead.
                logger.warning(
                    "tuya_cloud: status for device %s failed (%s: %s); "
                    "listing it without state", cloud_id, exc.kind, exc)
                status = {}
            device = self._build_device(entry, status)
            devices[device.id] = device
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
        status = self._device_status(client, self._cloud_ids[device_id])
        device.state = self._state_from_status(
            device, self._kinds[device_id], status)
        return dict(device.state)

    def set_property(self, device_id: str, property_name: str,
                     value: Any) -> dict[str, Any]:
        client = self._resolve_client()
        if device_id not in self._devices:
            self.discover()
        device = self._lookup(device_id)
        prop = device.property(property_name)
        if not prop.is_writable:
            raise PropertyValidationError(
                f"{property_name}: property is read-only")
        canonical = prop.validate(value)
        kind = self._kinds[device_id]
        code = _CANONICAL_TO_CODE[kind].get(property_name)
        if code is None:
            raise OmniButlerError(
                f"Tuya cloud driver cannot set {property_name!r} on a "
                f"{kind} device.")
        if property_name == "onoff":
            raw: Any = bool(canonical)
        elif property_name == "brightness":
            raw = _brightness_to_raw(float(canonical))
        elif property_name == "color_temp":
            low = prop.effective_minimum or 2700
            high = prop.effective_maximum or 6500
            raw = _color_temp_to_raw(float(canonical), low, high)
        elif property_name == "color":
            raw = _color_to_cloud(str(canonical))
        elif property_name == "open_close":
            raw = "open" if canonical else "close"
        elif property_name == "position":
            raw = max(0, min(100, round(float(canonical))))
        else:  # pragma: no cover - guarded by the code table above
            raw = canonical
        cloud_id = self._cloud_ids[device_id]
        client.request(
            "POST",
            f"/v1.0/devices/{urllib.parse.quote(str(cloud_id), safe='')}"
            "/commands",
            what=f"Tuya cloud device {cloud_id} command",
            body_obj={"commands": [{"code": code, "value": raw}]},
        )
        device.state[property_name] = canonical
        return {property_name: canonical}

    def call_action(self, device_id: str, action: str,
                    params: dict[str, Any]) -> dict[str, Any]:
        if action in {"turn_on", "turn_off", "toggle"}:
            if device_id not in self._devices:
                self.discover()
            if self._kinds.get(device_id) == "curtain":
                # Curtains have no on/off; the actions mean open/close.
                if action == "toggle":
                    current = self.get_state(device_id).get(
                        "open_close", False)
                    return self.set_property(
                        device_id, "open_close", not current)
                return self.set_property(
                    device_id, "open_close", action == "turn_on")
        if action in {"turn_on", "turn_off"}:
            return self.set_property(device_id, "onoff", action == "turn_on")
        if action == "toggle":
            current = self.get_state(device_id).get("onoff", False)
            return self.set_property(device_id, "onoff", not current)
        raise OmniButlerError(
            f"Tuya cloud driver does not support action {action!r}; "
            "supported: turn_on, turn_off, toggle."
        )

"""One-time cloud key fetching ("fetch the key over the cloud, once").

Some vendors give a device's local key out only through their cloud:
a Xiaomi miIO ``token`` lives behind the Xiaomi account service, and a
Tuya ``local_key`` behind the Tuya IoT platform. There is no way around
that first trip - but it is a *one-time* trip. This module performs it:

* :func:`fetch_xiaomi_tokens` logs in to the user's own Xiaomi account
  (the public mi cloud login flow), follows the STS redirect to a
  ``serviceToken`` and asks the miot API for the device list, which
  carries each device's local token.
* :func:`fetch_tuya_local_keys` signs requests to the Tuya IoT Open API
  with the user's own project credentials, lists the devices linked to
  their Tuya App account and reads each device's ``local_key``.

What happens to the keys afterwards is deliberately *not* this module's
job: the integrator stores them in the local config file (see
:func:`omnibutler.setup_guide.store_secret`, mode 0600) and every
day-to-day control call after that is local - the cloud is never
contacted again unless a device is re-paired and its key rotates.

Secrecy rules, same as the rest of the project:

* Passwords / secrets pass through memory only. They are never logged,
  never returned, and never interpolated into exception text (error
  messages are built from fixed wording plus numeric codes only), so a
  :class:`CloudKeyError` is always safe to show a user or write to a log.
* When Xiaomi answers with a captcha or a two-step-verification page,
  this module does **not** try to get around it: it raises
  :class:`CloudKeyError` with kind ``needs_human_verification`` and the
  message tells the user to finish verification in a browser or fall
  back to the manual guide in :mod:`omnibutler.setup_guide`.

The HTTP layer is injectable (``http=``) so tests run entirely against
fakes; the default implementation is stdlib ``urllib``.

Honesty note - what is *not* verified: this sandbox has no route to the
real Xiaomi or Tuya clouds, so neither flow has been run against the
live services. The Tuya signing follows Tuya's published algorithm and
is self-checked against test vectors in ``tests/test_cloud_keys.py``.
The Xiaomi flow follows the widely used public mi cloud login sequence
(serviceLogin -> serviceLoginAuth2 -> STS -> signed device_list); the
device_list call uses the plain signed-parameter variant - Xiaomi's
servers also speak an RC4-encrypted variant, and whether the plain
variant is still accepted everywhere could not be confirmed offline.
If a live account behaves differently, the failure surfaces as a
classified :class:`CloudKeyError`, never as a silent wrong key.
"""

from __future__ import annotations

import base64
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
from dataclasses import dataclass, field
from typing import Any

from omnibutler.core.errors import OmniButlerError

logger = logging.getLogger(__name__)

KIND_AUTH_FAILED = "auth_failed"
KIND_NEEDS_HUMAN = "needs_human_verification"
KIND_NETWORK = "network"
KIND_BAD_RESPONSE = "bad_response"

_KINDS = (KIND_AUTH_FAILED, KIND_NEEDS_HUMAN, KIND_NETWORK, KIND_BAD_RESPONSE)

_HTTP_TIMEOUT = 15.0
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class CloudKeyError(OmniButlerError):
    """A cloud key fetch failed, classified by ``kind``.

    ``kind`` is one of ``"auth_failed"`` (the cloud rejected the
    credentials), ``"needs_human_verification"`` (captcha / two-step
    verification - a human must finish it in a browser; this code never
    tries to bypass it), ``"network"`` (the cloud could not be reached
    or answered 5xx) or ``"bad_response"`` (the cloud answered
    something this client cannot make sense of). The message never
    contains the password, secret or any token material.
    """

    def __init__(self, kind: str, message: str) -> None:
        if kind not in _KINDS:
            raise ValueError(f"unknown CloudKeyError kind {kind!r}")
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------------
# Injectable HTTP layer
# ---------------------------------------------------------------------------


@dataclass
class HttpResponse:
    """One HTTP response as the fetchers need it.

    ``headers`` maps header names to values; repeated headers (notably
    ``Set-Cookie``) are joined with ``"\\n"``. Lookup via
    :meth:`header` is case-insensitive.
    """

    status: int
    text: str
    headers: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


def _headers_to_dict(message: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if message is None:
        return out
    for key in message:
        values = message.get_all(key) or []
        out[key] = "\n".join(values) if len(values) > 1 else (values[0] if values else "")
    return out


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
    callers can classify them; transport failures raise and are turned
    into ``network`` errors by :func:`_send`.
    """
    request = urllib.request.Request(url, data=data, method=method)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                status=response.status,
                text=response.read().decode("utf-8", errors="replace"),
                headers=_headers_to_dict(response.headers),
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            status=exc.code,
            text=exc.read().decode("utf-8", errors="replace"),
            headers=_headers_to_dict(exc.headers),
        )


def _send(
    http: Callable[..., HttpResponse],
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
    what: str,
) -> HttpResponse:
    """One HTTP call with transport failures classified as ``network``."""
    try:
        return http(method, url, headers=headers, data=data, timeout=_HTTP_TIMEOUT)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise CloudKeyError(
            KIND_NETWORK,
            f"{what}: could not reach the server ({type(exc).__name__}). "
            "Check the network connection and try again.",
        ) from exc


def _raise_for_status(response: HttpResponse, *, what: str) -> None:
    """Classify a non-200 status; auth statuses mean bad credentials."""
    if response.status == 200:
        return
    if response.status in (401, 403):
        raise CloudKeyError(
            KIND_AUTH_FAILED,
            f"{what}: the server rejected the credentials (HTTP "
            f"{response.status}). Check the account name and password "
            "and try again.",
        )
    if response.status >= 500:
        raise CloudKeyError(
            KIND_NETWORK,
            f"{what}: the server answered HTTP {response.status}. "
            "This looks like a problem on the vendor's side; try again later.",
        )
    raise CloudKeyError(
        KIND_BAD_RESPONSE,
        f"{what}: unexpected HTTP {response.status} from the server.",
    )


def _parse_json(text: str, *, what: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        raise CloudKeyError(
            KIND_BAD_RESPONSE,
            f"{what}: the server answered something that is not JSON.",
        ) from None


# ---------------------------------------------------------------------------
# Xiaomi mi cloud: serviceLogin -> serviceLoginAuth2 -> STS -> device_list
# ---------------------------------------------------------------------------

_XIAOMI_ACCOUNT = "https://account.xiaomi.com"
_XIAOMI_SID = "xiaomiio"
# Xiaomi login code that means "solve a graphical captcha first".
_XIAOMI_CODE_CAPTCHA = 87001


def _xiaomi_nonce(millis: int | None = None) -> str:
    """mi cloud nonce: 8 random bytes + minutes-since-epoch, base64."""
    if millis is None:
        millis = int(time.time() * 1000)
    raw = os.urandom(8) + (millis // 60000).to_bytes(4, byteorder="big")
    return base64.b64encode(raw).decode()


def _xiaomi_signed_nonce(ssecurity: str, nonce: str) -> str:
    """signed_nonce = base64(sha256(base64decode(ssecurity) + nonce bytes))."""
    try:
        key = base64.b64decode(ssecurity)
        nonce_bytes = base64.b64decode(nonce)
    except (ValueError, TypeError):
        raise CloudKeyError(
            KIND_BAD_RESPONSE,
            "Xiaomi login: the server returned an unusable security "
            "token; cannot sign the device request.",
        ) from None
    digest = hashlib.sha256(key + nonce_bytes).digest()
    return base64.b64encode(digest).decode()


def _xiaomi_signature(path: str, signed_nonce: str, nonce: str,
                      params: Mapping[str, str]) -> str:
    """Request signature over path, nonces and ``k=v`` pairs, in order."""
    parts = [path, signed_nonce, nonce]
    parts.extend(f"{key}={value}" for key, value in params.items())
    digest = hashlib.sha256("&".join(parts).encode("utf-8")).digest()
    return base64.b64encode(digest).decode()


def _xiaomi_login_fields(http: Callable[..., HttpResponse]) -> dict[str, Any]:
    """Step 1: GET serviceLogin to obtain qs / callback / _sign."""
    what = "Xiaomi login (step 1)"
    url = (f"{_XIAOMI_ACCOUNT}/pass/serviceLogin"
           f"?sid={_XIAOMI_SID}&_json=true")
    response = _send(http, "GET", url, headers={"User-Agent": _USER_AGENT},
                     what=what)
    _raise_for_status(response, what=what)
    text = response.text
    start = text.find("{")
    if start < 0:
        raise CloudKeyError(
            KIND_BAD_RESPONSE,
            "Xiaomi login (step 1): the server answered something "
            "that is not JSON.",
        )
    fields = _parse_json(text[start:], what=what)
    if not isinstance(fields, dict) or "_sign" not in fields:
        raise CloudKeyError(
            KIND_BAD_RESPONSE,
            "Xiaomi login (step 1): the server's answer is missing the "
            "signing field; the login flow may have changed.",
        )
    return fields


def _xiaomi_authenticate(
    http: Callable[..., HttpResponse],
    username: str,
    password: str,
    fields: Mapping[str, Any],
) -> dict[str, Any]:
    """Step 2: POST serviceLoginAuth2 with the MD5-hashed password.

    The password itself is hashed in memory and only the hash goes on
    the wire (that is how Xiaomi's own clients do it); the raw password
    never leaves this function and never appears in any message.
    """
    what = "Xiaomi login (step 2)"
    form = {
        "sid": _XIAOMI_SID,
        "callback": str(fields.get("callback", "")),
        "qs": str(fields.get("qs", "")),
        "_sign": str(fields.get("_sign", "")),
        "user": username,
        "hash": hashlib.md5(password.encode("utf-8")).hexdigest().upper(),
        "_json": "true",
    }
    response = _send(
        http, "POST", f"{_XIAOMI_ACCOUNT}/pass/serviceLoginAuth2",
        headers={
            "User-Agent": _USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=urllib.parse.urlencode(form).encode("utf-8"),
        what=what,
    )
    _raise_for_status(response, what=what)
    result = _parse_json(response.text, what=what)
    if not isinstance(result, dict):
        raise CloudKeyError(
            KIND_BAD_RESPONSE,
            "Xiaomi login (step 2): the server's answer is not an object.",
        )
    # Human-verification walls first: never try to work around them.
    if result.get("captchaUrl") or result.get("code") == _XIAOMI_CODE_CAPTCHA:
        raise CloudKeyError(
            KIND_NEEDS_HUMAN,
            "Xiaomi is asking for a captcha before it will log this "
            "account in. OmniButler will not try to get around that: "
            "log in once in a browser (or in the Mi Home app) to clear "
            "it, or use the manual token guide instead (tob setup miio).",
        )
    if result.get("notificationUrl"):
        raise CloudKeyError(
            KIND_NEEDS_HUMAN,
            "Xiaomi requires two-step verification for this account. "
            "OmniButler cannot complete that for you: finish the "
            "verification in a browser or the Mi Home app first, or "
            "use the manual token guide instead (tob setup miio).",
        )
    code = result.get("code")
    if code != 0:
        raise CloudKeyError(
            KIND_AUTH_FAILED,
            f"Xiaomi rejected the login (code {code}). The usual cause "
            "is a wrong account name or password; check both and try "
            "again.",
        )
    if not result.get("ssecurity") or not result.get("location"):
        raise CloudKeyError(
            KIND_BAD_RESPONSE,
            "Xiaomi login (step 2): the server accepted the login but "
            "returned no session material; the login flow may have changed.",
        )
    return result


def _xiaomi_service_token(
    http: Callable[..., HttpResponse], location: str
) -> str:
    """Step 3: follow the STS URL and harvest the serviceToken cookie."""
    what = "Xiaomi login (step 3, STS)"
    response = _send(http, "GET", location,
                     headers={"User-Agent": _USER_AGENT}, what=what)
    _raise_for_status(response, what=what)
    cookies = response.header("Set-Cookie") or ""
    for chunk in cookies.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if chunk.startswith("serviceToken="):
            token = chunk[len("serviceToken="):].strip()
            if token:
                return token
    raise CloudKeyError(
        KIND_BAD_RESPONSE,
        "Xiaomi login (step 3, STS): the server did not set a "
        "serviceToken cookie; the login flow may have changed.",
    )


def _xiaomi_device_list(
    http: Callable[..., HttpResponse],
    *,
    country: str,
    user_id: Any,
    ssecurity: str,
    service_token: str,
) -> list[dict[str, Any]]:
    """Step 4: signed POST to the miot API's /home/device_list."""
    what = "Xiaomi device list"
    host = "api.io.mi.com" if country == "cn" else f"{country}.api.io.mi.com"
    path = "/app/home/device_list"
    url = f"https://{host}{path}"
    nonce = _xiaomi_nonce()
    signed_nonce = _xiaomi_signed_nonce(ssecurity, nonce)
    params = {"data": '{"getVirtualModel":false,"getHuamiDevices":0}'}
    form = {
        **params,
        "signature": _xiaomi_signature(path, signed_nonce, nonce, params),
        "_nonce": nonce,
        "ssecurity": ssecurity,
    }
    response = _send(
        http, "POST", url,
        headers={
            "User-Agent": _USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
            "Cookie": f"userId={user_id}; serviceToken={service_token}",
        },
        data=urllib.parse.urlencode(form).encode("utf-8"),
        what=what,
    )
    _raise_for_status(response, what=what)
    payload = _parse_json(response.text, what=what)
    if not isinstance(payload, dict):
        raise CloudKeyError(
            KIND_BAD_RESPONSE,
            "Xiaomi device list: the server's answer is not an object.",
        )
    if payload.get("code") != 0:
        raise CloudKeyError(
            KIND_BAD_RESPONSE,
            f"Xiaomi device list: the server answered with code "
            f"{payload.get('code')}; the session may have expired - "
            "run the fetch again.",
        )
    result = payload.get("result")
    devices = result.get("list") if isinstance(result, dict) else None
    if not isinstance(devices, list):
        raise CloudKeyError(
            KIND_BAD_RESPONSE,
            "Xiaomi device list: the server's answer has no device list.",
        )
    return devices


def fetch_xiaomi_tokens(
    username: str,
    password: str,
    *,
    http: Callable[..., HttpResponse] | None = None,
    country: str = "cn",
) -> list[dict[str, Any]]:
    """Fetch every Xiaomi device's local miIO token, over the cloud, once.

    Logs in with the user's own Xiaomi account (public mi cloud flow:
    serviceLogin -> serviceLoginAuth2 -> STS ``serviceToken`` -> signed
    ``/home/device_list``) and returns one dict per device that has a
    token::

        {"name": ..., "model": ..., "did": ..., "mac": ...,
         "ip": ..., "token": ...}

    (``ip`` is the device's LAN address as Xiaomi last saw it - a hint
    for the local config, worth re-checking with ``tob doctor``.)

    ``country`` is the Xiaomi account region (``"cn"``, ``"de"``,
    ``"us"``, ...); it selects the miot API host. The password is used
    only to compute the login hash in memory: it is never logged,
    returned or embedded in an error. Raises :class:`CloudKeyError`
    (kinds: ``auth_failed``, ``needs_human_verification``, ``network``,
    ``bad_response``). Store the returned tokens locally right away -
    see :func:`omnibutler.setup_guide.store_secret`.
    """
    if not username or not password:
        raise CloudKeyError(
            KIND_AUTH_FAILED,
            "Xiaomi login: an account name and a password are both "
            "required.",
        )
    http = http or _urllib_http
    fields = _xiaomi_login_fields(http)
    auth = _xiaomi_authenticate(http, username, password, fields)
    service_token = _xiaomi_service_token(http, str(auth["location"]))
    devices = _xiaomi_device_list(
        http,
        country=country,
        user_id=auth.get("userId"),
        ssecurity=str(auth["ssecurity"]),
        service_token=service_token,
    )
    out: list[dict[str, Any]] = []
    for device in devices:
        if not isinstance(device, dict):
            continue
        token = device.get("token")
        if not token:
            continue  # virtual / shared devices carry no local token
        out.append({
            "name": device.get("name"),
            "model": device.get("model"),
            "did": device.get("did"),
            "mac": device.get("mac"),
            "ip": device.get("localip"),
            "token": token,
        })
    logger.debug("xiaomi key fetch: %d device(s) with a token", len(out))
    return out


# ---------------------------------------------------------------------------
# Tuya IoT Open API: signed token -> user devices -> per-device local_key
# ---------------------------------------------------------------------------

_TUYA_DEFAULT_BASE = "https://openapi.tuyacn.com"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _tuya_string_to_sign(method: str, url_path: str,
                         body: bytes = b"") -> str:
    """The stringToSign of Tuya's published signing algorithm.

    ``method + "\\n" + sha256(body) + "\\n" + signed-headers + "\\n" +
    url`` - this client signs no extra headers, so that component is
    empty. ``url_path`` is the path plus the sorted query string.
    """
    content_hash = _EMPTY_SHA256 if not body else hashlib.sha256(body).hexdigest()
    return "\n".join([method.upper(), content_hash, "", url_path])


def _tuya_sign(access_id: str, access_secret: str,
               access_token: str | None, t: str,
               string_to_sign: str) -> str:
    """sign = upper(hex(HMAC-SHA256(secret, client_id [+ token] + t + stringToSign)))."""
    source = access_id + (access_token or "") + t + string_to_sign
    return hmac.new(
        access_secret.encode("utf-8"), source.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().upper()


class _TuyaClient:
    """Minimal signed Tuya Open API client (GET only, as the flow needs)."""

    def __init__(self, access_id: str, access_secret: str,
                 http: Callable[..., HttpResponse], base_url: str) -> None:
        self._access_id = access_id
        self._access_secret = access_secret
        self._http = http
        self._base = base_url.rstrip("/")
        self.access_token: str | None = None

    def get(self, path: str, *, what: str) -> Any:
        """One signed GET; returns the ``result`` field of the answer."""
        t = str(int(time.time() * 1000))
        sign = _tuya_sign(
            self._access_id, self._access_secret, self.access_token, t,
            _tuya_string_to_sign("GET", path),
        )
        headers = {
            "client_id": self._access_id,
            "sign": sign,
            "t": t,
            "sign_method": "HMAC-SHA256",
        }
        if self.access_token:
            headers["access_token"] = self.access_token
        response = _send(self._http, "GET", f"{self._base}{path}",
                         headers=headers, what=what)
        if response.status in (401, 403):
            raise CloudKeyError(
                KIND_AUTH_FAILED,
                f"{what}: Tuya rejected the request (HTTP "
                f"{response.status}). Check the Access ID / Access "
                "Secret and that the project is linked to the App account.",
            )
        _raise_for_status(response, what=what)
        payload = _parse_json(response.text, what=what)
        if not isinstance(payload, dict) or "success" not in payload:
            raise CloudKeyError(
                KIND_BAD_RESPONSE,
                f"{what}: Tuya's answer is not the expected object.",
            )
        if not payload.get("success"):
            code = payload.get("code")
            if self.access_token is None:
                raise CloudKeyError(
                    KIND_AUTH_FAILED,
                    f"Tuya refused the project credentials (code "
                    f"{code}). Check the Access ID and Access Secret "
                    "of your IoT project and try again.",
                )
            raise CloudKeyError(
                KIND_BAD_RESPONSE,
                f"{what}: Tuya answered with an error (code {code}).",
            )
        return payload.get("result")


def fetch_tuya_local_keys(
    access_id: str,
    access_secret: str,
    uid: str,
    *,
    http: Callable[..., HttpResponse] | None = None,
    base_url: str = _TUYA_DEFAULT_BASE,
) -> list[dict[str, Any]]:
    """Fetch every linked Tuya device's ``local_key``, over the cloud, once.

    Uses the user's own Tuya IoT project credentials (``access_id`` /
    ``access_secret`` from iot.tuya.com) and the uid of the Tuya App
    account linked to that project: signs a token request
    (HMAC-SHA256, Tuya's published algorithm), lists the account's
    devices, then reads each device's detail for its ``local_key``.
    Returns one dict per device that has a key::

        {"device_id": ..., "name": ..., "local_key": ...}

    ``base_url`` selects the Tuya data center (default: China,
    ``https://openapi.tuyacn.com``). The secret is used only as the
    HMAC key in memory: it is never logged, returned or embedded in an
    error; neither is the short-lived access token. Raises
    :class:`CloudKeyError` (kinds: ``auth_failed``, ``network``,
    ``bad_response``). Store the returned keys locally right away - see
    :func:`omnibutler.setup_guide.store_secret`.
    """
    if not access_id or not access_secret or not uid:
        raise CloudKeyError(
            KIND_AUTH_FAILED,
            "Tuya key fetch: Access ID, Access Secret and the linked "
            "App account uid are all required.",
        )
    client = _TuyaClient(access_id, access_secret, http or _urllib_http,
                         base_url)
    token_info = client.get("/v1.0/token?grant_type=1", what="Tuya token")
    if not isinstance(token_info, dict) or not token_info.get("access_token"):
        raise CloudKeyError(
            KIND_BAD_RESPONSE,
            "Tuya token: the server's answer carries no access token.",
        )
    client.access_token = str(token_info["access_token"])

    listed = client.get(f"/v1.0/users/{uid}/devices",
                        what="Tuya device list")
    if isinstance(listed, dict):
        listed = listed.get("devices") or listed.get("list")
    if not isinstance(listed, list):
        raise CloudKeyError(
            KIND_BAD_RESPONSE,
            "Tuya device list: the server's answer has no device list.",
        )

    out: list[dict[str, Any]] = []
    for entry in listed:
        if not isinstance(entry, dict):
            continue
        device_id = entry.get("id") or entry.get("device_id")
        if not device_id:
            continue
        detail = client.get(f"/v1.0/devices/{device_id}",
                            what=f"Tuya device {device_id}")
        if not isinstance(detail, dict):
            continue
        local_key = detail.get("local_key") or entry.get("local_key")
        if not local_key:
            continue  # some device classes expose no local key
        out.append({
            "device_id": device_id,
            "name": detail.get("name") or entry.get("name"),
            "local_key": local_key,
        })
    logger.debug("tuya key fetch: %d device(s) with a local key", len(out))
    return out

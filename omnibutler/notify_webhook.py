"""Webhook notifications: a phone push when a high-risk action queues up.

High-risk actions park in the confirmation queue until a human approves
them out-of-band (approvals web page, macOS dialog, ``tob confirm``).
Those surfaces only help if the human happens to be looking: a bridge
running on a headless Linux box or in Docker has no dialog to pop, and
the web page does not announce itself. This module is the missing
announcement: when the daemon sees a *newly queued* confirmation, it
POSTs one JSON document to an operator-configured webhook URL - the
shape ntfy, Bark, WeCom group bots and similar services accept - so
the phone buzzes and the human can go approve (or reject) through the
usual human-only paths.

The webhook only ever *announces*; there is nothing to answer on it and
it changes nothing about the safety model. In particular there is still
no programmatic approve path, and this module is never reachable
through MCP.

Configuration (first hit wins):

* ``$OMNIBUTLER_NOTIFY_WEBHOOK_URL`` - the URL, straight from the
  environment;
* the ``notify`` section of the local config file:
  ``{"notify": {"webhook_url": "..."}}`` (the value may be an
  ``env:VAR`` reference, like every other secret in the config file -
  webhook URLs often embed a topic or key).

Unconfigured means silent: no URL, no POST, no noise. A configured
webhook that is down or slow must never hurt the bridge - sending uses
a short timeout, and every failure is reported on stderr and swallowed.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from omnibutler import config as config_module

#: Environment variable holding the webhook URL (wins over the config file).
WEBHOOK_ENV_VAR = "OMNIBUTLER_NOTIFY_WEBHOOK_URL"

#: Per-request timeout: long enough for a sleepy push service, short
#: enough that a dead one never stalls the daemon's tick.
DEFAULT_TIMEOUT = 5.0


def resolve_webhook_url(
    config: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """The configured webhook URL, or None when notifications are off.

    ``$OMNIBUTLER_NOTIFY_WEBHOOK_URL`` wins; otherwise the config
    file's ``notify.webhook_url`` applies (an ``env:`` reference is
    resolved like any other config secret). ``config`` is a dict as
    returned by :func:`omnibutler.config.load_config`; None loads the
    local config file, and a config file that cannot be parsed counts
    as "no webhook" here (reporting that is doctor's job). The URL can
    embed a credential (an ntfy topic, a Bark key): treat the result
    as a secret - never log it.
    """
    environ = os.environ if environ is None else environ
    from_env = environ.get(WEBHOOK_ENV_VAR, "").strip()
    if from_env:
        return from_env
    if config is None:
        try:
            config = config_module.load_config(environ=environ)
        except config_module.ConfigError:
            return None
    section = config_module.get_section(config, "notify")
    return config_module.resolve_secret(
        section.get("webhook_url"), environ=environ)


def describe_action(item) -> str:
    """One human line naming the parked action (mirrors the web page)."""
    if item.kind == "set_property":
        return f"set {item.device_id}.{item.name} = {item.value!r}"
    return f"call {item.device_id}.{item.name}({item.params})"


def build_payload(item) -> dict[str, Any]:
    """The JSON document POSTed for one newly queued confirmation.

    Generic on purpose - any service that accepts an HTTP POST of JSON
    can consume it. ``title`` / ``message`` are pre-rendered so services
    that display two fields (ntfy, Bark) show something sensible
    without mapping the raw fields themselves.
    """
    action = describe_action(item)
    created = datetime.datetime.fromtimestamp(
        item.created_at, tz=datetime.UTC)
    return {
        "event": "approval_requested",
        "id": item.id,
        "device": item.device_id,
        "device_id": item.device_id,
        "kind": item.kind,
        "action": action,
        "name": item.name,
        "value": item.value,
        "params": dict(item.params),
        "risk": item.risk,
        "requested_by": item.requested_by,
        "scene": item.scene,
        "created_at": item.created_at,
        "created_at_iso": created.isoformat(),
        "title": "Tiybai OmniButler: approval needed",
        "message": (
            f"High-risk action waiting for approval: {action} "
            f"(risk: {item.risk}, id: {item.id})"
        ),
    }


def send_webhook(
    url: str,
    payload: Mapping[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    opener: Callable | None = None,
) -> bool:
    """POST *payload* as JSON to *url*; True when the POST went out.

    ``opener`` is the transport: a ``urllib.request.urlopen``-compatible
    callable ``opener(request, timeout=...)`` (injectable for tests);
    the default is :func:`urllib.request.urlopen`. Any failure - DNS,
    refused, timeout, a non-2xx status - is printed to stderr and
    reported as False. This function never raises: a dead notification
    service must not take the daemon down with it.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    open_fn = urllib.request.urlopen if opener is None else opener
    try:
        with open_fn(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status is not None and not 200 <= status < 300:
                print(
                    f"omnibutler webhook: POST returned HTTP {status}",
                    file=sys.stderr,
                )
                return False
    except Exception as exc:  # never propagate, by design
        print(f"omnibutler webhook: POST failed ({exc})", file=sys.stderr)
        return False
    return True


class WebhookNotifier:
    """Sends approval notifications to one configured webhook URL.

    ``notify(item)`` never raises (see :func:`send_webhook`), so the
    daemon can call it inline in its tick loop.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        opener: Callable | None = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.opener = opener

    def notify(self, item) -> bool:
        """POST the approval notification for *item*; True on success."""
        return send_webhook(
            self.url, build_payload(item),
            timeout=self.timeout, opener=self.opener,
        )


def make_webhook_notifier(
    config: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    opener: Callable | None = None,
) -> WebhookNotifier | None:
    """A notifier for the configured webhook, or None when unconfigured."""
    url = resolve_webhook_url(config, environ=environ)
    if url is None:
        return None
    return WebhookNotifier(url, timeout=timeout, opener=opener)

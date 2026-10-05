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

import contextlib
import datetime
import json
import os
import queue
import sys
import threading
import time
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
        detail = str(exc)
        if url and url in detail:
            # Some transport errors embed the full URL (e.g. "unknown
            # url type: 'https://…/secret-topic'"), and a webhook URL
            # can carry a credential - never print it whole.
            detail = detail.replace(url, "<webhook URL>")
        print(f"omnibutler webhook: POST failed ({detail})",
              file=sys.stderr)
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


class AsyncNotifier:
    """Background-queue wrapper around a synchronous notifier.

    The daemon's tick used to call the webhook inline, so one slow
    push service (up to the full send timeout, per item) stalled every
    tick behind it. With this wrapper ``notify()`` only enqueues and
    returns at once; a single daemon thread sends in queue order.
    Send semantics are unchanged: failures are reported on stderr by
    the wrapped notifier and never retried. A full queue drops the
    announcement with a stderr note rather than blocking the tick -
    announcements are best-effort by design.

    ``close()`` flushes with a bounded wait (the daemon calls it on
    shutdown): it waits for queued items to drain, stops the thread,
    and gives up after ``timeout`` seconds rather than holding the
    process hostage to a dead endpoint.
    """

    _STOP: Any = object()  # queue sentinel: worker exits when read

    def __init__(self, notifier: Any, *, max_pending: int = 100) -> None:
        self._notifier = notifier
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_pending)
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="omnibutler-webhook-notify",
            daemon=True)
        self._thread.start()

    def notify(self, item) -> bool:
        """Enqueue *item* for sending; True when it was accepted.

        False means the announcement was dropped (queue full or this
        notifier is closed) - noted on stderr, never raised.
        """
        if self._closed:
            print("omnibutler webhook: notifier is closed; dropping "
                  "one announcement", file=sys.stderr)
            return False
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            print("omnibutler webhook: notification queue is full; "
                  "dropping one announcement", file=sys.stderr)
            return False
        return True

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                # The wrapped notifier reports its own failures on
                # stderr; suppress() is belt-and-braces so a broken
                # notifier can never kill the worker silently-mid-queue.
                with contextlib.suppress(Exception):
                    self._notifier.notify(item)
            finally:
                self._queue.task_done()

    def close(self, timeout: float = 5.0) -> None:
        """Flush queued announcements and stop the worker (bounded)."""
        if self._closed:
            return
        self._closed = True
        deadline = time.monotonic() + max(0.0, timeout)
        while (self._queue.unfinished_tasks
               and time.monotonic() < deadline):
            time.sleep(0.01)
        remaining = max(0.0, deadline - time.monotonic())
        try:
            self._queue.put(self._STOP, timeout=remaining)
        except queue.Full:  # daemon thread: process exit reaps it
            return
        self._thread.join(timeout=remaining)


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

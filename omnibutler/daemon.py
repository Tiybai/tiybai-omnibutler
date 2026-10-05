"""Daemon mode: keep the bridge running so scenes fire on their own.

The scene engine is purely reactive - something has to feed it events.
This module is that something for a long-running bridge:

* **Clock ticks** - once per minute a ``schedule`` event is handed to the
  engine, so scenes like "22:30, wind down for the night" fire without an
  AI agent (or a human running ``tob simulate``) in the loop.
* **State polling** - every ``poll_interval`` seconds each driver's device
  states are re-read and diffed against the previous snapshot; any change
  becomes a ``state_change`` event for the engine. A device whose read
  errors is logged to the audit log and skipped - the rest of its driver
  still polls, and the daemon keeps running. Drivers are polled in
  parallel (a bounded thread pool, at most four at once); devices within
  one driver stay serial, so no driver instance is ever driven from two
  threads at once - only distinct drivers overlap. Set
  ``OMNIBUTLER_POLL_SERIAL=1`` to restore the one-driver-at-a-time loop.
* **Delayed actions** - every tick the engine is asked to run any scene
  actions whose ``delay`` pause has elapsed (see
  :meth:`SceneEngine.process_due`); nothing ever sleeps on a delay, the
  tick simply finds them due.

Safety is unchanged: the daemon only ever *feeds events* to the engine, so
high-risk actions still land in the confirmation queue (never executed
directly), and every trigger plus its outcome is written to the audit log.

Optional extras run in the same process: the human approvals web page,
macOS confirmation dialogs, a webhook announcement for newly queued
confirmations (see notify_webhook; configured via environment/config,
silent when unset), and the phone gateway (``gateway_port``) -
the latter on the runtime's own event bus and stream store, so phone
geofence events fire scenes here, not in a second process that would
double-execute them.

``run_daemon`` takes injectable ``sleep_fn`` / ``now_fn`` and a
``max_ticks`` bound so tests can drive it deterministically with a fake
clock and no real sleeping.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import signal
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from omnibutler.core.confirmations import default_state_dir
from omnibutler.core.events import Event
from omnibutler.instance_lock import InstanceLock

AGENT = "daemon"


class Daemon:
    """The event-feeding loop behind :func:`run_daemon`."""

    def __init__(
        self,
        runtime,
        *,
        poll_interval: float = 30,
        tick_seconds: float = 1,
        sleep_fn: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime.datetime] = datetime.datetime.now,
        approvals_port: int = 0,
        approvals_host: str = "127.0.0.1",
        notify: bool = False,
        gateway_port: int = 0,
        gateway_host: str = "127.0.0.1",
        webhook_notifier=None,
    ) -> None:
        self.runtime = runtime
        self.engine = runtime.engine
        self.manager = runtime.manager
        self.audit = runtime.manager.audit
        self.poll_interval = poll_interval
        self.tick_seconds = tick_seconds
        self.sleep_fn = sleep_fn
        self.now_fn = now_fn
        # Optional human-approval extras (started/stopped with the loop):
        # the out-of-band approvals web page and/or the macOS dialog
        # watcher. Both only *surface* the confirmation queue to a human;
        # neither changes the safety model.
        self.approvals_port = approvals_port
        self.approvals_host = approvals_host
        self.notify = notify
        self._approvals_httpd: ThreadingHTTPServer | None = None
        self._approvals_thread: threading.Thread | None = None
        self._watcher_thread: threading.Thread | None = None
        # Optional phone-gateway extra (started/stopped with the loop):
        # the tokened HTTP ingest/event endpoint from gateway.py, served
        # in this same process on the runtime's own bus and stream store,
        # so a geofence event and the scenes it fires never cross a
        # process boundary (two processes would double-fire scenes).
        self.gateway_port = gateway_port
        self.gateway_host = gateway_host
        self._gateway_httpd: ThreadingHTTPServer | None = None
        self._gateway_thread: threading.Thread | None = None
        self._gateway_counter: Callable[[Any], None] | None = None
        # Optional approval-webhook announcement (see notify_webhook):
        # when a notifier is configured, each newly queued confirmation
        # is POSTed to it once. ``webhook_notifier`` may be injected
        # (tests); None means "resolve from env/config at startup", and
        # an unconfigured webhook simply stays off - silently.
        self.webhook_notifier = webhook_notifier
        # Ids already announced. Memory only, by design: after a daemon
        # restart, items still pending are announced once more - a
        # duplicate phone buzz after a restart beats a high-risk action
        # sitting in the queue unannounced.
        self._notified_approvals: set[str] = set()
        # Single-instance lock (see instance_lock): one daemon per
        # state directory, or two loops would double-fire scenes. The
        # directory is the one the confirmation queue actually uses;
        # runtimes without a queue path fall back to the default.
        queue_path = getattr(getattr(runtime, "confirmations", None),
                             "path", None)
        self._state_dir = (Path(queue_path).parent if queue_path
                           else default_state_dir())
        self._instance_lock: InstanceLock | None = None

        # Drivers with a live event feed (today: Home Assistant's
        # WebSocket subscription), started/stopped with the loop. Their
        # callbacks funnel into _diff_state - the same snapshot diff the
        # poller uses - so a change seen by both paths still fires once.
        self._event_drivers: list = []
        self._stop = threading.Event()
        self._last_minute_key: str | None = None
        # (scene name, "YYYY-MM-DD HH:MM") pairs already fired - the dedupe
        # guard that keeps one scene from triggering twice in one minute.
        self._fired: set[tuple[str, str]] = set()
        self._last_poll_at: float | None = None
        self._snapshot: dict[str, dict[str, Any]] = {}
        # Serializes _diff_state end to end (snapshot read -> compare
        # -> snapshot update -> stats -> dispatch): the polling loop
        # and the HA push-callback thread both funnel state through
        # it, and an interleaved pair could double-fire a change or
        # lose one. Dispatch happens inside the lock on purpose - the
        # snapshot must not move again until this change has fully
        # propagated.
        self._diff_lock = threading.Lock()
        self.stats: dict[str, int] = {
            "ticks": 0,
            "schedule_events": 0,
            "polls": 0,
            "state_changes": 0,
            "errors": 0,
            "gateway_events": 0,
            "gateway_errors": 0,
            "webhook_notifications": 0,
            "delayed_actions": 0,
        }

    # -- control ---------------------------------------------------------
    def stop(self) -> None:
        """Ask the loop to exit at the next tick boundary."""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # -- main loop ---------------------------------------------------------
    def run(self, max_ticks: int | None = None) -> dict[str, int]:
        lock = InstanceLock(self._state_dir)
        # Refuses (DaemonAlreadyRunning, a RuntimeError naming the
        # holder's pid) when a live daemon already owns this state
        # directory; takes over a stale lock with a note on stderr.
        lock.acquire()
        self._instance_lock = lock
        previous_handlers = self._install_signal_handlers()
        self.audit.record(
            AGENT, "daemon", "daemon:start",
            {"poll_interval": self.poll_interval, "tick_seconds": self.tick_seconds},
            result={"scenes": sorted(self.engine.scenes)}, ok=True,
        )
        try:
            self._start_extras()
            while not self._stop.is_set():
                if max_ticks is not None and self.stats["ticks"] >= max_ticks:
                    break
                now = self.now_fn()
                self._maybe_fire_schedule(now)
                self._maybe_poll(now)
                self._maybe_notify_approvals()
                self._maybe_process_delayed()
                self.stats["ticks"] += 1
                if max_ticks is not None and self.stats["ticks"] >= max_ticks:
                    break
                if self._stop.is_set():
                    break
                self.sleep_fn(self.tick_seconds)
        finally:
            self._stop_extras()
            self._restore_signal_handlers(previous_handlers)
            self.audit.record(
                AGENT, "daemon", "daemon:stop", {},
                result=dict(self.stats), ok=True,
            )
            lock.release()
            self._instance_lock = None
        return dict(self.stats)

    # -- human-approval extras (approvals web page / macOS dialogs) ----------
    def _start_extras(self) -> None:
        self._start_event_subscriptions()
        self._start_gateway_extra()
        self._start_webhook_extra()
        confirmations = self.runtime.confirmations
        if self.approvals_port > 0:
            from omnibutler.approvals_web import create_http_server

            self._approvals_httpd = create_http_server(
                self.engine, confirmations, self.manager,
                host=self.approvals_host, port=self.approvals_port,
            )
            self._approvals_thread = threading.Thread(
                target=self._approvals_httpd.serve_forever,
                name="omnibutler-approvals-web", daemon=True,
            )
            self._approvals_thread.start()
            self.audit.record(
                AGENT, "daemon", "daemon:approvals_web", {},
                result={"host": self.approvals_host,
                        "port": self._approvals_httpd.server_address[1]},
                ok=True,
            )
        if self.notify:
            from omnibutler import notify_macos

            if not notify_macos.is_supported():
                # Asked for, but impossible here: say so loudly in the
                # audit log (the CLI prints the same notice) and keep
                # running - the queue and the web page still work.
                self.audit.record(
                    AGENT, "daemon", "daemon:notify_unavailable",
                    {"requested": True},
                    result={"reason": "macOS dialogs need darwin"},
                    ok=False,
                )
            else:
                notify_fn = notify_macos.macos_dialog_notifier(
                    self.engine, confirmations, self.manager)
                watcher = notify_macos.PendingWatcher(
                    confirmations, notify_fn)
                self._watcher_thread = threading.Thread(
                    target=watcher.run_forever, args=(self._stop,),
                    name="omnibutler-notify-watcher", daemon=True,
                )
                self._watcher_thread.start()
                self.audit.record(
                    AGENT, "daemon", "daemon:notify_started", {}, ok=True,
                )

    # -- driver event subscriptions (near-real-time state feeds) -------------
    def _start_event_subscriptions(self) -> None:
        """Start live state feeds on the drivers that offer one.

        Duck-typed on purpose: only the Home Assistant driver has
        ``start_event_subscription`` today. A driver whose feed cannot
        start (library missing, feed switched off) answers False and the
        poll simply remains the only state source - no error, no fuss.
        Started drivers are remembered so _stop_extras can shut their
        feeds down with the loop.
        """
        for name, driver in self.manager.drivers.items():
            starter = getattr(driver, "start_event_subscription", None)
            if starter is None:
                continue
            try:
                started = starter(self._on_driver_state)
            except Exception:
                started = False  # a feed that fails must not kill the daemon
            if started:
                self._event_drivers.append(driver)
                self.audit.record(
                    AGENT, name, "daemon:event_feed_started", {}, ok=True,
                )

    def _on_driver_state(self, device_id: str, state: dict[str, Any]) -> None:
        """One pushed state update from a driver's live feed.

        Goes through the exact snapshot diff the poller uses: whichever
        path sees a change first updates the snapshot, so the other path
        diffs against the new values and stays silent - a change seen by
        both the feed and the next poll still fires only one event.
        """
        self._diff_state(device_id, state)

    # -- approval webhook extra ------------------------------------------------
    def _start_webhook_extra(self) -> None:
        if self.webhook_notifier is not None:
            return  # injected (tests) - nothing to resolve
        from omnibutler import notify_webhook

        try:
            notifier = notify_webhook.make_webhook_notifier()
        except Exception as exc:  # a broken config must not stop the daemon
            self.audit.record(
                AGENT, "daemon", "daemon:webhook_unavailable", {},
                result={"reason": str(exc)}, ok=False,
            )
            return
        if notifier is None:
            return  # unconfigured: the announcement channel is simply off
        # Send from a background queue, not the tick: one sleepy push
        # service must not stall the daemon (see notify_webhook's
        # AsyncNotifier). Injected notifiers are used as-is, unwrapped.
        self.webhook_notifier = notify_webhook.AsyncNotifier(notifier)
        # The audit entry names the feature, never the URL: webhook URLs
        # routinely embed a topic or key that acts as a credential.
        self.audit.record(
            AGENT, "daemon", "daemon:webhook_started", {}, ok=True,
        )

    def _maybe_notify_approvals(self) -> None:
        """Announce each newly queued confirmation on the webhook, once.

        Runs every tick but is a cheap no-op while no webhook is
        configured. Items are marked announced *before* sending: a
        failing webhook must not re-fire every tick (and the notifier
        itself never raises - see notify_webhook).
        """
        notifier = self.webhook_notifier
        if notifier is None:
            return
        try:
            pending = self.runtime.confirmations.pending()
        except Exception:
            return  # queue re-reads its file per call; retry next tick
        for item in pending:
            if item.id in self._notified_approvals:
                continue
            self._notified_approvals.add(item.id)
            try:
                sent = notifier.notify(item)
            except Exception:
                sent = False
            if sent:
                self.stats["webhook_notifications"] += 1

    # -- phone gateway extra ---------------------------------------------------
    def _start_gateway_extra(self) -> None:
        if self.gateway_port <= 0:
            return
        from omnibutler.gateway import GatewayError, create_http_server

        streams = getattr(self.runtime, "streams", None)
        bus = getattr(self.runtime, "bus", None)
        if streams is None or bus is None:
            self._gateway_refused("runtime has no stream store / event bus")
            return
        try:
            httpd = create_http_server(
                bus, streams, host=self.gateway_host, port=self.gateway_port,
            )
        except (GatewayError, OSError) as exc:
            # No token configured (or the port is taken): refuse just
            # this extra, say why in the audit log and stats, and keep
            # the daemon itself running - scenes and polling still work.
            self._gateway_refused(str(exc))
            return
        self._gateway_httpd = httpd
        self._gateway_thread = threading.Thread(
            target=httpd.serve_forever,
            name="omnibutler-gateway", daemon=True,
        )
        self._gateway_thread.start()

        def _count_phone_event(event) -> None:
            if event.source == "phone":
                self.stats["gateway_events"] += 1

        self._gateway_counter = _count_phone_event
        bus.subscribe("*", _count_phone_event)
        self.audit.record(
            AGENT, "daemon", "daemon:gateway_started", {},
            result={"host": self.gateway_host,
                    "port": httpd.server_address[1]},
            ok=True,
        )

    def _gateway_refused(self, reason: str) -> None:
        self.stats["gateway_errors"] += 1
        self.audit.record(
            AGENT, "daemon", "daemon:gateway_error",
            {"host": self.gateway_host, "port": self.gateway_port},
            result={"reason": reason},
            ok=False, error=reason,
        )

    def _stop_extras(self) -> None:
        # Flush/close the webhook notifier first (it is an AsyncNotifier
        # when the daemon resolved it itself): a bounded wait, so queued
        # announcements get their chance without stalling shutdown.
        close = getattr(self.webhook_notifier, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()
        for driver in self._event_drivers:
            with contextlib.suppress(Exception):
                driver.stop_event_subscription()
        self._event_drivers = []
        if self._gateway_httpd is not None:
            self._gateway_httpd.shutdown()
            self._gateway_httpd.server_close()
            self._gateway_httpd = None
        if self._gateway_counter is not None:
            bus = getattr(self.runtime, "bus", None)
            if bus is not None:
                bus.unsubscribe("*", self._gateway_counter)
            self._gateway_counter = None
        if self._gateway_thread is not None:
            self._gateway_thread.join(timeout=5)
            self._gateway_thread = None
        if self._approvals_httpd is not None:
            self._approvals_httpd.shutdown()
            self._approvals_httpd.server_close()
            self._approvals_httpd = None
        # The watcher thread exits on its own once _stop is set (or at
        # process exit - it is a daemon thread).

    # -- schedule ticks ----------------------------------------------------
    def _maybe_fire_schedule(self, now: datetime.datetime) -> None:
        sessions = getattr(self.runtime, "sessions", None)
        if sessions is not None:
            sessions.expire_idle()
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        if minute_key == self._last_minute_key:
            return
        self._last_minute_key = minute_key
        # Drop dedupe entries from earlier minutes; the set only ever needs
        # to remember the current minute.
        self._fired = {pair for pair in self._fired if pair[1] == minute_key}
        event = Event(
            type="schedule", source=AGENT,
            data={"time": now.strftime("%H:%M"), "minute": now.minute},
        )
        report = self._dispatch(event)
        if report is None:
            return
        self.stats["schedule_events"] += 1
        for name in report.evaluated:
            self._fired.add((name, minute_key))

    # -- delayed scene actions -------------------------------------------------
    def _maybe_process_delayed(self) -> None:
        """Run delayed scene actions whose pause has elapsed.

        Runs every tick and is a cheap no-op while nothing is pending.
        The outcomes are audited like a dispatched event's; a failure is
        counted and logged, never raised into the loop.
        """
        try:
            report = self.engine.process_due()
        except Exception as exc:
            self.stats["errors"] += 1
            self.audit.record(
                AGENT, "daemon", "daemon:delayed_error", {},
                ok=False, error=str(exc),
            )
            return
        if not report.outcomes:
            return
        self.stats["delayed_actions"] += len(report.outcomes)
        self.audit.record(
            AGENT, "daemon", "daemon:delayed",
            {"event": "delay"},
            result={
                "scenes": sorted({o.scene for o in report.outcomes}),
                "executed": len(report.executed),
                "queued": len(report.queued),
                "failed": len([o for o in report.outcomes
                               if o.status == "failed"]),
            },
            ok=True,
        )

    # -- state polling -------------------------------------------------------
    def _maybe_poll(self, now: datetime.datetime) -> None:
        ts = now.timestamp()
        if self._last_poll_at is not None and ts - self._last_poll_at < self.poll_interval:
            return
        self._last_poll_at = ts
        self.stats["polls"] += 1
        names = list(self.manager.drivers)
        serial = os.environ.get("OMNIBUTLER_POLL_SERIAL", "").strip() == "1"
        if len(names) > 1 and not serial:
            # Poll drivers in parallel on a bounded pool: with several
            # slow drivers (each device read is a network round-trip),
            # a serial loop makes the round as slow as the sum of all
            # drivers. Devices within a driver are still read serially
            # by _poll_driver, so no driver instance is ever used
            # concurrently - only distinct drivers overlap, which the
            # drivers' thread-safety posture allows.
            with ThreadPoolExecutor(
                max_workers=min(4, len(names)),
                thread_name_prefix="omnibutler-poll",
            ) as pool:
                futures = [
                    pool.submit(self._poll_driver_guarded, name)
                    for name in names
                ]
                for future in futures:
                    future.result()  # guarded: never raises
        else:
            for name in names:
                self._poll_driver_guarded(name)

    def _poll_driver_guarded(self, driver_name: str) -> None:
        """_poll_driver plus a last-resort net.

        Per-device read errors are already handled inside _poll_driver;
        this catches anything unexpected escaping it (a driver whose
        device listing blows up, say) so one driver can never kill the
        loop - or, in parallel mode, its fellow drivers' round.
        """
        try:
            self._poll_driver(driver_name)
        except Exception as exc:
            self.stats["errors"] += 1
            self.audit.record(
                AGENT, driver_name, "daemon:poll_error",
                {"device": "-"}, ok=False, error=str(exc),
            )

    def _poll_driver(self, driver_name: str) -> None:
        devices = [d for d in self.manager.list_devices() if d.driver == driver_name]
        for device in devices:
            try:
                state = self.manager.get_state(device.id)
            except Exception as exc:  # a sick device must not kill the daemon
                self.stats["errors"] += 1
                self.audit.record(
                    AGENT, driver_name, "daemon:poll_error",
                    {"device": device.id}, ok=False, error=str(exc),
                )
                # Skip just this device. The driver's remaining devices
                # still get their round - one dead bulb must not blind
                # the whole driver until the next poll.
                continue
            self._diff_state(device.id, state)

    def _diff_state(self, device_id: str, state: dict[str, Any]) -> None:
        with self._diff_lock:
            previous = self._snapshot.get(device_id)
            self._snapshot[device_id] = dict(state)
            if previous is None:
                return  # first sighting is the baseline, not a change
            for key in sorted(state):
                if key in previous and previous[key] == state[key]:
                    continue
                self.stats["state_changes"] += 1
                event = Event(
                    type="state_change", source=AGENT,
                    data={"device": device_id, "property": key,
                          "value": state[key], "old_value": previous.get(key)},
                )
                self._dispatch(event)

    # -- shared --------------------------------------------------------------
    def _dispatch(self, event: Event):
        """Hand one event to the engine; audit the outcome; never raise."""
        try:
            report = self.engine.handle_event(event)
        except Exception as exc:
            self.stats["errors"] += 1
            self.audit.record(
                AGENT, "daemon", "daemon:event_error",
                {"event": event.type, "data": event.data},
                ok=False, error=str(exc),
            )
            return None
        self.audit.record(
            AGENT, "daemon", "daemon:event",
            {"event": event.type, "data": event.data},
            result={
                "evaluated": report.evaluated,
                "executed": len(report.executed),
                "queued": len(report.queued),
                "failed": len([o for o in report.outcomes if o.status == "failed"]),
                "skipped": report.skipped,
            },
            ok=True,
        )
        return report

    # -- signals ---------------------------------------------------------------
    def _install_signal_handlers(self) -> dict[int, Any]:
        """SIGINT/SIGTERM set the stop flag; only possible on the main thread."""
        previous: dict[int, Any] = {}
        if threading.current_thread() is not threading.main_thread():
            return previous

        def _handler(signum, frame):  # noqa: ANN001 - signal module convention
            self.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ValueError, OSError):
                previous[sig] = signal.signal(sig, _handler)
        return previous

    def _restore_signal_handlers(self, previous: dict[int, Any]) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)


def run_daemon(
    runtime,
    *,
    poll_interval: float = 30,
    tick_seconds: float = 1,
    max_ticks: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime.datetime] = datetime.datetime.now,
    approvals_port: int = 0,
    approvals_host: str = "127.0.0.1",
    notify: bool = False,
    gateway_port: int = 0,
    gateway_host: str = "127.0.0.1",
    webhook_notifier=None,
) -> dict[str, int]:
    """Run the bridge daemon until SIGINT/SIGTERM (or ``max_ticks`` ticks).

    Returns a stats dict: ticks run, schedule events fired, polls done,
    state changes forwarded to the engine, and errors survived.

    ``approvals_port`` > 0 additionally serves the human approvals web
    page (see approvals_web; needs $OMNIBUTLER_APPROVALS_TOKEN) in the
    same process. ``notify`` enables macOS dialog popups for newly queued
    confirmations; on other platforms it is reported and skipped.
    ``gateway_port`` > 0 additionally serves the phone gateway (see
    gateway; needs $OMNIBUTLER_GATEWAY_TOKEN) in the same process, on
    the runtime's own bus and stream store; a missing token refuses
    only the gateway, never the daemon. When an approval webhook is
    configured ($OMNIBUTLER_NOTIFY_WEBHOOK_URL or the config file's
    ``notify.webhook_url``), newly queued confirmations are announced
    there once each; ``webhook_notifier`` injects one directly (tests).
    """
    daemon = Daemon(
        runtime,
        poll_interval=poll_interval,
        tick_seconds=tick_seconds,
        sleep_fn=sleep_fn,
        now_fn=now_fn,
        approvals_port=approvals_port,
        approvals_host=approvals_host,
        notify=notify,
        gateway_port=gateway_port,
        gateway_host=gateway_host,
        webhook_notifier=webhook_notifier,
    )
    return daemon.run(max_ticks=max_ticks)

"""Daemon mode: keep the bridge running so scenes fire on their own.

The scene engine is purely reactive - something has to feed it events.
This module is that something for a long-running bridge:

* **Clock ticks** - once per minute a ``schedule`` event is handed to the
  engine, so scenes like "22:30, wind down for the night" fire without an
  AI agent (or a human running ``tob simulate``) in the loop.
* **State polling** - every ``poll_interval`` seconds each driver's device
  states are re-read and diffed against the previous snapshot; any change
  becomes a ``state_change`` event for the engine. A driver that errors is
  logged to the audit log and skipped - the daemon keeps running.

Safety is unchanged: the daemon only ever *feeds events* to the engine, so
high-risk actions still land in the confirmation queue (never executed
directly), and every trigger plus its outcome is written to the audit log.

``run_daemon`` takes injectable ``sleep_fn`` / ``now_fn`` and a
``max_ticks`` bound so tests can drive it deterministically with a fake
clock and no real sleeping.
"""

from __future__ import annotations

import datetime
import signal
import threading
import time
from typing import Any, Callable

from omnibutler.core.events import Event

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
        self._approvals_httpd = None
        self._approvals_thread: threading.Thread | None = None
        self._watcher_thread: threading.Thread | None = None

        self._stop = threading.Event()
        self._last_minute_key: str | None = None
        # (scene name, "YYYY-MM-DD HH:MM") pairs already fired - the dedupe
        # guard that keeps one scene from triggering twice in one minute.
        self._fired: set[tuple[str, str]] = set()
        self._last_poll_at: float | None = None
        self._snapshot: dict[str, dict[str, Any]] = {}
        self.stats: dict[str, int] = {
            "ticks": 0,
            "schedule_events": 0,
            "polls": 0,
            "state_changes": 0,
            "errors": 0,
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
        return dict(self.stats)

    # -- human-approval extras (approvals web page / macOS dialogs) ----------
    def _start_extras(self) -> None:
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

    def _stop_extras(self) -> None:
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

    # -- state polling -------------------------------------------------------
    def _maybe_poll(self, now: datetime.datetime) -> None:
        ts = now.timestamp()
        if self._last_poll_at is not None and ts - self._last_poll_at < self.poll_interval:
            return
        self._last_poll_at = ts
        self.stats["polls"] += 1
        for driver_name in list(self.manager.drivers):
            self._poll_driver(driver_name)

    def _poll_driver(self, driver_name: str) -> None:
        devices = [d for d in self.manager.list_devices() if d.driver == driver_name]
        for device in devices:
            try:
                state = self.manager.get_state(device.id)
            except Exception as exc:  # a sick driver must not kill the daemon
                self.stats["errors"] += 1
                self.audit.record(
                    AGENT, driver_name, "daemon:poll_error",
                    {"device": device.id}, ok=False, error=str(exc),
                )
                return  # skip the rest of this driver until the next poll
            self._diff_state(device.id, state)

    def _diff_state(self, device_id: str, state: dict[str, Any]) -> None:
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
            try:
                previous[sig] = signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass
        return previous

    def _restore_signal_handlers(self, previous: dict[int, Any]) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


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
) -> dict[str, int]:
    """Run the bridge daemon until SIGINT/SIGTERM (or ``max_ticks`` ticks).

    Returns a stats dict: ticks run, schedule events fired, polls done,
    state changes forwarded to the engine, and errors survived.

    ``approvals_port`` > 0 additionally serves the human approvals web
    page (see approvals_web; needs $OMNIBUTLER_APPROVALS_TOKEN) in the
    same process. ``notify`` enables macOS dialog popups for newly queued
    confirmations; on other platforms it is reported and skipped.
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
    )
    return daemon.run(max_ticks=max_ticks)

"""macOS dialog approvals: the second human-only, out-of-band path.

When the bridge runs on a Mac (``tob run --notify``), a native dialog is
the most direct way to reach the human: a high-risk action parks in the
confirmation queue, a dialog pops up on the Mac itself, and the person
sitting there clicks 「批准执行」 or 「拒绝」. Like the approvals web page,
this path is *not* reachable through MCP and no agent can trigger or
answer the dialog - it is answered by a physical click on the host.

Two pieces:

* :class:`PendingWatcher` - polls the (persisted, shared) confirmation
  queue and calls a ``notify_fn`` once per *newly appeared* pending item.
  The first poll after startup only builds a baseline (items already
  waiting when the process started do not pop dialogs), mirroring the
  daemon's state-polling baseline policy.
* :func:`macos_dialog_notifier` - builds a ``notify_fn`` that shows the
  item in an ``osascript`` ``display dialog``. Approving goes through the
  exact same engine + audit path as ``tob confirm`` (see
  approvals_web.apply_human_decision), recorded under agent
  ``macos:human``. If the dialog gives up (120 s, nobody at the Mac) the
  item simply stays pending - silence never approves anything.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from typing import Callable

from omnibutler.approvals_web import apply_human_decision, describe_item

logger = logging.getLogger(__name__)

AGENT = "macos:human"
APPROVE_BUTTON = "批准执行"
REJECT_BUTTON = "拒绝"
DIALOG_TIMEOUT_SECONDS = 120


class NotSupportedError(RuntimeError):
    """Raised when macOS dialog notifications are requested off macOS."""


def is_supported() -> bool:
    """True when native dialogs can be shown (macOS only)."""
    return sys.platform == "darwin"


# -- watcher -------------------------------------------------------------------

class PendingWatcher:
    """Call ``notify_fn(item)`` once for each newly pending confirmation.

    ``interval`` is the default poll cadence used by :meth:`run_forever`;
    tests drive :meth:`poll_once` directly.
    """

    def __init__(
        self,
        confirmations,
        notify_fn: Callable,
        interval: float = 5.0,
    ) -> None:
        self.confirmations = confirmations
        self.notify_fn = notify_fn
        self.interval = interval
        # None until the first poll establishes the baseline: ids already
        # pending at startup are recorded silently, never notified.
        self._seen: set[str] | None = None

    def poll_once(self) -> list:
        """Poll the queue once; notify for new items; return those items.

        A notify_fn that raises does not break the watcher (the item is
        still marked seen - a broken notifier must not spam every poll).
        """
        pending = self.confirmations.pending()
        current_ids = {item.id for item in pending}
        if self._seen is None:
            self._seen = current_ids
            return []
        new_items = [item for item in pending if item.id not in self._seen]
        self._seen = current_ids
        for item in new_items:
            try:
                self.notify_fn(item)
            except Exception:
                logger.exception("notify_fn failed for %s", item.id)
        return new_items

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        """Poll until *stop_event* is set (blocking; run in a thread)."""
        stop = stop_event if stop_event is not None else threading.Event()
        while not stop.is_set():
            self.poll_once()
            stop.wait(self.interval)


# -- osascript dialog ------------------------------------------------------------

def escape_applescript(text: str) -> str:
    """Escape *text* for inclusion inside an AppleScript string literal.

    Backslashes first (so the quote escapes we add are not themselves
    escaped), then double quotes. Newlines become spaces: a raw line
    break inside the literal is legal AppleScript but makes the script
    fragile to build and to test, and a dialog reads fine without them.
    """
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
    )


def build_dialog_script(item) -> str:
    """Build the osascript source that shows *item* and asks the human."""
    origin = item.scene or item.requested_by or "-"
    message = (
        f"高风险动作等待批准：{describe_item(item)}"
        f"  设备：{item.device_id}  风险：{item.risk}  来源：{origin}"
        f"  编号：{item.id}"
    )
    # Default button is 拒绝 (reject) on purpose: the safe action must be
    # the one a reflexive Enter / accidental keypress lands on. Approving
    # a high-risk action should always be a deliberate click, never the
    # path of least resistance. (The dialog also gives up after 120 s,
    # which leaves the item pending - inaction never approves.)
    return (
        f'display dialog "{escape_applescript(message)}" '
        f'with title "Tiybai OmniButler 待确认" '
        f'buttons {{"{REJECT_BUTTON}", "{APPROVE_BUTTON}"}} '
        f'default button "{REJECT_BUTTON}" '
        f"giving up after {DIALOG_TIMEOUT_SECONDS}"
    )


def macos_dialog_notifier(
    engine,
    confirmations,
    manager,
    *,
    runner: Callable = subprocess.run,
    platform: str | None = None,
):
    """Return a ``notify_fn(item)`` that asks via a native macOS dialog.

    The returned callable shows the dialog (blocking until the human
    clicks or it gives up after 120 s) and resolves the item:

    * 批准执行 -> ``engine.confirm`` + audit under ``macos:human``,
      returns ``"approved"`` (or ``"failed"`` if execution failed)
    * 拒绝 -> ``engine.reject`` + audit under ``macos:human``,
      returns ``"rejected"``
    * gave up / osascript error / timeout -> nothing happens, the item
      stays pending, returns ``None``

    ``runner`` is the subprocess.run-compatible callable used to invoke
    osascript (injectable for tests). Raises NotSupportedError when the
    effective platform is not macOS.
    """
    effective_platform = sys.platform if platform is None else platform
    if effective_platform != "darwin":
        raise NotSupportedError(
            f"macOS dialog notifications need macOS (darwin); "
            f"this platform is {effective_platform!r}"
        )

    def notify(item) -> str | None:
        script = build_dialog_script(item)
        try:
            result = runner(
                ["osascript", "-e", script],
                capture_output=True, text=True,
                timeout=DIALOG_TIMEOUT_SECONDS + 60,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("osascript dialog unavailable for %s", item.id)
            return None
        stdout = getattr(result, "stdout", "") or ""
        if getattr(result, "returncode", 0) != 0 or "gave up:true" in stdout:
            return None  # nobody answered; the item stays pending
        if f"button returned:{APPROVE_BUTTON}" in stdout:
            ok, _message = apply_human_decision(
                engine, confirmations, manager, item.id,
                approve=True, agent=AGENT,
            )
            return "approved" if ok else "failed"
        if f"button returned:{REJECT_BUTTON}" in stdout:
            ok, _message = apply_human_decision(
                engine, confirmations, manager, item.id,
                approve=False, agent=AGENT,
            )
            return "rejected" if ok else "failed"
        return None

    return notify

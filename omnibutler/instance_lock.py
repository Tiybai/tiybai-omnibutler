"""Single-instance lock for the daemon.

Two daemons sharing one state directory would double-fire every
schedule scene and fight over the confirmation queue, so ``tob run``
takes an exclusive lock before it starts: a small file ``daemon.lock``
in the state directory holding the owner's pid and a random token.

* If the lock is held by a *live* process, startup refuses with an
  error naming that pid and the lock path - never a silent takeover.
* If the recorded pid is gone (crash, kill -9, reboot), the lock is
  stale: the new daemon takes it over and says so on stderr.
* Normal shutdown removes the file - but only when the token still
  matches, so a daemon never deletes a successor's lock.

Liveness is probed per platform. On POSIX, ``os.kill(pid, 0)``.
On Windows that probe cannot be trusted (some CPython builds treat
``os.kill`` differently), so the probe goes through Win32 directly -
``OpenProcess`` + ``GetExitCodeProcess`` via ctypes. Whenever a probe
cannot even be performed, the pid is treated as alive: a refused
start is recoverable, two daemons on one queue are not. Everything
is stdlib; no file locking APIs, so nothing here can block or crash
on a platform quirk.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

#: Name of the lock file inside the state directory.
LOCK_FILENAME = "daemon.lock"


class DaemonAlreadyRunning(RuntimeError):
    """A live daemon already holds this state directory's lock."""


#: Win32 access right that only *queries* a process, never controls it.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
#: GetExitCodeProcess value for a process that is still running.
_STILL_ACTIVE = 259
#: OpenProcess error for "the process exists but is not ours to query".
_ERROR_ACCESS_DENIED = 5


def _pid_alive_windows(pid: int) -> bool:
    """Probe ``pid`` through Win32; unprobeable means alive (see above)."""
    try:
        import ctypes

        # typeshed only declares WinDLL / get_last_error when checking
        # *on* Windows, hence the targeted ignores for the Linux-run gate.
        kernel32 = ctypes.WinDLL(  # type: ignore[attr-defined]
            "kernel32", use_last_error=True)
    except Exception:  # no ctypes / no kernel32 handle: cannot probe
        return True
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied still proves the process exists; other errors
        # (invalid parameter, not found) mean there is no such pid.
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED  # type: ignore[attr-defined]
    try:
        code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """Best-effort liveness probe for ``pid``; see the module docstring."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by another user
    except OSError:
        return False  # not expected on POSIX; nothing there to take over
    return True


class InstanceLock:
    """The daemon lock for one state directory (see module docstring)."""

    def __init__(self, state_dir: str | Path) -> None:
        self.dir = Path(state_dir)
        self.path = self.dir / LOCK_FILENAME
        self._token: str | None = None

    # -- inspection ------------------------------------------------------
    def _read_owner(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def owner_pid(self) -> int | None:
        owner = self._read_owner()
        if owner is None:
            return None
        pid = owner.get("pid")
        return pid if isinstance(pid, int) and not isinstance(pid, bool) else None

    # -- lifecycle ---------------------------------------------------------
    def acquire(self) -> None:
        """Take the lock, or raise :class:`DaemonAlreadyRunning`.

        A stale lock (dead or unreadable owner) is taken over with a
        note on stderr. Creation is atomic (``O_EXCL``), so two
        daemons starting in the same moment cannot both win.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        payload = json.dumps({
            "pid": os.getpid(),
            "token": token,
            "started_at": time.time(),
        })
        for _attempt in range(3):
            try:
                fd = os.open(self.path,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                pid = self.owner_pid()
                if pid is not None and pid_alive(pid):
                    raise DaemonAlreadyRunning(
                        f"another omnibutler daemon is already running "
                        f"(pid {pid}); lock file: {self.path} - stop that "
                        f"daemon first, or remove the lock file yourself "
                        f"if it is wrong") from None
                print(f"omnibutler: taking over stale daemon lock "
                      f"{self.path} (previous pid {pid})", file=sys.stderr)
                with contextlib.suppress(OSError):
                    self.path.unlink()
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            self._token = token
            return
        raise DaemonAlreadyRunning(
            f"could not acquire the daemon lock at {self.path}: "
            f"it keeps reappearing - is another daemon racing to start?")

    def release(self) -> None:
        """Drop the lock if (and only if) it is still ours."""
        if self._token is None:
            return
        token, self._token = self._token, None
        owner = self._read_owner()
        if owner is not None and owner.get("token") == token:
            with contextlib.suppress(OSError):
                self.path.unlink()

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()

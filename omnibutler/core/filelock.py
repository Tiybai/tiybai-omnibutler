"""A small cross-process file lock (stdlib only).

The confirmation queue and the config file are both shared between
processes (daemon, CLI, MCP server) that may write at the same moment.
A ``threading.Lock`` cannot help across processes, so critical
sections are additionally wrapped in one of these: an OS-level lock on
a dedicated lock file sitting next to the data file.

Implementation is native-first:

* POSIX - ``fcntl.flock`` on the lock file's descriptor.
* Windows - ``msvcrt.locking`` on byte 0 of the lock file.
* Anything else - an ``O_CREAT | O_EXCL`` spin lock on the lock file
  itself (the file's existence *is* the lock).

Native locks are released by the OS if the holder dies, so a crashed
writer can never wedge the queue forever; the fallback's spin file can
go stale in that case, which is why it is only a fallback. Acquisition
is a bounded spin: after ``timeout`` seconds a :class:`FileLockTimeout`
is raised rather than blocking a caller (say, a human at ``tob
confirm``) forever on a wedged peer.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import time
from pathlib import Path

# Backend selection is probed, not imported at module level: neither
# fcntl (POSIX) nor msvcrt (Windows) exists on the other platform, and
# the lazy imports inside the methods keep the module importable - and
# type-checkable - everywhere.
_FCNTL_AVAILABLE = importlib.util.find_spec("fcntl") is not None
_MSVCRT_AVAILABLE = importlib.util.find_spec("msvcrt") is not None


class FileLockTimeout(TimeoutError):
    """The lock was still held by someone else after the timeout."""


class FileLock:
    """Exclusive lock on ``path``, held between acquire and release.

    The lock file itself is created (parent directories included) and
    left in place afterwards - deleting it on release would race with
    a peer that has just opened it. Use as a context manager::

        with FileLock(queue_path.with_suffix(".lock")):
            ...
    """

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 10.0,
        poll_interval: float = 0.025,
    ) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._fd: int | None = None
        self._fallback_held = False

    # -- acquisition ------------------------------------------------------
    def acquire(self) -> FileLock:
        if self._fd is not None or self._fallback_held:
            return self  # this instance already holds it
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        if _FCNTL_AVAILABLE or _MSVCRT_AVAILABLE:
            self._acquire_native(deadline)
        else:  # pragma: no cover - neither fcntl nor msvcrt exists
            self._acquire_fallback(deadline)
        return self

    def _acquire_native(self, deadline: float) -> None:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            while True:
                try:
                    if _FCNTL_AVAILABLE:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        self._lock_windows(fd)
                    self._fd = fd
                    return
                except OSError:
                    if time.monotonic() >= deadline:
                        raise FileLockTimeout(
                            f"timed out after {self.timeout:.1f}s waiting "
                            f"for lock {self.path} (held by another process)"
                        ) from None
                    time.sleep(self.poll_interval)
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _lock_windows(fd: int) -> None:
        import msvcrt

        # msvcrt locks a byte range starting at the file position, and
        # the range must exist - make sure byte 0 is really there.
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        # typeshed only declares msvcrt's locking API on Windows builds.
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]

    def _acquire_fallback(self, deadline: float) -> None:  # pragma: no cover
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise FileLockTimeout(
                        f"timed out after {self.timeout:.1f}s waiting "
                        f"for lock {self.path} (held by another process)"
                    ) from None
                time.sleep(self.poll_interval)
                continue
            os.close(fd)
            self._fallback_held = True
            return

    # -- release ------------------------------------------------------------
    def release(self) -> None:
        if self._fallback_held:  # pragma: no cover - fallback path only
            self._fallback_held = False
            with contextlib.suppress(OSError):
                os.unlink(self.path)
            return
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if _FCNTL_AVAILABLE:
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            elif _MSVCRT_AVAILABLE:
                import msvcrt

                with contextlib.suppress(OSError):
                    os.lseek(fd, 0, os.SEEK_SET)
                    # typeshed only declares this API on Windows builds.
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        finally:
            os.close(fd)

    def __enter__(self) -> FileLock:
        return self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()

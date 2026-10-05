"""Append-only JSONL audit log for every control call.

Every property change and action - whether it came from the CLI, an MCP tool
call, or a scene - is recorded with who asked, what was requested, and what
happened. The log is local-only by design; see docs/security.md.

The log file rotates by size so a long-lived install on a small disk (the
reference host has 256 GB) cannot grow it without bound: before a write
that would push the file past the cap, the current file becomes
``audit.jsonl.1``, older backups shift up one number, and backups beyond
the keep count are deleted. The defaults are 10 MiB per file and 5 backups
(60 MiB worst case per log), overridable with the ``OMNIBUTLER_LOG_MAX_MB``
and ``OMNIBUTLER_LOG_KEEP`` environment variables. Rotation is
best-effort: a failure (permissions, a racing writer) is reported on
stderr and the write still goes through - losing history quietly is bad,
but a control call must never fail because its log could not rotate.

The rotation helpers live here and are shared with
:mod:`omnibutler.core.streams`, whose ``streams.jsonl`` grows the same
way; the two modules already share helpers across ``core`` siblings, and
keeping one implementation guarantees both logs rotate identically.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

#: Default per-file size cap for JSONL state files (audit, streams).
DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB

#: Default number of rotated backups kept next to the live file.
DEFAULT_KEEP = 5


def retention_from_env(
    max_bytes: int | None = None,
    keep: int | None = None,
) -> tuple[int, int]:
    """Resolve the rotation limits: explicit argument, env var, default.

    ``OMNIBUTLER_LOG_MAX_MB`` is in mebibytes (a float is accepted,
    e.g. ``0.001`` in tests); ``OMNIBUTLER_LOG_KEEP`` is an integer
    count of backups. An unset, unreadable or nonsensical value (zero
    or negative, non-numeric) silently falls back to the default - a
    typo in an env var must never crash a control call at startup.
    """
    if max_bytes is None:
        max_bytes = DEFAULT_MAX_BYTES
        raw_mb = os.environ.get("OMNIBUTLER_LOG_MAX_MB", "").strip()
        if raw_mb:
            try:
                mb = float(raw_mb)
            except ValueError:
                mb = 0.0
            if mb > 0:
                max_bytes = max(1, int(mb * 1024 * 1024))
    if keep is None:
        keep = DEFAULT_KEEP
        raw_keep = os.environ.get("OMNIBUTLER_LOG_KEEP", "").strip()
        if raw_keep:
            try:
                parsed = int(raw_keep)
            except ValueError:
                parsed = 0
            if parsed > 0:
                keep = parsed
    return max_bytes, keep


def rotated_paths(path: Path, keep: int) -> list[Path]:
    """All files making up one logical JSONL log, oldest first.

    ``[path.<keep>, ..., path.2, path.1, path]`` restricted to files
    that exist - exactly the order a reader must replay them in to see
    history in write order.
    """
    candidates = [path.with_name(f"{path.name}.{n}")
                  for n in range(keep, 0, -1)]
    candidates.append(path)
    return [p for p in candidates if p.exists()]


def rotate_if_needed(path: Path, incoming_bytes: int, max_bytes: int,
                     keep: int) -> None:
    """Rotate ``path`` if appending ``incoming_bytes`` would exceed the cap.

    ``path`` becomes ``path.1``, each older backup shifts up one number,
    and backups numbered above ``keep`` are deleted. A no-op when the
    file does not exist yet or the write still fits - except that a
    single record larger than the whole cap rotates too (the record is
    written to a fresh file rather than dropped; that file will simply
    exceed the cap until the next write). Never raises: failures are
    reported on stderr and the caller proceeds to write anyway.
    """
    try:
        if max_bytes <= 0 or keep <= 0 or not path.exists():
            return
        if path.stat().st_size + incoming_bytes <= max_bytes:
            return
        oldest = path.with_name(f"{path.name}.{keep}")
        if oldest.exists():
            oldest.unlink()
        for n in range(keep - 1, 0, -1):
            src = path.with_name(f"{path.name}.{n}")
            if src.exists():
                src.replace(path.with_name(f"{path.name}.{n + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError as exc:
        with contextlib.suppress(Exception):
            print(f"omnibutler: could not rotate {path}: {exc}",
                  file=sys.stderr)


def default_audit_path() -> Path:
    override = os.environ.get("TOB_AUDIT_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".omnibutler" / "audit.jsonl"


class AuditLog:
    def __init__(
        self,
        path: str | Path | None = None,
        enabled: bool = True,
        max_bytes: int | None = None,
        keep: int | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_audit_path()
        self.enabled = enabled
        self.max_bytes, self.keep = retention_from_env(max_bytes, keep)
        self._lock = threading.Lock()
        #: Lines skipped by the most recent read pass (see iter_entries).
        self.last_read_skipped = 0

    def record(
        self,
        agent: str,
        device_id: str,
        action: str,
        params: dict[str, Any] | None = None,
        result: Any = None,
        ok: bool = True,
        error: str | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": time.time(),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "agent": agent,
            "device": device_id,
            "action": action,
            "params": params or {},
            "ok": ok,
            "result": result,
            "error": error,
        }
        if self.enabled:
            line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                rotate_if_needed(
                    self.path, len(line.encode("utf-8")),
                    self.max_bytes, self.keep,
                )
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
        return entry

    def iter_entries(self) -> Iterator[dict[str, Any]]:
        """Stream every retained entry, oldest first, one line at a time.

        A full retained log is tens of megabytes; slurping it whole (the
        old read_all) cost hundreds of MB of RAM to answer questions
        like "the last 20 entries". This generator reads each rotated
        file line by line instead, so memory stays flat no matter how
        large the log grows.

        A line that fails to parse - the torn tail of a write cut off
        mid-line, say by a kill - is skipped and counted, the same
        tolerance streams.py applies to its file; one bad line must not
        make the whole history unreadable. The count lands in
        ``last_read_skipped`` (reset when a pass starts, final once the
        iterator is exhausted) and, when non-zero, is also announced on
        stderr at the end of the pass - damage is never silent. Files
        are decoded with ``errors="replace"`` so a write torn in the
        middle of a multi-byte character degrades to one skipped line
        instead of a UnicodeDecodeError killing the whole read.
        """
        self.last_read_skipped = 0
        skipped = 0
        for path in rotated_paths(self.path, self.keep):
            try:
                handle = path.open(encoding="utf-8", errors="replace")
            except OSError:
                continue
            with handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    entry = self._parse_line(line)
                    if entry is None:
                        skipped += 1
                        continue
                    yield entry
        self.last_read_skipped = skipped
        self._report_skipped(skipped)

    @staticmethod
    def _parse_line(line: str) -> dict[str, Any] | None:
        """One log line -> its entry dict, or None when the line is torn."""
        try:
            entry = json.loads(line)
        except ValueError:
            return None
        return entry if isinstance(entry, dict) else None

    def _report_skipped(self, skipped: int) -> None:
        if skipped:
            print(
                f"omnibutler: audit log {self.path}: skipped {skipped} "
                "unparseable line(s) (torn write?)",
                file=sys.stderr,
            )

    @staticmethod
    def _count_lines(path: Path) -> int:
        """Line count of one file, counted raw without decoding it."""
        try:
            count = 0
            last_byte = b"\n"
            with path.open("rb") as fh:
                while chunk := fh.read(1 << 20):
                    count += chunk.count(b"\n")
                    last_byte = chunk[-1:]
            if last_byte != b"\n":
                count += 1  # a final line missing its newline (torn tail)
            return count
        except OSError:
            return 0

    def tail(self, count: int) -> tuple[list[dict[str, Any]], int]:
        """The newest ``count`` entries (oldest first) and the log's size.

        Built for "show me the last few" readers: the retained files
        are walked newest-first and parsing stops as soon as ``count``
        entries are collected, so the cost tracks ``count`` - not the
        tens of megabytes of history in front of it. Files beyond the
        collection point are not even decoded, only line-counted. The
        second return value is that total retained line count; it
        equals the entry count on any log whose lines all parse, which
        is every line record() finished writing. Torn lines met while
        collecting the tail are skipped and reported exactly as in
        :meth:`iter_entries`.
        """
        self.last_read_skipped = 0
        collected: deque[dict[str, Any]] = deque(maxlen=max(count, 0))
        skipped = 0
        total = 0
        paths = rotated_paths(self.path, self.keep)
        for path in reversed(paths):
            if len(collected) >= count:
                total += self._count_lines(path)
                continue
            try:
                lines = path.read_text(
                    encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            total += len(lines)
            # This file may hold part of the tail: parse from its end.
            # (Files are size-capped, so holding one file's lines is
            # bounded by the cap, never by the whole log.)
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                entry = self._parse_line(line)
                if entry is None:
                    skipped += 1
                    continue
                collected.appendleft(entry)
                if len(collected) >= count:
                    break
        self.last_read_skipped = skipped
        self._report_skipped(skipped)
        return list(collected), total

    def read_all(self) -> list[dict[str, Any]]:
        """Every retained entry, oldest first, spanning rotated backups.

        Kept for callers that genuinely need the whole history as a
        list; it is exactly ``list(iter_entries())``, torn lines
        included in the skip count. New readers that only need a tail
        or a filtered pass should iterate instead of materialising.
        """
        return list(self.iter_entries())

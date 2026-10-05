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

import json
import os
import sys
import threading
import time
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
        try:
            print(f"omnibutler: could not rotate {path}: {exc}",
                  file=sys.stderr)
        except Exception:
            pass


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

    def read_all(self) -> list[dict[str, Any]]:
        """Every retained entry, oldest first, spanning rotated backups."""
        entries = []
        for path in rotated_paths(self.path, self.keep):
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

"""Confirmation queue for high-risk actions.

High-risk actions (garage doors, locks, gas valves) are never executed
directly by scenes or agents. They are parked here until a human confirms
them *on the host* (``tob confirm <id>``), which is the bridge's main
safety guardrail. Agents - including the MCP server - can only read the
queue; there is deliberately no programmatic approve path.

The queue is persisted to ``confirmations.json`` in the state directory
(``$OMNIBUTLER_STATE_DIR``, default ``~/.omnibutler``) so pending items
survive restarts and are shared between processes (MCP server, CLI,
daemon). Writes are atomic (temp file + rename) and the file is kept at
mode 0600.

Because several processes share the file, every mutation runs inside a
critical section: an in-process ``threading.Lock`` plus a cross-process
file lock (see :mod:`omnibutler.core.filelock`) around the whole
re-read -> modify -> save sequence. Ids are minted inside that section,
and :meth:`ConfirmationQueue.claim` moves an item out of ``pending``
atomically, so two approvers racing on the same item cannot both win -
the action behind an item can execute at most once. Reads re-read the
file, but skip the parse when its mtime+size signature is unchanged
(the daemon polls :meth:`pending` every second).
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnibutler.core.filelock import FileLock
from omnibutler.instance_lock import pid_alive

#: How long a *resolved* entry (confirmed / rejected / ...) is kept in
#: the queue file after creation before a save prunes it. Pending
#: entries are never pruned - a parked high-risk action waits for its
#: human for as long as it takes.
TERMINAL_RETENTION_SECONDS = 30 * 24 * 60 * 60

#: Budget for acquiring the cross-process queue lock. A mutation that
#: cannot get the lock within this long fails loudly instead of
#: blocking a human (or the daemon) on a wedged peer forever.
LOCK_TIMEOUT_SECONDS = 10.0


def default_state_dir() -> Path:
    override = os.environ.get("OMNIBUTLER_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".omnibutler"


@dataclass
class PendingConfirmation:
    id: str
    device_id: str
    kind: str  # "set_property" | "call_action"
    name: str  # property or action name
    value: Any = None
    params: dict[str, Any] = field(default_factory=dict)
    requested_by: str = ""
    scene: str | None = None
    risk: str = "high"
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | confirmed | rejected

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device": self.device_id,
            "kind": self.kind,
            "name": self.name,
            "value": self.value,
            "params": dict(self.params),
            "requested_by": self.requested_by,
            "scene": self.scene,
            "risk": self.risk,
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingConfirmation:
        return cls(
            id=str(data["id"]),
            device_id=str(data.get("device", data.get("device_id", ""))),
            kind=str(data["kind"]),
            name=str(data["name"]),
            value=data.get("value"),
            params=data.get("params") or {},
            requested_by=data.get("requested_by", ""),
            scene=data.get("scene"),
            risk=data.get("risk", "high"),
            created_at=float(data.get("created_at", time.time())),
            status=data.get("status", "pending"),
        )


class ConfirmationQueue:
    def __init__(
        self,
        state_dir: str | Path | None = None,
        path: str | Path | None = None,
    ) -> None:
        if path is not None:
            self.path = Path(path)
        elif state_dir is not None:
            self.path = Path(state_dir) / "confirmations.json"
        else:
            self.path = default_state_dir() / "confirmations.json"
        self._lock_path = self.path.with_name(f"{self.path.name}.lock")
        self._mutex = threading.Lock()
        self._items: dict[str, PendingConfirmation] = {}
        self._counter = itertools.count(1)
        # (mtime_ns, size) of the file as last loaded or saved by us;
        # _refresh skips the re-read while the signature stands.
        self._loaded_sig: tuple[int, int] | None = None
        self._loaded_once = False
        # Signature of a file already reported corrupt, so a rename that
        # failed (permissions) is not re-reported on every poll.
        self._corrupt_sig: tuple[int, int] | None = None
        self._clean_stale_tmp_files()
        self._refresh()

    # -- locking ------------------------------------------------------------
    @contextlib.contextmanager
    def _critical(self) -> Iterator[None]:
        """In-process mutex + cross-process file lock, in that order.

        Every mutation (add / mark / claim) runs its whole
        refresh -> modify -> save sequence in here, so no peer process
        can interleave a write or mint a colliding id.
        """
        with self._mutex, FileLock(self._lock_path, timeout=LOCK_TIMEOUT_SECONDS):
            yield

    # -- persistence --------------------------------------------------------
    def _stat_sig(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _refresh(self) -> None:
        """Reload from disk so other processes' changes become visible.

        Skipped while the file's mtime+size signature matches what we
        last loaded or saved - the daemon calls :meth:`pending` every
        second and an unchanged queue must cost one stat, not a parse.
        """
        sig = self._stat_sig()
        if self._loaded_once and sig == self._loaded_sig:
            return
        if sig is None:
            # No file (yet, or anymore): whatever we hold stands.
            self._loaded_once = True
            self._loaded_sig = None
            return
        if sig == self._corrupt_sig:
            return  # already quarantined/reported; do not spam stderr
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return  # lost a race with a peer's atomic replace; retry next time
        try:
            raw = json.loads(text)
        except ValueError:
            self._quarantine_corrupt(sig)
            return
        entries = raw.get("items", []) if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            self._quarantine_corrupt(sig)
            return
        items: dict[str, PendingConfirmation] = {}
        highest = 0
        for entry in entries:
            try:
                item = PendingConfirmation.from_dict(entry)
            except (KeyError, TypeError, ValueError):
                continue
            items[item.id] = item
            suffix = item.id.rsplit("-", 1)[-1]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        self._items = items
        self._counter = itertools.count(highest + 1)
        self._loaded_sig = sig
        self._loaded_once = True
        self._corrupt_sig = None

    def _quarantine_corrupt(self, sig: tuple[int, int]) -> None:
        """Move an unparseable queue file aside and start empty.

        The bad file is evidence (something - a peer, a disk, a hand
        edit - damaged the safety queue), so it is renamed to
        ``<name>.corrupt-<timestamp>-<pid>`` and kept, never deleted
        and never silently treated as "no pending items".
        """
        self._corrupt_sig = sig
        if self._stat_sig() != sig:
            return  # a peer already replaced it; next refresh re-reads
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(
            f"{self.path.name}.corrupt-{stamp}-{os.getpid()}"
        )
        try:
            os.replace(self.path, target)
        except OSError as exc:
            print(
                f"omnibutler: confirmation queue {self.path} is corrupt "
                f"and could not be moved aside ({exc}); starting with an "
                "empty queue - the corrupt file was left in place",
                file=sys.stderr,
            )
        else:
            print(
                f"omnibutler: confirmation queue {self.path} is corrupt; "
                f"moved aside to {target} and starting with an empty queue",
                file=sys.stderr,
            )
        self._items = {}
        self._counter = itertools.count(1)
        self._loaded_sig = None
        self._loaded_once = True

    def _save(self) -> None:
        # Prune long-resolved entries on the way out: the file is the
        # queue's memory, not its archive (the audit log is). Pending
        # entries are kept however old they are.
        cutoff = time.time() - TERMINAL_RETENTION_SECONDS
        self._items = {
            item_id: item
            for item_id, item in self._items.items()
            if item.status == "pending" or item.created_at >= cutoff
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "items": [item.to_dict() for item in self._items.values()],
        }
        tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
        # The queue can carry device details a stranger should not
        # read off the disk; keep it owner-only like the config file.
        # (On Windows chmod only toggles the read-only bit - harmless.)
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)
        self._loaded_sig = self._stat_sig()
        self._loaded_once = True

    def _clean_stale_tmp_files(self) -> None:
        """Remove our own crashed writers' leftover temp files.

        A save writes ``<name>.tmp-<pid>`` and renames it over the
        queue; a crash between the two leaves the temp behind. Only
        files whose pid is provably dead (and not ours) are removed -
        a live peer may be mid-save with its temp right now.
        """
        parent = self.path.parent
        if not parent.is_dir():
            return
        prefix = f"{self.path.name}.tmp-"
        try:
            entries = list(parent.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.name.startswith(prefix):
                continue
            suffix = entry.name[len(prefix):]
            if not suffix.isdigit():
                continue
            pid = int(suffix)
            if pid == os.getpid() or pid_alive(pid):
                continue
            with contextlib.suppress(OSError):
                entry.unlink()

    # -- in-memory interface (backed by the file) -----------------------------
    def add(
        self,
        device_id: str,
        kind: str,
        name: str,
        value: Any = None,
        params: dict[str, Any] | None = None,
        requested_by: str = "",
        scene: str | None = None,
        risk: str = "high",
    ) -> PendingConfirmation:
        with self._critical():
            self._refresh()
            item = PendingConfirmation(
                id=f"cfm-{next(self._counter):04d}",
                device_id=device_id,
                kind=kind,
                name=name,
                value=value,
                params=params or {},
                requested_by=requested_by,
                scene=scene,
                risk=risk,
            )
            self._items[item.id] = item
            self._save()
            return item

    def pending(self) -> list[PendingConfirmation]:
        with self._mutex:
            self._refresh()
            return [i for i in self._items.values() if i.status == "pending"]

    def all(self) -> list[PendingConfirmation]:
        with self._mutex:
            self._refresh()
            return list(self._items.values())

    def get(self, confirmation_id: str) -> PendingConfirmation | None:
        with self._mutex:
            self._refresh()
            return self._items.get(confirmation_id)

    def claim(self, confirmation_id: str, status: str = "confirmed") -> bool:
        """Atomically move a pending item to a terminal status.

        Returns True only for the caller that actually made the
        transition; anyone racing the same id - another thread, the
        CLI against the approvals page - gets False and must not act
        on the item. This is what makes "approve" safe to attempt from
        two places at once: at most one claim ever succeeds, so the
        parked action can be executed at most once.
        """
        with self._critical():
            self._refresh()
            item = self._items.get(confirmation_id)
            if item is None or item.status != "pending":
                return False
            item.status = status
            self._save()
            return True

    def mark(self, confirmation_id: str, status: str) -> PendingConfirmation | None:
        with self._critical():
            self._refresh()
            item = self._items.get(confirmation_id)
            if item is not None:
                item.status = status
                self._save()
            return item

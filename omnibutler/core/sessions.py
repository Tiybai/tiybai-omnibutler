"""Terminal sessions: the third modelling object, next to devices and
data streams.

A pair of smart glasses or a watch is not really a *device* in the switch
sense - it is a terminal the AI talks *through*: it shows text, speaks,
listens. Terminals come and go (glasses wake, a watch disconnects), so
what the bridge actually manages is a **session** with a lifecycle:

    opening -> active -> closed

Sessions are deliberately *ephemeral runtime state*: unlike the
confirmation queue they are not persisted, because a bridge restart ends
every real terminal connection anyway. What survives is the trail - every
transition is published on the event bus (``session_opened`` /
``session_closed``) and written to the audit log, mirroring how device
control calls are recorded.

This module is self-contained on purpose; wiring it into the runtime,
the MCP surface and the scene engine's trigger vocabulary is integration
work that builds on these primitives.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from omnibutler.core.audit import AuditLog
from omnibutler.core.errors import OmniButlerError
from omnibutler.core.events import EventBus

#: Kinds are free-form labels; these are the ones the architecture names.
TERMINAL_KINDS = ("glasses", "watch", "earbuds", "phone")

DEFAULT_IDLE_TIMEOUT = 300.0  # seconds without activity before auto-close


class SessionError(OmniButlerError):
    """Raised for unknown sessions and illegal lifecycle transitions."""


class SessionState(str, enum.Enum):  # noqa: UP042 - StrEnum changes str(member) output; mixin kept deliberately
    OPENING = "opening"
    ACTIVE = "active"
    CLOSED = "closed"


@dataclass
class TerminalSession:
    """One live (or finished) conversation with a terminal device."""

    id: str
    device_id: str
    kind: str
    state: SessionState = SessionState.OPENING
    started_at: float = 0.0
    last_activity: float = 0.0
    closed_at: float | None = None
    close_reason: str | None = None
    opened_by: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.state is not SessionState.CLOSED

    def idle_seconds(self, now: float) -> float:
        return max(0.0, now - self.last_activity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "kind": self.kind,
            "state": self.state.value,
            "started_at": self.started_at,
            "last_activity": self.last_activity,
            "closed_at": self.closed_at,
            "close_reason": self.close_reason,
            "opened_by": self.opened_by,
            "metadata": dict(self.metadata),
        }


class SessionManager:
    """Owns terminal sessions and their lifecycle transitions.

    Parameters mirror the rest of the core: an optional event bus and
    audit log, an injectable ``clock`` (seconds, like ``time.time``) so
    tests and the daemon control time, and ``idle_timeout`` - a session
    with no activity for longer than this is closed automatically with
    reason ``"timeout"``.
    """

    def __init__(
        self,
        bus: EventBus | None = None,
        audit: AuditLog | None = None,
        *,
        clock: Callable[[], float] = time.time,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    ) -> None:
        self.bus = bus
        self.audit = audit
        self._clock = clock
        self.idle_timeout = float(idle_timeout)
        self._sessions: dict[str, TerminalSession] = {}
        self._counter = 0

    # -- lifecycle ------------------------------------------------------

    def open_session(
        self,
        device_id: str,
        kind: str,
        *,
        agent: str = "sessions",
        metadata: dict[str, Any] | None = None,
    ) -> TerminalSession:
        """Begin a session in ``opening`` state and announce it.

        The session becomes ``active`` via :meth:`activate`, which is the
        terminal's handshake completing (glasses awake and listening).
        """
        if not device_id or not str(device_id).strip():
            raise SessionError("open_session needs a device_id")
        if not kind or not str(kind).strip():
            raise SessionError("open_session needs a terminal kind (e.g. 'glasses')")
        self._counter += 1
        now = self._clock()
        session = TerminalSession(
            id=f"ses-{self._counter:04d}",
            device_id=str(device_id),
            kind=str(kind),
            state=SessionState.OPENING,
            started_at=now,
            last_activity=now,
            opened_by=agent,
            metadata=dict(metadata or {}),
        )
        self._sessions[session.id] = session
        # Audit before emitting: the log should show the opening first,
        # then anything the announcement triggered.
        self._record(session, agent, "session:opened",
                     result={"state": session.state.value})
        self._emit("session_opened", session)
        return session

    def activate(self, session_id: str, *, agent: str | None = None) -> TerminalSession:
        session = self.get(session_id)
        if session.state is SessionState.CLOSED:
            raise SessionError(f"session {session_id} is already closed")
        session.state = SessionState.ACTIVE
        session.last_activity = self._clock()
        self._record(session, agent or session.opened_by, "session:activated",
                     result={"state": session.state.value})
        return session

    def touch(self, session_id: str) -> TerminalSession:
        """Record activity (input heard, output delivered) on a session."""
        session = self.get(session_id)
        if session.state is SessionState.CLOSED:
            raise SessionError(f"session {session_id} is already closed")
        session.last_activity = self._clock()
        return session

    def close(
        self,
        session_id: str,
        *,
        reason: str = "closed",
        agent: str | None = None,
    ) -> TerminalSession:
        session = self.get(session_id)
        if session.state is SessionState.CLOSED:
            raise SessionError(f"session {session_id} is already closed")
        session.state = SessionState.CLOSED
        session.closed_at = self._clock()
        session.close_reason = reason
        self._record(session, agent or session.opened_by, "session:closed",
                     params={"reason": reason},
                     result={"state": session.state.value})
        self._emit("session_closed", session, reason=reason)
        return session

    # -- queries ----------------------------------------------------------

    def get(self, session_id: str) -> TerminalSession:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise SessionError(f"unknown session {session_id!r}") from None

    def list_sessions(self, *, include_closed: bool = False) -> list[TerminalSession]:
        self.expire_idle()
        return [s for s in self._sessions.values()
                if include_closed or s.is_open]

    def list_active(self) -> list[TerminalSession]:
        """Sessions still open (``opening`` or ``active``), idle ones swept."""
        return self.list_sessions()

    def active_for_device(self, device_id: str) -> TerminalSession | None:
        for session in self.list_active():
            if session.device_id == device_id:
                return session
        return None

    # -- timeout sweep ----------------------------------------------------

    def expire_idle(self) -> list[TerminalSession]:
        """Close every open session idle past the timeout; return them."""
        now = self._clock()
        expired = []
        for session in list(self._sessions.values()):
            if session.is_open and session.idle_seconds(now) > self.idle_timeout:
                self.close(session.id, reason="timeout", agent="sessions")
                expired.append(session)
        return expired

    # -- plumbing ---------------------------------------------------------

    def _emit(self, event_type: str, session: TerminalSession, **extra: Any) -> None:
        if self.bus is None:
            return
        self.bus.emit(
            event_type,
            source="sessions",
            session_id=session.id,
            device_id=session.device_id,
            device=session.device_id,  # alias, matching state_change events
            kind=session.kind,
            state=session.state.value,
            **extra,
        )

    def _record(
        self,
        session: TerminalSession,
        agent: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        result: Any = None,
    ) -> None:
        if self.audit is None:
            return
        merged = {"session_id": session.id, "kind": session.kind}
        merged.update(params or {})
        self.audit.record(
            agent=agent,
            device_id=session.device_id,
            action=action,
            params=merged,
            result=result,
        )

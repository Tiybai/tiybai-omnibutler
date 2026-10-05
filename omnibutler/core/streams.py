"""Read-only data streams: the second modelling object of the bridge.

The capability model (core/models.py) covers *entities* - devices with
properties an agent can read and set. A growing class of sources does not
fit that shape at all: a phone or watch produces *time series*, not device
state. Steps, sleep stages, heart rate, the phone's own location - nobody
"sets" yesterday's step count, and the interesting questions are all
historical ("how did I sleep this week?", "when did I get home?").

This module models those as data streams:

* :class:`DataStream` - one named series: an id, a ``kind`` in a dotted
  vocabulary (``health.steps``, ``health.sleep``, ``health.heart_rate``,
  ``location``, ``presence``, ...), the ``source`` that produces it
  (``phone``, ``watch``, a gateway peer name) and an optional ``unit``.
* :class:`DataPoint` - one observation: ``(stream_id, ts, value, meta)``.
* :class:`StreamStore` - an append-only, thread-safe store with the three
  queries consumers actually need: :meth:`StreamStore.latest`,
  :meth:`StreamStore.history` and :meth:`StreamStore.query` (by stream
  and/or kind). Points are persisted as JSONL to ``streams.jsonl`` in the
  state directory (``$OMNIBUTLER_STATE_DIR``, default ``~/.omnibutler``),
  one line per point with its stream descriptor inlined, so the store
  rebuilds itself - descriptors included - by replaying the file. Writes
  append a single line, the same low-tech approach as the audit log; the
  store is meant for phone/watch-rate data, not sensor firehoses.

Streams are deliberately read-only from the bridge's point of view:
producers append, scenes/agents query. There is no set/delete API.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnibutler.core.confirmations import default_state_dir

STREAMS_FILENAME = "streams.jsonl"


@dataclass
class DataStream:
    """One named, read-only time series (e.g. the phone's step count)."""

    id: str
    kind: str  # dotted vocabulary: health.steps, health.sleep, location, ...
    source: str = ""  # producer: "phone", "watch", a gateway peer, ...
    unit: str = ""  # "count", "minutes", "bpm", ... ("" when dimensionless)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("stream id must be a non-empty string")
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("stream kind must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DataStream":
        if not isinstance(data, dict):
            raise ValueError(f"stream descriptor must be an object, got {data!r}")
        return cls(
            id=data.get("id", ""),
            kind=data.get("kind", ""),
            source=str(data.get("source") or ""),
            unit=str(data.get("unit") or ""),
        )


@dataclass
class DataPoint:
    """One observation in a stream: a value at a moment in time."""

    stream_id: str
    ts: float
    value: Any = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "ts": self.ts,
            "value": self.value,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DataPoint":
        return cls(
            stream_id=str(data["stream_id"]),
            ts=float(data["ts"]),
            value=data.get("value"),
            meta=data.get("meta") or {},
        )


class StreamStore:
    """Append-only, thread-safe, JSONL-backed store of data points."""

    def __init__(
        self,
        state_dir: str | Path | None = None,
        path: str | Path | None = None,
    ) -> None:
        if path is not None:
            self.path = Path(path)
        elif state_dir is not None:
            self.path = Path(state_dir) / STREAMS_FILENAME
        else:
            self.path = default_state_dir() / STREAMS_FILENAME
        self._lock = threading.RLock()
        self._streams: dict[str, DataStream] = {}
        self._points: dict[str, list[DataPoint]] = {}
        self._load()

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        """Rebuild descriptors and points by replaying the JSONL file."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                descriptor = record["stream"]
                point = DataPoint.from_dict(record)
            except (ValueError, KeyError, TypeError):
                continue  # a torn/corrupt line must not kill the store
            stream = self._streams.get(point.stream_id)
            if stream is None:
                try:
                    stream = DataStream.from_dict(descriptor)
                except ValueError:
                    continue
                self._streams[stream.id] = stream
            self._points.setdefault(point.stream_id, []).append(point)
        for points in self._points.values():
            points.sort(key=lambda p: p.ts)

    def _persist(self, stream: DataStream, point: DataPoint) -> None:
        record = {"stream": stream.to_dict(), **point.to_dict()}
        # Serialise first: a non-JSON value must fail before anything is
        # written or kept in memory.
        line = json.dumps(record, ensure_ascii=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # -- streams ------------------------------------------------------------
    def register(self, stream: DataStream) -> DataStream:
        """Register (or refresh) a stream descriptor. Returns the stream.

        Descriptors are persisted lazily: a descriptor reaches disk with
        the first point appended to the stream (each point line carries
        its descriptor), which is all the gateway ingest path needs.
        """
        with self._lock:
            self._streams[stream.id] = stream
            return stream

    def get_stream(self, stream_id: str) -> DataStream | None:
        with self._lock:
            return self._streams.get(stream_id)

    def streams(self, kind: str | None = None) -> list[DataStream]:
        """All registered streams, optionally filtered by exact ``kind``."""
        with self._lock:
            found = list(self._streams.values())
        if kind is not None:
            found = [s for s in found if s.kind == kind]
        return found

    # -- points ---------------------------------------------------------------
    def append(
        self,
        stream_id: str,
        value: Any,
        *,
        ts: float | None = None,
        meta: dict[str, Any] | None = None,
        stream: DataStream | None = None,
    ) -> DataPoint:
        """Append one point to a stream and persist it.

        The stream must already be registered, unless its descriptor is
        passed as ``stream=`` (which registers it first). Raises KeyError
        for an unknown stream id, ValueError when a serialisation-hostile
        value/meta is supplied (nothing is stored in that case).
        """
        point = DataPoint(
            stream_id=stream_id,
            ts=time.time() if ts is None else float(ts),
            value=value,
            meta=dict(meta) if meta else {},
        )
        with self._lock:
            if stream is not None:
                self.register(stream)
            descriptor = self._streams.get(stream_id)
            if descriptor is None:
                raise KeyError(
                    f"unknown stream {stream_id!r}: register it first "
                    f"(or pass stream=...)"
                )
            self._persist(descriptor, point)
            points = self._points.setdefault(stream_id, [])
            points.append(point)
            points.sort(key=lambda p: p.ts)
            return point

    def latest(self, stream_id: str) -> DataPoint | None:
        """The most recent point of a stream, or None when it has none."""
        with self._lock:
            points = self._points.get(stream_id)
            return points[-1] if points else None

    def history(
        self,
        stream_id: str,
        *,
        since: float | None = None,
        until: float | None = None,
    ) -> list[DataPoint]:
        """Points of one stream in ``[since, until]``, oldest first."""
        return self.query(stream_id=stream_id, since=since, until=until)

    def query(
        self,
        *,
        stream_id: str | None = None,
        kind: str | None = None,
        since: float | None = None,
        until: float | None = None,
    ) -> list[DataPoint]:
        """Points filtered by stream and/or kind and time window.

        Bounds are inclusive on both ends; results are ordered by
        timestamp (per stream when several streams match a kind query,
        merged and globally sorted).
        """
        with self._lock:
            if stream_id is not None:
                ids = [stream_id] if stream_id in self._points else []
            elif kind is not None:
                ids = [s.id for s in self._streams.values() if s.kind == kind]
            else:
                ids = list(self._points)
            found: list[DataPoint] = []
            for sid in ids:
                for point in self._points.get(sid, []):
                    if since is not None and point.ts < since:
                        continue
                    if until is not None and point.ts > until:
                        continue
                    found.append(point)
            found.sort(key=lambda p: p.ts)
            return found

    def __len__(self) -> int:
        with self._lock:
            return sum(len(points) for points in self._points.values())

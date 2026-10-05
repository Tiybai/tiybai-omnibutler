"""Phone gateway: how a phone pushes data and events into the bridge.

Access mode B (phone gateway) exists because some of the most useful
sources in a home have no direct bridge interface at all: the phone in
your pocket knows when you left the house, and the watch on your wrist
knows how you slept. Neither speaks miIO or Tuya. What they *can* do is
make an HTTPS call - so the bridge offers them one small, token-guarded
HTTP entry point (stdlib only, same shape as the MCP HTTP transport):

    POST /ingest   batches of data points -> core.streams.StreamStore
    POST /event    phone events -> core.events.EventBus (source="phone")
    GET  /health   {"status": "ok"} -- deliberately unauthenticated

The /event endpoint is also the bridge's *geofence producer*: until now
the scene engine could react to geofence triggers but nothing in the
system ever emitted one. A phone posting

    {"type": "geofence", "zone": "home", "transition": "enter"}

publishes Event(type="geofence", source="phone",
data={"zone": "home", "transition": "enter"}) - exactly the shape
SceneEngine._trigger_matches compares trigger.zone / trigger.transition
against (see examples/scenes/arrive-home.yaml: zone "home", transitions
"enter" / "exit"). Other event types (presence, ...) pass through with
their payload as event data.

Authentication is not optional and uses its own token
(OMNIBUTLER_GATEWAY_TOKEN) - separate from the MCP HTTP token and from
the approvals-page token, so a phone never holds a credential that can
do anything else. The token is only ever compared, never logged.

Do NOT expose this port directly to the public internet; like the rest
of the bridge it belongs behind Cloudflare Access or WireGuard. The
default bind is 127.0.0.1 and binding 0.0.0.0 logs a loud warning.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from omnibutler.core.events import Event, EventBus
from omnibutler.core.streams import DataPoint, DataStream, StreamStore

logger = logging.getLogger(__name__)

TOKEN_ENV_VAR = "OMNIBUTLER_GATEWAY_TOKEN"
MAX_BODY_BYTES = 1024 * 1024  # 1 MiB: a day of phone data is far smaller.
DEFAULT_PORT = 8766

_GEOFENCE_TRANSITIONS = ("enter", "exit")


class GatewayError(RuntimeError):
    """Raised when the phone gateway cannot be started safely."""


def _resolve_token(token: str | None) -> str:
    resolved = token if token is not None else os.environ.get(TOKEN_ENV_VAR, "")
    if not resolved:
        raise GatewayError(
            f"refusing to start the phone gateway without a token: set the "
            f"{TOKEN_ENV_VAR} environment variable (the endpoint is never "
            f"served unauthenticated)"
        )
    return resolved


# -- payload handling (pure helpers, shared by the handler and tests) ---------


def build_event(payload: Any) -> Event:
    """Translate one /event payload into a bus Event (source="phone").

    The payload's ``type`` becomes the event type and every remaining
    field becomes event data - except that geofence events are validated
    against the shape the scene engine matches on: a non-empty ``zone``
    string and a ``transition`` of "enter" or "exit". An optional numeric
    ``ts`` becomes the event timestamp and is removed from the data.
    Raises ValueError for anything else.
    """
    if not isinstance(payload, dict):
        raise ValueError("event payload must be a JSON object")
    event_type = payload.get("type")
    if not isinstance(event_type, str) or not event_type.strip():
        raise ValueError("event payload needs a non-empty string 'type'")
    data = {k: v for k, v in payload.items() if k not in ("type", "ts")}
    if event_type == "geofence":
        zone = data.get("zone")
        if not isinstance(zone, str) or not zone.strip():
            raise ValueError("geofence event needs a non-empty string 'zone'")
        transition = data.get("transition")
        if transition not in _GEOFENCE_TRANSITIONS:
            raise ValueError(
                f"geofence event 'transition' must be one of "
                f"{_GEOFENCE_TRANSITIONS}, got {transition!r}"
            )
    ts = payload.get("ts")
    event = Event(type=event_type, source="phone", data=data)
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        event.timestamp = float(ts)
    return event


def ingest_points(store: StreamStore, payload: Any) -> int:
    """Write one /ingest payload into *store*; returns the point count.

    Accepted shape (both forms may be mixed in one batch)::

        {"stream": {"id": ..., "kind": ..., "source": ..., "unit": ...},
         "points": [{"ts": ..., "value": ..., "meta": {...}}, ...]}

        {"points": [{"stream": {...descriptor...}, "ts":..., "value":...},
                    {"stream_id": "<already registered>", "value": ...}]}

    Every point needs a ``value``; ``ts`` is optional (server time when
    absent). The whole batch is validated before anything is written, so
    a malformed batch stores nothing. Raises ValueError on bad shapes.
    """
    if not isinstance(payload, dict):
        raise ValueError("ingest payload must be a JSON object")
    raw_points = payload.get("points")
    if not isinstance(raw_points, list):
        raise ValueError("ingest payload needs a 'points' array")
    default_descriptor = payload.get("stream")

    parsed: list[tuple[DataStream | None, DataPoint]] = []
    for raw in raw_points:
        if not isinstance(raw, dict):
            raise ValueError(f"each point must be an object, got {raw!r}")
        if "value" not in raw:
            raise ValueError(f"point is missing its 'value': {raw!r}")
        descriptor = raw.get("stream", default_descriptor)
        stream: DataStream | None = None
        if descriptor is not None:
            stream = DataStream.from_dict(descriptor)
        stream_id = raw.get("stream_id") or (stream.id if stream else None)
        if not stream_id:
            raise ValueError(
                f"point has neither 'stream_id' nor a stream descriptor: "
                f"{raw!r}"
            )
        ts = raw.get("ts")
        if ts is not None and (
            not isinstance(ts, (int, float)) or isinstance(ts, bool)
        ):
            raise ValueError(f"point 'ts' must be a number, got {ts!r}")
        meta = raw.get("meta") or {}
        if not isinstance(meta, dict):
            raise ValueError(f"point 'meta' must be an object, got {meta!r}")
        parsed.append((stream, DataPoint(
            stream_id=str(stream_id),
            ts=time.time() if ts is None else float(ts),
            value=raw["value"],
            meta=meta,
        )))

    # Batch-local descriptors count as registrations for validation, so a
    # point may reference a stream an earlier point in the batch defines.
    batch_ids = {s.id for s, _ in parsed if s is not None}
    for stream, point in parsed:
        if stream is None and point.stream_id not in batch_ids \
                and store.get_stream(point.stream_id) is None:
            raise ValueError(
                f"point references unknown stream {point.stream_id!r} and "
                f"carries no stream descriptor"
            )

    for stream, _ in parsed:
        if stream is not None:
            store.register(stream)
    accepted = 0
    for _, point in parsed:
        store.append(point.stream_id, point.value, ts=point.ts, meta=point.meta)
        accepted += 1
    return accepted


# -- HTTP plumbing --------------------------------------------------------------


def make_handler(bus: EventBus, store: StreamStore, token: str):
    """Build a request-handler class bound to *bus*, *store* and *token*.

    Exposed as a factory so tests (or an embedding application) can inject
    their own bus/store/token pair into a server of their choosing.
    """

    class GatewayHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "tiybai-omnibutler-gateway"

        # -- plumbing ------------------------------------------------------
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Route access logs through logging at debug level. Headers and
            # bodies are never part of this format, so the token cannot
            # leak into logs through here.
            logger.debug("%s - %s", self.address_string(), format % args)

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, status: int, message: str) -> None:
            self._send_json(status, {"error": message})

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization") or ""
            expected = f"Bearer {token}"
            return hmac.compare_digest(header, expected)

        def _read_json_body(self) -> dict[str, Any] | None:
            """Parse the request body, or send the error and return None."""
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_error_json(400, "invalid Content-Length")
                return None
            if length > MAX_BODY_BYTES:
                self._send_error_json(413, "request body too large")
                return None
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_error_json(400, "request body is not valid JSON")
                return None
            if not isinstance(payload, dict):
                self._send_error_json(400, "request body must be a JSON object")
                return None
            return payload

        # -- routes ----------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            if self.path == "/health":
                self._send_json(200, {"status": "ok"})
                return
            if self.path in ("/ingest", "/event"):
                self._send_error_json(
                    405, f"method not allowed: use POST {self.path}")
                return
            self._send_error_json(404, "not found")

        def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
            if self.path not in ("/ingest", "/event"):
                self._send_error_json(404, "not found")
                return
            if not self._authorized():
                self.send_response(401)
                self.send_header("WWW-Authenticate", "Bearer")
                body = json.dumps({"error": "unauthorized"}).encode("utf-8")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            payload = self._read_json_body()
            if payload is None:
                return  # error response already sent
            try:
                if self.path == "/ingest":
                    accepted = ingest_points(store, payload)
                    self._send_json(200, {"status": "ok", "accepted": accepted})
                else:
                    event = build_event(payload)
                    bus.publish(event)
                    self._send_json(
                        200, {"status": "published", "type": event.type})
            except ValueError as exc:
                self._send_error_json(400, str(exc))
            except Exception:  # the gateway must not die on a bad call
                logger.exception("phone gateway dispatch failed")
                self._send_error_json(500, "internal error")

    return GatewayHandler


def create_http_server(
    bus: EventBus,
    store: StreamStore,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    token: str | None = None,
) -> ThreadingHTTPServer:
    """Create (but do not start) the gateway server; caller serves it.

    Raises GatewayError when no token is available.
    """
    resolved = _resolve_token(token)
    if host in {"0.0.0.0", "::"}:
        logger.warning(
            "phone gateway binding %s:%s is reachable from the whole "
            "network. Do not expose it directly to the internet - put it "
            "behind Cloudflare Access or WireGuard.", host, port,
        )
    return ThreadingHTTPServer((host, port), make_handler(bus, store, resolved))


def serve(
    bus: EventBus,
    store: StreamStore,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    token: str | None = None,
) -> None:
    """Serve the phone gateway until interrupted (blocking)."""
    httpd = create_http_server(bus, store, host=host, port=port, token=token)
    logger.info("phone gateway listening on %s:%s (POST /ingest, "
                "POST /event, GET /health)", host, httpd.server_address[1])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

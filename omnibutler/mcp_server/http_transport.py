"""HTTP transport for the MCP server (streamable-HTTP style, stdlib only).

The stdio transport (see server.run_stdio) only works for agents running on
the same machine as the bridge. This module exposes the *same* JSON-RPC
dispatch (McpServer.handle) over HTTP so remote / service-style agents
(OpenClaw, Hermes, a cloud Muse, ...) can reach the bridge:

    POST /mcp     one JSON-RPC message in, one JSON response out
    GET  /health  {"status": "ok"} -- deliberately unauthenticated so load
                  balancers / uptime checks can use it

Authentication is not optional. The server refuses to start unless a bearer
token is configured (environment variable OMNIBUTLER_HTTP_TOKEN, or the
``token=`` argument to serve()/create_http_server(), which exists for
tests). Requests to /mcp without ``Authorization: Bearer <token>`` get a
401. The token is only ever compared, never logged.

Do NOT expose this port directly to the public internet. It is meant to sit
behind the project's usual remote-access story (Cloudflare Access or
WireGuard); binding 0.0.0.0 logs a loud warning for that reason. The default
bind is 127.0.0.1.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnibutler.mcp_server.server import McpServer

logger = logging.getLogger(__name__)

TOKEN_ENV_VAR = "OMNIBUTLER_HTTP_TOKEN"
MAX_BODY_BYTES = 1024 * 1024  # 1 MiB: JSON-RPC tool calls are small.


class HttpTransportError(RuntimeError):
    """Raised when the HTTP transport cannot be started safely."""


def _resolve_token(token: str | None) -> str:
    resolved = token if token is not None else os.environ.get(TOKEN_ENV_VAR, "")
    if not resolved:
        raise HttpTransportError(
            f"refusing to start the MCP HTTP transport without a token: set "
            f"the {TOKEN_ENV_VAR} environment variable (the endpoint is never "
            f"served unauthenticated)"
        )
    return resolved


def make_handler(server: McpServer, token: str):
    """Build a request-handler class bound to *server* and *token*.

    Exposed as a factory so tests (or an embedding application) can inject
    their own McpServer/token pair into an HTTPServer of their choosing.
    """

    class McpHttpHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "tiybai-omnibutler-http"

        # -- plumbing ------------------------------------------------------
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Route access logs through logging at debug level. Headers and
            # bodies are never part of this format, so the token cannot leak
            # into logs through here.
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

        # -- routes ----------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            if self.path == "/health":
                self._send_json(200, {"status": "ok"})
                return
            if self.path == "/mcp":
                self._send_error_json(405, "method not allowed: use POST /mcp")
                return
            self._send_error_json(404, "not found")

        def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
            if self.path != "/mcp":
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

            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_error_json(400, "invalid Content-Length")
                return
            if length > MAX_BODY_BYTES:
                self._send_error_json(413, "request body too large")
                return
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_error_json(400, "request body is not valid JSON")
                return
            if not isinstance(message, dict):
                self._send_error_json(
                    400, "request body must be a single JSON-RPC object")
                return

            try:
                response = server.handle(message)
            except Exception:  # transport must not die on a bad call
                logger.exception("MCP dispatch failed over HTTP")
                self._send_error_json(500, "internal error")
                return
            if response is None:
                # JSON-RPC notification: accepted, nothing to say back.
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_json(200, response)

    return McpHttpHandler


def create_http_server(
    server: McpServer,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
) -> ThreadingHTTPServer:
    """Create (but do not start) the HTTP server; caller runs serve_forever.

    Raises HttpTransportError when no token is available.
    """
    resolved = _resolve_token(token)
    if host in {"0.0.0.0", "::"}:
        logger.warning(
            "MCP HTTP transport binding %s:%s is reachable from the whole "
            "network. Do not expose it directly to the internet - put it "
            "behind Cloudflare Access or WireGuard.", host, port,
        )
    return ThreadingHTTPServer((host, port), make_handler(server, resolved))


def serve(
    server: McpServer,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
) -> None:
    """Serve the MCP HTTP transport until interrupted (blocking)."""
    httpd = create_http_server(server, host=host, port=port, token=token)
    logger.info("MCP HTTP transport listening on %s:%s (POST /mcp, "
                "GET /health)", host, httpd.server_address[1])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

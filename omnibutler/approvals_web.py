"""Local approvals web page: human-only, out-of-band confirmation clicks.

v0.3 moved high-risk approvals out of the MCP server entirely - an agent
can park an action in the confirmation queue but can never approve it.
The remaining approval paths are the host terminal (``tob confirm``) and
this page. Both are *human-only and out-of-band*: nothing here is exposed
through MCP, and no agent tool can reach these endpoints.

The page is deliberately tiny (stdlib ``http.server``, no framework):

    GET  /                list pending confirmations, each with two form
                          buttons: 「批准执行」 (approve) and 「拒绝」 (reject)
    POST /approve/<id>    approve + execute, exactly like ``tob confirm``
    POST /reject/<id>     reject, exactly like ``tob reject``

Authentication is not optional and is *separate* from the MCP HTTP
transport: the server refuses to start unless a token is configured via
the ``OMNIBUTLER_APPROVALS_TOKEN`` environment variable (or the
``token=`` argument, which exists for tests). Requests must carry it as
``Authorization: Bearer <token>`` or ``?token=<token>``; the page's own
forms carry it in their action URLs so the buttons work from a browser.
The token is only ever compared (constant-time), never logged - access
logs strip the query string for exactly that reason.

Do NOT expose this port directly to the public internet. It is meant to
sit behind the project's usual remote-access story (Cloudflare Access or
WireGuard); binding 0.0.0.0 logs a loud warning. Default bind is 127.0.0.1.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import os
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)

TOKEN_ENV_VAR = "OMNIBUTLER_APPROVALS_TOKEN"
AGENT = "web:human"
MAX_BODY_BYTES = 64 * 1024  # approval forms are tiny


class ApprovalsWebError(RuntimeError):
    """Raised when the approvals page cannot be started safely."""


def _resolve_token(token: str | None) -> str:
    resolved = token if token is not None else os.environ.get(TOKEN_ENV_VAR, "")
    if not resolved:
        raise ApprovalsWebError(
            f"refusing to start the approvals page without a token: set "
            f"the {TOKEN_ENV_VAR} environment variable (it is separate "
            f"from OMNIBUTLER_HTTP_TOKEN; the page is never served "
            f"unauthenticated)"
        )
    return resolved


# -- shared human-decision path ----------------------------------------------
# The web page, the macOS dialog (notify_macos) and the CLI all resolve a
# queued item the same way: check it is still pending, go through
# engine.confirm / engine.reject, and write the same audit record shape.
# The only difference is the ``agent`` label (cli:human / web:human /
# macos:human), which is how the audit log shows *where* the human was.

def describe_item(item) -> str:
    if item.kind == "set_property":
        what = f"set {item.device_id}.{item.name} = {item.value!r}"
    else:
        what = f"call {item.device_id}.{item.name}({item.params})"
    return what


def apply_human_decision(
    engine,
    confirmations,
    manager,
    confirmation_id: str,
    *,
    approve: bool,
    agent: str,
) -> tuple[bool, str]:
    """Approve (execute) or reject one pending confirmation for a human.

    Returns ``(ok, human-readable message)``. Mirrors ``tob confirm`` /
    ``tob reject``: execution goes through the scene engine, and the
    decision lands in the audit log under *agent*.
    """
    item = confirmations.get(confirmation_id)
    if item is None or item.status != "pending":
        return False, f"no pending confirmation {confirmation_id!r}"

    if approve:
        try:
            result = engine.confirm(item.id, agent=agent)
        except Exception as exc:  # a sick driver must not kill the page
            return False, f"execution failed for {item.id}: {exc}"
        if result is None:
            return False, f"confirmation {item.id} could not be executed"
        decision = "approved"
    else:
        if not engine.reject(item.id):
            return False, f"confirmation {item.id} could not be rejected"
        result = None
        decision = "rejected"

    manager.audit.record(
        agent=agent, device_id=item.device_id,
        action=f"confirmation:{decision}",
        params={"confirmation_id": item.id, "kind": item.kind,
                "name": item.name, "value": item.value,
                "requested_by": item.requested_by, "scene": item.scene},
        result={"confirmation_id": item.id, "decision": decision},
    )
    if approve:
        return True, (f"已批准并执行：{describe_item(item)}"
                      f"（结果：{json.dumps(result, ensure_ascii=False, default=str)}）")
    return True, f"已拒绝：{describe_item(item)}（未执行任何动作）"


def _format_wait(created_at: float) -> str:
    seconds = max(0, int(time.time() - created_at))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {seconds} 秒"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {minutes} 分"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时"


# -- page rendering -------------------------------------------------------------
# Every interpolated value is HTML-escaped: device names, scene names and
# requested_by strings come from outside this process (scenes, agents) and
# must never become markup.

_PAGE_HEAD = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OmniButler 待确认动作</title>
<style>
body { font-family: -apple-system, "PingFang SC", sans-serif; margin: 2rem auto;
       max-width: 44rem; padding: 0 1rem; color: #222; }
.item { border: 1px solid #ddd; border-radius: 8px; padding: 1rem; margin: 1rem 0; }
.meta { color: #666; font-size: .9em; }
.notice { border-radius: 8px; padding: .8rem 1rem; margin: 1rem 0; }
.ok { background: #e6f4ea; } .err { background: #fce8e6; }
button { font-size: 1rem; padding: .5rem 1.2rem; margin-right: .6rem;
         border-radius: 6px; border: 1px solid #999; cursor: pointer; }
button.approve { background: #b3261e; color: #fff; border-color: #b3261e; }
</style></head><body>
<h1>待确认的高风险动作</h1>
<p class="meta">这些动作由场景或 AI 请求，已被安全护栏拦下。
只有你在这里（或在主机终端 <code>tob confirm</code>）批准后才会执行。</p>
"""


def _render_page(confirmations, token: str, notice: tuple[bool, str] | None) -> bytes:
    parts = [_PAGE_HEAD]
    if notice is not None:
        ok, message = notice
        parts.append(
            f'<div class="notice {"ok" if ok else "err"}">'
            f"{html.escape(message)}</div>"
        )
    pending = confirmations.pending()
    if not pending:
        parts.append("<p>（没有待确认的动作）</p>")
    quoted_token = urllib.parse.quote(token, safe="")
    for item in pending:
        quoted_id = urllib.parse.quote(item.id, safe="")
        origin = item.scene or item.requested_by or "-"
        parts.append(
            '<div class="item">'
            f"<div><strong>{html.escape(describe_item(item))}</strong></div>"
            f'<div class="meta">编号：{html.escape(item.id)}'
            f" · 设备：{html.escape(item.device_id)}"
            f" · 风险：{html.escape(item.risk)}"
            f" · 来源：{html.escape(origin)}"
            f" · 已等待：{_format_wait(item.created_at)}</div>"
            f'<form method="post" action="/approve/{quoted_id}?token={quoted_token}"'
            ' style="display:inline">'
            '<button class="approve" type="submit">批准执行</button></form>'
            f'<form method="post" action="/reject/{quoted_id}?token={quoted_token}"'
            ' style="display:inline">'
            '<button type="submit">拒绝</button></form>'
            "</div>"
        )
    parts.append("</body></html>")
    return "".join(parts).encode("utf-8")


def make_handler(engine, confirmations, manager, token: str):
    """Build a request-handler class bound to the bridge + *token*.

    Exposed as a factory so tests (or an embedding application) can inject
    their own engine/queue/manager into an HTTPServer of their choosing.
    """

    class ApprovalsHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "tiybai-omnibutler-approvals"

        # -- plumbing ------------------------------------------------------
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Never log the request line verbatim: the URL carries the
            # token in its query string. Log method + path only.
            logger.debug("%s - %s %s", self.address_string(),
                         self.command, self.path.split("?", 1)[0])

        def _send_html(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, status: int, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self, extra_tokens: list[str] | None = None) -> bool:
            header = self.headers.get("Authorization") or ""
            if hmac.compare_digest(header, f"Bearer {token}"):
                return True
            query = urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query)
            candidates = list(query.get("token", [])) + (extra_tokens or [])
            return any(hmac.compare_digest(c, token) for c in candidates)

        def _unauthorized(self) -> None:
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Bearer")
            body = b"unauthorized"
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_form_token(self) -> list[str]:
            """Drain the request body; return any ``token`` form field."""
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                length = 0
            if length <= 0:
                return []
            raw = self.rfile.read(min(length, MAX_BODY_BYTES))
            try:
                fields = urllib.parse.parse_qs(raw.decode("utf-8"))
            except UnicodeDecodeError:
                return []
            return list(fields.get("token", []))

        # -- routes ----------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            path = urllib.parse.urlsplit(self.path).path
            if path != "/":
                self._send_text(404, "not found")
                return
            if not self._authorized():
                self._unauthorized()
                return
            self._send_html(200, _render_page(confirmations, token, None))

        def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
            path = urllib.parse.urlsplit(self.path).path
            parts = [p for p in path.split("/") if p]
            action = parts[0] if parts else ""
            if action not in {"approve", "reject"} or len(parts) != 2:
                self._send_text(404, "not found")
                return
            form_tokens = self._read_form_token()
            if not self._authorized(extra_tokens=form_tokens):
                self._unauthorized()
                return
            confirmation_id = urllib.parse.unquote(parts[1])
            ok, message = apply_human_decision(
                engine, confirmations, manager, confirmation_id,
                approve=(action == "approve"), agent=AGENT,
            )
            self._send_html(200, _render_page(confirmations, token, (ok, message)))

    return ApprovalsHandler


def create_http_server(
    engine,
    confirmations,
    manager,
    host: str = "127.0.0.1",
    port: int = 8766,
    token: str | None = None,
) -> ThreadingHTTPServer:
    """Create (but do not start) the approvals server.

    Raises ApprovalsWebError (a RuntimeError) when no token is available.
    """
    resolved = _resolve_token(token)
    if host in {"0.0.0.0", "::"}:
        logger.warning(
            "approvals page binding %s:%s is reachable from the whole "
            "network. Do not expose it directly to the internet - put it "
            "behind Cloudflare Access or WireGuard.", host, port,
        )
    return ThreadingHTTPServer(
        (host, port), make_handler(engine, confirmations, manager, resolved))


def serve(
    engine,
    confirmations,
    manager,
    host: str = "127.0.0.1",
    port: int = 8766,
    token: str | None = None,
) -> None:
    """Serve the approvals page until interrupted (blocking)."""
    httpd = create_http_server(engine, confirmations, manager,
                               host=host, port=port, token=token)
    logger.info("approvals page listening on %s:%s",
                host, httpd.server_address[1])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

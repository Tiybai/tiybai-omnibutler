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

Language: the page renders in Chinese when the browser's
``Accept-Language`` includes a ``zh`` preference, otherwise in English;
an explicit ``?lang=zh`` / ``?lang=en`` always wins over the header, and
the page's forms carry the choice so a decision result comes back in
the same language.

Hardening: every request socket has a 30 s timeout (the same budget the
gateway and the MCP HTTP transport use); a server bound to a loopback
address refuses requests whose Host header is not loopback, which
blunts DNS rebinding from a web page the operator happens to visit; an
oversized or malformed POST body is answered with ``Connection:
close`` and the connection is dropped instead of being reused.
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
# Per-request socket timeout, matching the gateway / MCP HTTP servers:
# a client that connects and stalls must not pin a handler forever.
SOCKET_TIMEOUT_SECONDS = 30.0
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


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


# -- page language --------------------------------------------------------------
# Every user-facing string on the page, per language. "zh" keeps the
# wording the page has always used; "en" is the translation. The
# decision messages produced by apply_human_decision live here too.

_STRINGS: dict[str, dict[str, str]] = {
    "zh": {
        "html_lang": "zh-CN",
        "title": "OmniButler 待确认动作",
        "heading": "待确认的高风险动作",
        "intro": (
            "这些动作由场景或 AI 请求，已被安全护栏拦下。"
            "只有你在这里（或在主机终端 <code>tob confirm</code>）批准后才会执行。"
        ),
        "empty": "（没有待确认的动作）",
        "field_id": "编号",
        "field_device": "设备",
        "field_risk": "风险",
        "field_source": "来源",
        "field_waited": "已等待",
        "field_sep": "：",
        "approve": "批准执行",
        "reject": "拒绝",
        "wait_seconds": "{n} 秒",
        "wait_minutes": "{m} 分 {s} 秒",
        "wait_hours": "{h} 小时 {m} 分",
        "wait_days": "{d} 天 {h} 小时",
        "not_pending": "没有待确认的动作 {item_id!r}",
        "exec_failed": "执行 {item_id} 失败：{exc}",
        "not_executed": "动作 {item_id} 无法执行",
        "not_rejected": "动作 {item_id} 无法拒绝",
        "approved": "已批准并执行：{desc}（结果：{result}）",
        "rejected": "已拒绝：{desc}（未执行任何动作）",
    },
    "en": {
        "html_lang": "en",
        "title": "OmniButler pending approvals",
        "heading": "High-risk actions waiting for approval",
        "intro": (
            "These actions were requested by a scene or an AI and were "
            "stopped by the safety guardrails. They run only after you "
            "approve them here (or with <code>tob confirm</code> on the "
            "host terminal)."
        ),
        "empty": "(nothing waiting for approval)",
        "field_id": "ID",
        "field_device": "Device",
        "field_risk": "Risk",
        "field_source": "Source",
        "field_waited": "Waiting",
        "field_sep": ": ",
        "approve": "Approve &amp; run",
        "reject": "Reject",
        "wait_seconds": "{n} sec",
        "wait_minutes": "{m} min {s} sec",
        "wait_hours": "{h} h {m} min",
        "wait_days": "{d} d {h} h",
        "not_pending": "no pending confirmation {item_id!r}",
        "exec_failed": "execution failed for {item_id}: {exc}",
        "not_executed": "confirmation {item_id} could not be executed",
        "not_rejected": "confirmation {item_id} could not be rejected",
        "approved": "Approved and executed: {desc} (result: {result})",
        "rejected": "Rejected: {desc} (nothing was executed)",
    },
}


def _resolve_lang(explicit: str | None,
                  accept_language: str | None) -> str:
    """Pick the page language: an explicit ``?lang=`` always wins.

    Without it, the browser's ``Accept-Language`` decides - Chinese
    when its preferences include a ``zh`` tag, English otherwise.
    """
    if explicit:
        tag = explicit.strip().lower()
        if tag.startswith("zh"):
            return "zh"
        if tag.startswith("en"):
            return "en"
    if accept_language:
        tags = [part.split(";")[0].strip().lower()
                for part in accept_language.split(",")]
        if any(tag.startswith("zh") for tag in tags):
            return "zh"
    return "en"


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
    lang: str = "zh",
) -> tuple[bool, str]:
    """Approve (execute) or reject one pending confirmation for a human.

    Returns ``(ok, human-readable message)``. Mirrors ``tob confirm`` /
    ``tob reject``: execution goes through the scene engine, and the
    decision lands in the audit log under *agent*. ``lang`` only picks
    the message language; the macOS dialog keeps the default.
    """
    strings = _STRINGS.get(lang, _STRINGS["zh"])
    item = confirmations.get(confirmation_id)
    if item is None or item.status != "pending":
        return False, strings["not_pending"].format(item_id=confirmation_id)

    if approve:
        try:
            result = engine.confirm(item.id, agent=agent)
        except Exception as exc:  # a sick driver must not kill the page
            return False, strings["exec_failed"].format(
                item_id=item.id, exc=exc)
        if result is None:
            return False, strings["not_executed"].format(item_id=item.id)
        decision = "approved"
    else:
        if not engine.reject(item.id):
            return False, strings["not_rejected"].format(item_id=item.id)
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
        return True, strings["approved"].format(
            desc=describe_item(item),
            result=json.dumps(result, ensure_ascii=False, default=str))
    return True, strings["rejected"].format(desc=describe_item(item))


def _format_wait(created_at: float, lang: str = "zh") -> str:
    strings = _STRINGS.get(lang, _STRINGS["zh"])
    seconds = max(0, int(time.time() - created_at))
    if seconds < 60:
        return strings["wait_seconds"].format(n=seconds)
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return strings["wait_minutes"].format(m=minutes, s=seconds)
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return strings["wait_hours"].format(h=hours, m=minutes)
    days, hours = divmod(hours, 24)
    return strings["wait_days"].format(d=days, h=hours)


# -- page rendering -------------------------------------------------------------
# Every interpolated value is HTML-escaped: device names, scene names and
# requested_by strings come from outside this process (scenes, agents) and
# must never become markup.

def _page_head(lang: str) -> str:
    strings = _STRINGS.get(lang, _STRINGS["zh"])
    return f"""<!DOCTYPE html>
<html lang="{strings['html_lang']}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{strings['title']}</title>
<style>
body {{ font-family: -apple-system, "PingFang SC", sans-serif; margin: 2rem auto;
       max-width: 44rem; padding: 0 1rem; color: #222; }}
.item {{ border: 1px solid #ddd; border-radius: 8px; padding: 1rem; margin: 1rem 0;
        overflow-wrap: anywhere; }}
.meta {{ color: #666; font-size: .9em; }}
.notice {{ border-radius: 8px; padding: .8rem 1rem; margin: 1rem 0; }}
.ok {{ background: #e6f4ea; }} .err {{ background: #fce8e6; }}
button {{ font-size: 1rem; padding: .5rem 1.2rem; margin-right: .6rem;
         min-height: 44px;
         border-radius: 6px; border: 1px solid #999; cursor: pointer; }}
button.approve {{ background: #b3261e; color: #fff; border-color: #b3261e; }}
@media (prefers-color-scheme: dark) {{
  body {{ background: #121212; color: #e8e8e8; }}
  .item {{ border-color: #444; }}
  .meta {{ color: #aaa; }}
  .ok {{ background: #17351a; }} .err {{ background: #3d1a16; }}
  button {{ background: #1e1e1e; color: #e8e8e8; border-color: #666; }}
  button.approve {{ background: #b3261e; color: #fff; border-color: #b3261e; }}
}}
</style></head><body>
<h1>{strings['heading']}</h1>
<p class="meta">{strings['intro']}</p>
"""


def _render_page(confirmations, token: str, notice: tuple[bool, str] | None,
                 lang: str = "zh") -> bytes:
    strings = _STRINGS.get(lang, _STRINGS["zh"])
    sep = strings["field_sep"]
    parts = [_page_head(lang)]
    if notice is not None:
        ok, message = notice
        parts.append(
            f'<div class="notice {"ok" if ok else "err"}">'
            f"{html.escape(message)}</div>"
        )
    pending = confirmations.pending()
    if not pending:
        parts.append(f"<p>{strings['empty']}</p>")
    quoted_token = urllib.parse.quote(token, safe="")
    for item in pending:
        quoted_id = urllib.parse.quote(item.id, safe="")
        origin = item.scene or item.requested_by or "-"
        parts.append(
            '<div class="item">'
            f"<div><strong>{html.escape(describe_item(item))}</strong></div>"
            f'<div class="meta">{strings["field_id"]}{sep}'
            f"{html.escape(item.id)}"
            f' · {strings["field_device"]}{sep}'
            f"{html.escape(item.device_id)}"
            f' · {strings["field_risk"]}{sep}{html.escape(item.risk)}'
            f' · {strings["field_source"]}{sep}{html.escape(origin)}'
            f' · {strings["field_waited"]}{sep}'
            f"{_format_wait(item.created_at, lang)}</div>"
            f'<form method="post" '
            f'action="/approve/{quoted_id}?token={quoted_token}&amp;lang={lang}"'
            ' style="display:inline">'
            f'<button class="approve" type="submit">{strings["approve"]}</button></form>'
            f'<form method="post" '
            f'action="/reject/{quoted_id}?token={quoted_token}&amp;lang={lang}"'
            ' style="display:inline">'
            f'<button type="submit">{strings["reject"]}</button></form>'
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
        def setup(self) -> None:
            super().setup()
            # Bound every request, like the gateway and the MCP HTTP
            # transport: a client that connects and then stalls must
            # not pin a handler thread (and its socket) forever.
            self.connection.settimeout(SOCKET_TIMEOUT_SECONDS)

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

        def _send_text(self, status: int, text: str, *,
                       close: bool = False) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
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

        def _host_rejected(self) -> bool:
            """Loopback binds refuse non-loopback Host headers.

            Otherwise any web page the operator visits could aim the
            browser at this local port (DNS rebinding); the token is
            the only other barrier. Wider binds are the operator's
            explicit choice (already loudly warned about) and skip
            the check.
            """
            address = self.server.server_address
            bind_host = str(
                address[0] if isinstance(address, tuple) else address
            ).lower()
            if bind_host not in _LOOPBACK_HOSTS:
                return False
            host = self.headers.get("Host")
            if host is None:
                return False
            name = host.strip().lower()
            name = (
                name[1:].split("]", 1)[0]
                if name.startswith("[")
                else name.split(":", 1)[0]
            )
            return name not in _LOOPBACK_HOSTS

        def _request_lang(self) -> str:
            query = urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query)
            lang_values = query.get("lang")
            explicit = lang_values[0] if lang_values else None
            return _resolve_lang(
                explicit, self.headers.get("Accept-Language"))

        def _read_form_token(self) -> list[str] | None:
            """Drain the request body; return any ``token`` form field.

            Returns None when the body is unusable - a malformed or
            negative Content-Length, or a body over MAX_BODY_BYTES.
            Those are answered on the spot (400/413 with
            ``Connection: close``): the unread remainder would poison
            the keep-alive stream, so the connection is dropped rather
            than reused for another request.
            """
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                length = -1
            if length < 0:
                self._send_text(400, "bad request", close=True)
                return None
            if length > MAX_BODY_BYTES:
                self._send_text(413, "request body too large", close=True)
                return None
            if length == 0:
                return []
            raw = self.rfile.read(length)
            try:
                fields = urllib.parse.parse_qs(raw.decode("utf-8"))
            except UnicodeDecodeError:
                return []
            return list(fields.get("token", []))

        # -- routes ----------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            if self._host_rejected():
                self._send_text(403, "forbidden host")
                return
            path = urllib.parse.urlsplit(self.path).path
            if path != "/":
                self._send_text(404, "not found")
                return
            if not self._authorized():
                self._unauthorized()
                return
            self._send_html(200, _render_page(
                confirmations, token, None, lang=self._request_lang()))

        def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
            if self._host_rejected():
                self._send_text(403, "forbidden host")
                return
            path = urllib.parse.urlsplit(self.path).path
            parts = [p for p in path.split("/") if p]
            action = parts[0] if parts else ""
            if action not in {"approve", "reject"} or len(parts) != 2:
                self._send_text(404, "not found")
                return
            form_tokens = self._read_form_token()
            if form_tokens is None:
                return  # error already answered, connection closing
            if not self._authorized(extra_tokens=form_tokens):
                self._unauthorized()
                return
            lang = self._request_lang()
            confirmation_id = urllib.parse.unquote(parts[1])
            ok, message = apply_human_decision(
                engine, confirmations, manager, confirmation_id,
                approve=(action == "approve"), agent=AGENT, lang=lang,
            )
            self._send_html(200, _render_page(
                confirmations, token, (ok, message), lang=lang))

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

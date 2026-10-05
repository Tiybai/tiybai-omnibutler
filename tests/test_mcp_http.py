"""HTTP transport for the MCP server: real socket, real urllib client.

Covers the auth gate (no token -> 401, wrong token -> 401, no token
configured -> refuse to start), /health without auth, and parity between
HTTP responses and the stdio dispatch (McpServer.handle) for initialize,
tools/list and a read-only tools/call.
"""

import http.client
import json
import threading
import urllib.error
import urllib.request

import pytest

from omnibutler.mcp_server.http_transport import (
    MAX_BODY_BYTES,
    TOKEN_ENV_VAR,
    HttpTransportError,
    create_http_server,
    serve,
)
from omnibutler.mcp_server.server import create_server

TOKEN = "test-token-not-a-real-secret"


@pytest.fixture()
def mcp(manager, engine):
    return create_server(manager, engine=engine,
                         confirmations=engine.confirmations)


@pytest.fixture()
def http_base(mcp):
    httpd = create_http_server(mcp, host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, None


def _rpc_http(base, message, token=TOKEN):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return _request(base + "/mcp",
                    data=json.dumps(message).encode(), headers=headers)


# -- the auth gate -------------------------------------------------------------


def test_health_needs_no_auth(http_base):
    status, payload = _request(http_base + "/health")
    assert status == 200
    assert payload == {"status": "ok"}


def test_mcp_without_token_is_401(http_base):
    status, _ = _rpc_http(http_base, {"jsonrpc": "2.0", "id": 1,
                                      "method": "ping"}, token=None)
    assert status == 401


def test_mcp_with_wrong_token_is_401(http_base):
    status, _ = _rpc_http(http_base, {"jsonrpc": "2.0", "id": 1,
                                      "method": "ping"}, token="nope")
    assert status == 401


def test_refuses_to_start_without_token(mcp, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    with pytest.raises(HttpTransportError) as exc:
        create_http_server(mcp, host="127.0.0.1", port=0)
    assert TOKEN_ENV_VAR in str(exc.value)
    with pytest.raises(HttpTransportError):
        serve(mcp, host="127.0.0.1", port=0)


def test_token_can_come_from_env(mcp, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV_VAR, TOKEN)
    httpd = create_http_server(mcp, "127.0.0.1", 0)
    try:
        assert httpd.server_address[1] > 0
    finally:
        httpd.server_close()


# -- parity with the stdio dispatch ---------------------------------------------


def test_initialize_and_tools_list_match_stdio(http_base, mcp):
    for message in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ):
        status, payload = _rpc_http(http_base, message)
        assert status == 200
        assert payload == mcp.handle(dict(message))


def test_readonly_tools_call_matches_stdio(http_base, mcp):
    message = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
               "params": {"name": "list_devices", "arguments": {}}}
    status, payload = _rpc_http(http_base, message)
    assert status == 200
    assert payload == mcp.handle(json.loads(json.dumps(message)))
    devices = json.loads(payload["result"]["content"][0]["text"])
    assert len(devices) >= 7
    assert payload["result"]["isError"] is False


def test_notification_gets_202(http_base):
    status, _ = _rpc_http(http_base, {"jsonrpc": "2.0",
                                      "method": "notifications/initialized"})
    assert status == 202


# -- malformed requests ------------------------------------------------------------


def test_bad_json_is_400(http_base):
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {TOKEN}"}
    status, _ = _request(http_base + "/mcp", data=b"{not json",
                         headers=headers)
    assert status == 400


def test_oversized_body_is_413(http_base):
    conn = http.client.HTTPConnection("127.0.0.1",
                                      int(http_base.rsplit(":", 1)[1]),
                                      timeout=10)
    try:
        conn.putrequest("POST", "/mcp")
        conn.putheader("Authorization", f"Bearer {TOKEN}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
        conn.endheaders(b"{}")  # declared length is what the gate checks
        response = conn.getresponse()
        response.read()
        assert response.status == 413
    finally:
        conn.close()


def test_unknown_path_is_404(http_base):
    status, _ = _request(http_base + "/nope")
    assert status == 404

"""MCP stdio robustness + encoding (v0.10 line B2).

handle() must answer malformed envelopes with JSON-RPC errors instead
of raising, run_stdio must survive bad lines without ending the
stream, and the default stdio streams must be UTF-8 with LF endings
even where the platform text streams would use a locale code page
(Windows pipes) - Chinese device names must round-trip.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from omnibutler.mcp_server.server import create_server, run_stdio


@pytest.fixture()
def server(manager, engine):
    return create_server(manager, engine=engine,
                         confirmations=engine.confirmations)


def _lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# -- handle() envelope validation ------------------------------------------------


def test_handle_rejects_non_dict_message(server):
    for bad in ([1, 2, 3], "hello", 42, None):
        response = server.handle(bad)
        assert response is not None
        assert response["error"]["code"] == -32600
        assert response["id"] is None


def test_handle_rejects_non_dict_params(server):
    response = server.handle({"jsonrpc": "2.0", "id": 9,
                              "method": "ping", "params": [1, 2]})
    assert response["error"]["code"] == -32600
    assert response["id"] == 9


def test_handle_rejects_non_dict_tool_arguments(server):
    response = server.handle({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "list_devices", "arguments": ["room"]},
    })
    assert response["error"]["code"] == -32602
    assert response["id"] == 4


def test_handle_still_serves_valid_messages(server):
    response = server.handle({"jsonrpc": "2.0", "id": 1,
                              "method": "ping", "params": {}})
    assert response == {"jsonrpc": "2.0", "id": 1, "result": {}}


# -- run_stdio stream survival -----------------------------------------------------


def test_run_stdio_answers_bad_lines_and_keeps_going(server):
    incoming = io.StringIO("\n".join([
        "this is not json",
        "[1, 2, 3]",
        json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}),
        "",
    ]))
    outgoing = io.StringIO()
    run_stdio(server, stdin=incoming, stdout=outgoing)
    responses = _lines(outgoing.getvalue())
    assert [r["error"]["code"] for r in responses[:2]] == [-32700, -32600]
    assert responses[0]["id"] is None
    assert responses[2] == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_run_stdio_survives_a_dispatch_explosion(server, monkeypatch):
    real_handle = server.handle

    def flaky(message):
        if isinstance(message, dict) and message.get("method") == "boom":
            raise RuntimeError("kaboom")
        return real_handle(message)

    monkeypatch.setattr(server, "handle", flaky)
    incoming = io.StringIO("\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "boom"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
    ]))
    outgoing = io.StringIO()
    run_stdio(server, stdin=incoming, stdout=outgoing)
    responses = _lines(outgoing.getvalue())
    assert responses[0]["error"]["code"] == -32603
    assert responses[0]["id"] == 1
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


# -- run_stdio default streams are UTF-8 over the raw buffers ----------------------


class _FakeTextStream:
    """Stands in for sys.stdin/sys.stdout: only a .buffer, like a pipe."""

    def __init__(self, buffer: io.BytesIO):
        self.buffer = buffer


def test_run_stdio_default_streams_are_utf8(server, monkeypatch):
    request = {
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "get_device_state",
                   "arguments": {"device_id": "客厅灯"}},
    }
    in_buffer = io.BytesIO(
        (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
    out_buffer = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", _FakeTextStream(in_buffer))
    monkeypatch.setattr(sys, "stdout", _FakeTextStream(out_buffer))

    run_stdio(server)  # no stream args: must wrap the raw buffers

    raw = out_buffer.getvalue()
    text = raw.decode("utf-8")  # strict: a code-page write would break
    assert "客厅灯" in text  # the unknown-device error echoes the id
    assert b"\r\n" not in raw  # single LF line endings everywhere
    assert raw.endswith(b"\n")
    responses = _lines(text)
    assert responses[0]["id"] == 5
    assert responses[0]["result"]["isError"] is True

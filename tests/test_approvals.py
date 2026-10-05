"""Human-only approval surfaces: approvals web page + macOS dialogs.

v0.3 removed approval from MCP entirely; these tests pin down the two
out-of-band replacements added afterwards:

* the local approvals web page (token-gated, engine + audit path shared
  with ``tob confirm`` / ``tob reject``, agent ``web:human``), and
* the macOS dialog notifier + queue watcher (agent ``macos:human``).

Neither surface may ever let a non-human approve anything: no token, no
service; a dialog nobody answers changes nothing.
"""

import http.client
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from omnibutler import approvals_web, cli, notify_macos

TOKEN = "test-approvals-token"


def _seed(queue, **overrides):
    kwargs = dict(
        device_id="garage_door", kind="call_action", name="open", params={},
        requested_by="scene:garage-arrival", scene="garage-arrival",
        risk="high",
    )
    kwargs.update(overrides)
    return queue.add(**kwargs)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# -- approvals web page ---------------------------------------------------------

@pytest.fixture()
def web(engine, manager):
    httpd = approvals_web.create_http_server(
        engine, engine.confirmations, manager,
        host="127.0.0.1", port=0, token=TOKEN,
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _request(port, method, path, *, bearer=TOKEN, lang=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    if lang is not None:
        headers["Accept-Language"] = lang
    conn.request(method, path, headers=headers)
    response = conn.getresponse()
    body = response.read().decode("utf-8")
    conn.close()
    return response.status, body


def test_approvals_server_refuses_to_start_without_token(
        engine, manager, monkeypatch):
    monkeypatch.delenv(approvals_web.TOKEN_ENV_VAR, raising=False)
    with pytest.raises(RuntimeError, match="OMNIBUTLER_APPROVALS_TOKEN"):
        approvals_web.create_http_server(
            engine, engine.confirmations, manager, token=None)
    with pytest.raises(RuntimeError):
        approvals_web.create_http_server(
            engine, engine.confirmations, manager, token="")


def test_approvals_page_requires_token(web, engine, manager):
    item = _seed(engine.confirmations)

    assert _request(web, "GET", "/", bearer=None)[0] == 401
    assert _request(web, "GET", "/", bearer="wrong-token")[0] == 401
    status, _ = _request(web, "POST", f"/approve/{item.id}", bearer=None)
    assert status == 401

    # Nothing was approved behind the 401s.
    assert engine.confirmations.get(item.id).status == "pending"
    assert manager.get_state("garage_door")["open_close"] is False


def test_approvals_page_lists_pending(web, engine):
    item = _seed(engine.confirmations)

    status, body = _request(web, "GET", "/", lang="zh")
    assert status == 200
    assert item.id in body
    assert "garage_door" in body
    assert "garage-arrival" in body
    assert "批准执行" in body and "拒绝" in body
    # The buttons post to the per-item endpoints, token in the action URL.
    assert f"/approve/{item.id}?token={TOKEN}" in body
    assert f"/reject/{item.id}?token={TOKEN}" in body

    # Query-string token works too (that is how a human opens the page).
    status, _ = _request(web, "GET", f"/?token={TOKEN}", bearer=None)
    assert status == 200


def test_web_approve_executes_and_audits(web, engine, manager):
    item = _seed(engine.confirmations)
    assert manager.get_state("garage_door")["open_close"] is False

    # Exactly what the page's form does: POST, token in the query string.
    status, body = _request(
        web, "POST", f"/approve/{item.id}?token={TOKEN}", bearer=None,
        lang="zh")
    assert status == 200
    assert "已批准并执行" in body

    assert manager.get_state("garage_door")["open_close"] is True
    assert engine.confirmations.get(item.id).status == "confirmed"
    approvals = [e for e in manager.audit.read_all()
                 if e["action"] == "confirmation:approved"]
    assert len(approvals) == 1
    assert approvals[0]["agent"] == "web:human"
    assert approvals[0]["params"]["confirmation_id"] == item.id


def test_web_reject_then_cannot_approve(web, engine, manager):
    item = _seed(engine.confirmations)

    status, body = _request(web, "POST", f"/reject/{item.id}", lang="zh")
    assert status == 200
    assert "已拒绝" in body
    assert engine.confirmations.get(item.id).status == "rejected"
    assert manager.get_state("garage_door")["open_close"] is False
    rejections = [e for e in manager.audit.read_all()
                  if e["action"] == "confirmation:rejected"]
    assert len(rejections) == 1
    assert rejections[0]["agent"] == "web:human"

    # A rejected item is gone for good: approving afterwards is a no-op.
    status, body = _request(web, "POST", f"/approve/{item.id}")
    assert status == 200
    assert "no pending confirmation" in body
    assert manager.get_state("garage_door")["open_close"] is False


def test_approvals_page_escapes_html(web, engine):
    _seed(
        engine.confirmations,
        device_id='evil"><img src=x onerror=alert(1)>',
        scene='<script>alert("xss")</script>',
    )
    status, body = _request(web, "GET", "/")
    assert status == 200
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body
    assert "<img src=x" not in body
    assert "&lt;img" in body


# -- pending watcher --------------------------------------------------------------

def test_watcher_baseline_does_not_notify(engine):
    queue = engine.confirmations
    _seed(queue)  # already waiting before the watcher starts
    calls = []
    watcher = notify_macos.PendingWatcher(queue, calls.append)
    assert watcher.poll_once() == []
    assert calls == []


def test_watcher_notifies_new_items_once(engine):
    queue = engine.confirmations
    calls = []
    watcher = notify_macos.PendingWatcher(queue, calls.append)
    assert watcher.poll_once() == []  # baseline over an empty queue

    item = _seed(queue)
    notified = watcher.poll_once()
    assert [i.id for i in notified] == [item.id]
    assert [c.id for c in calls] == [item.id]

    # Same item, next poll: no second popup.
    assert watcher.poll_once() == []
    assert len(calls) == 1

    item2 = _seed(queue, name="close")
    watcher.poll_once()
    assert [c.id for c in calls] == [item.id, item2.id]


# -- macOS dialog notifier ----------------------------------------------------------

class FakeRunner:
    """subprocess.run stand-in: records argv, replays canned stdout."""

    def __init__(self, stdout="", returncode=0, exc=None):
        self.stdout = stdout
        self.returncode = returncode
        self.exc = exc
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(stdout=self.stdout,
                               returncode=self.returncode, stderr="")


def _notifier(engine, manager, runner):
    return notify_macos.macos_dialog_notifier(
        engine, engine.confirmations, manager,
        runner=runner, platform="darwin",
    )


def test_macos_notifier_requires_darwin(engine, manager):
    with pytest.raises(RuntimeError, match="darwin"):
        notify_macos.macos_dialog_notifier(
            engine, engine.confirmations, manager,
            runner=FakeRunner(), platform="linux",
        )
    assert notify_macos.is_supported() is (sys.platform == "darwin")


def test_macos_dialog_approve_executes_and_audits(engine, manager):
    item = _seed(engine.confirmations)
    runner = FakeRunner(stdout="button returned:批准执行, gave up:false")
    notify = _notifier(engine, manager, runner)

    assert notify(item) == "approved"
    assert manager.get_state("garage_door")["open_close"] is True
    assert engine.confirmations.get(item.id).status == "confirmed"
    approvals = [e for e in manager.audit.read_all()
                 if e["action"] == "confirmation:approved"]
    assert len(approvals) == 1
    assert approvals[0]["agent"] == "macos:human"
    # The dialog itself: osascript, reject is the default button.
    argv = runner.calls[0]
    assert argv[0] == "osascript"
    assert 'default button "拒绝"' in argv[2]
    assert "giving up after 120" in argv[2]


def test_macos_dialog_reject(engine, manager):
    item = _seed(engine.confirmations)
    runner = FakeRunner(stdout="button returned:拒绝, gave up:false")
    notify = _notifier(engine, manager, runner)

    assert notify(item) == "rejected"
    assert engine.confirmations.get(item.id).status == "rejected"
    assert manager.get_state("garage_door")["open_close"] is False
    rejections = [e for e in manager.audit.read_all()
                  if e["action"] == "confirmation:rejected"]
    assert len(rejections) == 1
    assert rejections[0]["agent"] == "macos:human"


def test_macos_dialog_gave_up_leaves_item_pending(engine, manager):
    item = _seed(engine.confirmations)
    notify = _notifier(engine, manager, FakeRunner(stdout="gave up:true"))
    assert notify(item) is None
    assert engine.confirmations.get(item.id).status == "pending"
    assert manager.get_state("garage_door")["open_close"] is False

    # osascript itself timing out is also just "nobody answered".
    runner = FakeRunner(exc=subprocess.TimeoutExpired("osascript", 180))
    notify = _notifier(engine, manager, runner)
    assert notify(item) is None
    assert engine.confirmations.get(item.id).status == "pending"


def test_applescript_escaping():
    assert notify_macos.escape_applescript('a"b\\c\nd') == 'a\\"b\\\\c d'

    queue_item = SimpleNamespace(
        id="cfm-0001", device_id='bad"actor\\x', kind="call_action",
        name="open", value=None, params={}, requested_by="scene:x",
        scene="scene:x", risk="high",
    )
    script = notify_macos.build_dialog_script(queue_item)
    assert 'bad\\"actor\\\\x' in script
    assert 'bad"actor' not in script


# -- CLI / daemon wiring -------------------------------------------------------------

def test_cli_wiring_parses():
    parser = cli.build_parser()
    args = parser.parse_args(["approvals", "--port", "8770"])
    assert args.func is cli.cmd_approvals
    assert args.host == "127.0.0.1" and args.port == 8770

    args = parser.parse_args(
        ["run", "--approvals-port", "8766", "--notify"])
    assert args.func is cli.cmd_run
    assert args.approvals_port == 8766 and args.notify is True


def test_daemon_serves_approvals_in_same_process(engine, manager, monkeypatch):
    from omnibutler.daemon import Daemon

    monkeypatch.setenv(approvals_web.TOKEN_ENV_VAR, TOKEN)
    item = _seed(engine.confirmations)
    runtime = SimpleNamespace(engine=engine, manager=manager,
                              confirmations=engine.confirmations)
    daemon = Daemon(runtime, approvals_port=_free_port(), tick_seconds=0.05)
    port = daemon.approvals_port
    thread = threading.Thread(target=daemon.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        body = ""
        while time.time() < deadline:
            try:
                status, body = _request(port, "GET", "/")
                if status == 200:
                    break
            except OSError:
                time.sleep(0.05)
        assert item.id in body
        status, _ = _request(port, "POST", f"/approve/{item.id}")
        assert status == 200
        assert manager.get_state("garage_door")["open_close"] is True
    finally:
        daemon.stop()
        thread.join(timeout=5)
    assert not thread.is_alive()

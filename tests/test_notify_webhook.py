"""Webhook approval notifications: resolution, payload, daemon wiring,
failure isolation, and the doctor presence-only check.

The promise under test: when a high-risk action joins the confirmation
queue, a configured webhook gets exactly one POST; an unconfigured one
stays silent; a broken one never hurts the daemon. Approving is still
human-only and out-of-band - nothing here (or in the module) can
confirm anything.
"""

import json
import urllib.request
from pathlib import Path

from omnibutler import notify_webhook
from omnibutler.config import load_config
from omnibutler.core.audit import AuditLog
from omnibutler.core.confirmations import ConfirmationQueue
from omnibutler.core.manager import DeviceManager
from omnibutler.daemon import Daemon, run_daemon
from omnibutler.doctor import check_all, format_report
from omnibutler.drivers.mock import MockDriver
from omnibutler.runtime import Runtime
from omnibutler.scenes.engine import SceneEngine

HOOK_URL = "https://ntfy.example/approvals-topic"
SECRET_URL = "https://ntfy.example/do-not-print-this-topic"


# -- fakes -----------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingOpener:
    """urlopen stand-in: records (request, timeout), replies `status`."""

    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def __call__(self, request, timeout=None):
        self.calls.append((request, timeout))
        return FakeResponse(self.status)


class FailingOpener:
    def __call__(self, request, timeout=None):
        raise OSError("connection refused")


class RecordingNotifier:
    def __init__(self):
        self.items = []

    def notify(self, item):
        self.items.append(item)
        return True


def _queue(tmp_path) -> ConfirmationQueue:
    return ConfirmationQueue(path=tmp_path / "confirmations.json")


def _park(queue, device="garage_door"):
    return queue.add(
        device, "call_action", "open", requested_by="scene:garage-arrival",
        scene="garage-arrival", risk="high",
    )


def _runtime(tmp_path):
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    driver = MockDriver()
    manager = DeviceManager(drivers={driver.name: driver}, audit=audit)
    confirmations = _queue(tmp_path)
    engine = SceneEngine(manager, confirmations)
    return Runtime(manager=manager, engine=engine,
                   confirmations=confirmations, bus=manager.bus,
                   driver_name=driver.name)


# -- resolution --------------------------------------------------------------

def test_unconfigured_resolves_to_none_and_builds_no_notifier():
    opener = RecordingOpener()
    assert notify_webhook.resolve_webhook_url({}, environ={}) is None
    assert notify_webhook.make_webhook_notifier(
        {}, environ={}, opener=opener) is None
    assert opener.calls == []


def test_env_var_wins_over_config_file():
    config = {"notify": {"webhook_url": "https://config.example/hook"}}
    environ = {"OMNIBUTLER_NOTIFY_WEBHOOK_URL": HOOK_URL}
    assert notify_webhook.resolve_webhook_url(
        config, environ=environ) == HOOK_URL


def test_config_section_is_used_without_env_var():
    config = {"notify": {"webhook_url": HOOK_URL}}
    assert notify_webhook.resolve_webhook_url(
        config, environ={}) == HOOK_URL


def test_config_webhook_url_may_be_an_env_reference():
    config = {"notify": {"webhook_url": "env:MY_HOOK_URL"}}
    environ = {"MY_HOOK_URL": HOOK_URL}
    assert notify_webhook.resolve_webhook_url(
        config, environ=environ) == HOOK_URL
    # Reference present but the variable unset: counts as unconfigured.
    assert notify_webhook.resolve_webhook_url(config, environ={}) is None


def test_example_config_carries_a_notify_section():
    path = Path(__file__).resolve().parent.parent / "config.example.json"
    config = load_config(path)  # parses + version-validates (still v1)
    assert config["version"] == 1
    assert "webhook_url" in config["notify"]


# -- payload + sending ---------------------------------------------------------

def test_payload_carries_the_approval_facts(tmp_path):
    item = _park(_queue(tmp_path))
    payload = notify_webhook.build_payload(item)
    assert payload["event"] == "approval_requested"
    assert payload["id"] == item.id
    assert payload["device"] == "garage_door"
    assert payload["device_id"] == "garage_door"
    assert payload["action"] == "call garage_door.open({})"
    assert payload["name"] == "open"
    assert payload["risk"] == "high"
    assert payload["scene"] == "garage-arrival"
    assert payload["created_at"] == item.created_at
    assert payload["created_at_iso"].startswith("20")  # ISO-8601 UTC
    assert item.id in payload["message"]


def test_send_posts_json_once_with_default_timeout(tmp_path):
    item = _park(_queue(tmp_path))
    opener = RecordingOpener()
    notifier = notify_webhook.WebhookNotifier(HOOK_URL, opener=opener)
    assert notifier.notify(item) is True
    assert len(opener.calls) == 1
    request, timeout = opener.calls[0]
    assert isinstance(request, urllib.request.Request)
    assert request.full_url == HOOK_URL
    assert request.get_method() == "POST"
    assert request.headers["Content-type"] == "application/json"
    assert timeout == notify_webhook.DEFAULT_TIMEOUT == 5.0
    body = json.loads(request.data.decode("utf-8"))
    assert body["event"] == "approval_requested"
    assert body["id"] == item.id


def test_send_failure_is_swallowed_and_reported_on_stderr(capsys):
    payload = {"event": "approval_requested", "id": "cfm-0001"}
    assert notify_webhook.send_webhook(
        HOOK_URL, payload, opener=FailingOpener()) is False
    assert "webhook" in capsys.readouterr().err


def test_send_non_2xx_is_a_failure_not_an_exception(capsys):
    payload = {"event": "approval_requested", "id": "cfm-0001"}
    opener = RecordingOpener(status=500)
    assert notify_webhook.send_webhook(HOOK_URL, payload, opener=opener) is False
    assert "500" in capsys.readouterr().err


# -- daemon wiring -------------------------------------------------------------

def test_daemon_announces_each_new_pending_once(tmp_path):
    runtime = _runtime(tmp_path)
    notifier = RecordingNotifier()
    daemon = Daemon(runtime, webhook_notifier=notifier)
    first = _park(runtime.confirmations)
    daemon._maybe_notify_approvals()
    daemon._maybe_notify_approvals()  # same tick again: no re-send
    assert [i.id for i in notifier.items] == [first.id]
    second = _park(runtime.confirmations, device="front_lock")
    daemon._maybe_notify_approvals()
    assert [i.id for i in notifier.items] == [first.id, second.id]
    assert daemon.stats["webhook_notifications"] == 2


def test_daemon_loop_announces_queued_item_exactly_once(tmp_path):
    runtime = _runtime(tmp_path)
    item = _park(runtime.confirmations)
    notifier = RecordingNotifier()
    stats = run_daemon(
        runtime, poll_interval=3600, tick_seconds=1, max_ticks=5,
        sleep_fn=lambda _s: None, webhook_notifier=notifier,
    )
    assert [i.id for i in notifier.items] == [item.id]
    assert stats["webhook_notifications"] == 1


def test_fresh_daemon_reannounces_still_pending_once(tmp_path):
    # The announced-id set is memory-only by design: a restarted daemon
    # announces a still-pending item once more rather than never.
    runtime = _runtime(tmp_path)
    _park(runtime.confirmations)
    first, second = RecordingNotifier(), RecordingNotifier()
    Daemon(runtime, webhook_notifier=first)._maybe_notify_approvals()
    Daemon(runtime, webhook_notifier=second)._maybe_notify_approvals()
    assert len(first.items) == 1
    assert len(second.items) == 1


def test_broken_webhook_does_not_break_the_daemon_loop(tmp_path):
    runtime = _runtime(tmp_path)
    _park(runtime.confirmations)
    notifier = notify_webhook.WebhookNotifier(HOOK_URL, opener=FailingOpener())
    stats = run_daemon(
        runtime, poll_interval=3600, tick_seconds=1, max_ticks=3,
        sleep_fn=lambda _s: None, webhook_notifier=notifier,
    )
    assert stats["ticks"] == 3
    assert stats["webhook_notifications"] == 0


def test_daemon_without_webhook_configured_stays_silent(
        tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIBUTLER_NOTIFY_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("OMNIBUTLER_CONFIG", str(tmp_path / "absent.json"))
    runtime = _runtime(tmp_path)
    _park(runtime.confirmations)
    stats = run_daemon(
        runtime, poll_interval=3600, tick_seconds=1, max_ticks=3,
        sleep_fn=lambda _s: None,
    )
    assert stats["webhook_notifications"] == 0


# -- doctor --------------------------------------------------------------------

def _webhook_result(results):
    return next(r for r in results if r.name == "notify-webhook")


def test_doctor_reports_configured_without_showing_the_url(tmp_path):
    config = {"notify": {"webhook_url": SECRET_URL}}
    results = check_all(config, environ={}, scenes_dir=tmp_path)
    result = _webhook_result(results)
    assert result.status == "ok"
    assert SECRET_URL not in result.detail
    assert SECRET_URL not in format_report(results)


def test_doctor_sees_the_env_var_without_showing_it(tmp_path):
    environ = {"OMNIBUTLER_NOTIFY_WEBHOOK_URL": SECRET_URL}
    results = check_all({}, environ=environ, scenes_dir=tmp_path)
    result = _webhook_result(results)
    assert result.status == "ok"
    assert SECRET_URL not in format_report(results)


def test_doctor_warns_when_no_webhook_is_configured(tmp_path):
    results = check_all({}, environ={}, scenes_dir=tmp_path)
    assert _webhook_result(results).status == "warn"

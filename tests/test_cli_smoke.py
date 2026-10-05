"""End-to-end smoke tests for the ``tob`` CLI (omnibutler/cli.py).

The deep behaviour of every subsystem has its own test module; this file
only checks that the *command wiring* works: each major subcommand runs
against the mock driver, prints the content a user would look for, and
returns the right exit code. Interactive commands (fetch-keys prompts
are stubbed via monkeypatched cloud functions; setup-secret via a
stubbed getpass) and the long-running servers (run / mcp / approvals /
gateway) are the only ones not driven end-to-end here.
"""

import getpass
import json
import re
import time

import pytest

from omnibutler import __version__, cli
from omnibutler.cloud_keys import CloudKeyError
from omnibutler.core.confirmations import ConfirmationQueue
from omnibutler.core.streams import DataStream, StreamStore


@pytest.fixture(autouse=True)
def _cli_env(tmp_path, monkeypatch):
    """Keep audit + config writes inside the test's tmp dir.

    (The confirmation queue and stream store already live under
    $OMNIBUTLER_STATE_DIR, which conftest points at tmp_path.)
    """
    monkeypatch.setenv("TOB_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("OMNIBUTLER_CONFIG", str(tmp_path / "config.json"))


def _run(capsys, argv):
    rc = cli.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# -- parser / small pure helpers ----------------------------------------------


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"tob {__version__}"


def test_parse_value():
    assert cli.parse_value("true") is True
    assert cli.parse_value("OFF") is False
    assert cli.parse_value("24") == 24
    assert cli.parse_value("2.5") == 2.5
    assert cli.parse_value('{"a": 1}') == {"a": 1}
    assert cli.parse_value("auto") == "auto"


def test_format_wait():
    now = time.time()
    assert cli._format_wait(now - 30) == "30s"
    assert cli._format_wait(now - 150) == "2m30s"
    assert cli._format_wait(now - 3723) == "1h02m"
    assert cli._format_wait(now - 90061) == "1d1h"


# -- devices / state / set ------------------------------------------------------


def test_devices_lists_the_mock_home(capsys):
    rc, out, _ = _run(capsys, ["devices"])
    assert rc == 0
    for device_id in ("living_ac", "bedroom_ac", "air_purifier", "living_light",
                      "living_room_vacuum", "bedroom_curtain", "bathroom_scale",
                      "garage_door"):
        assert device_id in out
    assert "Living Room AC" in out
    assert "driver=mock" in out
    assert "risk:high" in out  # the garage door line is flagged


def test_devices_room_filter_and_no_match(capsys):
    rc, out, _ = _run(capsys, ["devices", "--room", "bedroom"])
    assert rc == 0
    assert "bedroom_ac" in out and "bedroom_curtain" in out
    assert "living_ac" not in out

    rc, out, _ = _run(capsys, ["devices", "--room", "nowhere"])
    assert rc == 0
    assert "(no devices)" in out


def test_devices_with_state(capsys):
    rc, out, _ = _run(capsys, ["devices", "--state"])
    assert rc == 0
    assert "state:" in out
    assert '"pm25": 12' in out  # the purifier's bundled state


def test_state_command(capsys):
    rc, out, _ = _run(capsys, ["state", "bedroom_ac"])
    assert rc == 0
    assert "Bedroom AC (bedroom_ac) room=bedroom risk=low" in out
    assert "  onoff = False" in out
    assert "  target_temperature = 26" in out


def test_state_unknown_device_errors(capsys):
    rc, _, err = _run(capsys, ["state", "no_such_device"])
    assert rc == 1
    assert "error:" in err


def test_set_command(capsys):
    rc, out, _ = _run(capsys, ["set", "living_ac", "target_temperature", "24"])
    assert rc == 0
    assert "ok: living_ac.target_temperature = 24" in out
    assert '"target_temperature": 24' in out

    rc, out, _ = _run(capsys, ["set", "living_light", "onoff", "on"])
    assert rc == 0
    assert "ok: living_light.onoff = True" in out


# -- scenes ---------------------------------------------------------------------


def test_scenes_lists_bundled_examples(capsys):
    rc, out, _ = _run(capsys, ["scenes"])
    assert rc == 0
    for name in ("arrive-home", "leave-home-check", "sleep-mode",
                 "air-quality-guard", "garage-arrival", "weekday-morning"):
        assert name in out
    assert re.search(r"\d+ scene\(s\) valid", out)


def test_scenes_invalid_dir_fails(capsys, tmp_path):
    (tmp_path / "broken.yaml").write_text("name: broken\n", encoding="utf-8")
    rc, _, err = _run(capsys, ["scenes", "--dir", str(tmp_path)])
    assert rc == 1
    assert "scene validation failed" in err


def test_scenes_empty_dir(capsys, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    rc, out, _ = _run(capsys, ["scenes", "--dir", str(empty)])
    assert rc == 0
    assert "(no scenes in" in out


# -- simulate -------------------------------------------------------------------


def test_simulate_geofence_arrive_home(capsys):
    rc, out, _ = _run(capsys, ["simulate"])
    assert rc == 0
    assert "event: geofence: phone entered zone 'home'" in out
    assert "scene evaluated: arrive-home" in out
    assert "[EXECUTED] arrive-home" in out
    assert "device state after:" in out


def test_simulate_leave_home(capsys):
    rc, out, _ = _run(capsys, ["simulate", "--event", "leave"])
    assert rc == 0
    assert "phone left zone 'home'" in out
    assert "[EXECUTED] leave-home-check" in out


def test_simulate_pm25_spike(capsys):
    rc, out, _ = _run(capsys, ["simulate", "--event", "pm25"])
    assert rc == 0
    assert "state_change: air_purifier.pm25 = 86" in out
    assert "[EXECUTED] air-quality-guard" in out
    assert '"mode": "turbo"' in out  # purifier state after the scene ran


def test_simulate_garage_queues_high_risk_action(capsys):
    rc, out, _ = _run(capsys, ["simulate", "--event", "garage"])
    assert rc == 0
    assert "garage_gate" in out
    assert "[QUEUED (needs confirmation)] garage-arrival" in out
    assert "pending confirmations:" in out
    assert "garage_door.open" in out


def test_simulate_schedule_sleep_mode(capsys):
    rc, out, _ = _run(capsys, ["simulate", "--event", "schedule"])
    assert rc == 0
    assert "schedule: time = 22:30" in out
    assert "[EXECUTED] sleep-mode" in out


def test_simulate_scene_filter(capsys):
    rc, out, _ = _run(capsys, ["simulate", "--event", "pm25",
                               "--scene", "air-quality-guard"])
    assert rc == 0
    assert "[EXECUTED] air-quality-guard" in out
    assert "arrive-home" not in out


def test_simulate_unknown_scene_errors(capsys):
    rc, _, err = _run(capsys, ["simulate", "--scene", "nope"])
    assert rc == 1
    assert "unknown scene 'nope'" in err


# -- pending / confirm / reject ---------------------------------------------------


def _seed_pending(kind="call_action", name="open", value=None):
    return ConfirmationQueue().add(
        device_id="garage_door", kind=kind, name=name, value=value, params={},
        requested_by="scene:garage-arrival", scene="garage-arrival", risk="high",
    )


def test_pending_empty(capsys):
    rc, out, _ = _run(capsys, ["pending"])
    assert rc == 0
    assert "(no pending confirmations)" in out


def test_pending_confirm_roundtrip(capsys):
    item = _seed_pending()
    rc, out, _ = _run(capsys, ["pending"])
    assert rc == 0
    assert "1 pending confirmation(s):" in out
    assert item.id in out
    assert "call garage_door.open" in out
    assert "risk=high" in out
    assert "waiting=" in out

    rc, out, _ = _run(capsys, ["confirm", item.id])
    assert rc == 0
    assert f"confirmed and executed: {item.id}" in out
    assert '"open_close": true' in out  # the door actually opened

    rc, out, _ = _run(capsys, ["pending"])
    assert rc == 0
    assert "(no pending confirmations)" in out


def test_pending_describes_set_property_items(capsys):
    _seed_pending(kind="set_property", name="onoff", value=True)
    rc, out, _ = _run(capsys, ["pending"])
    assert rc == 0
    assert "set garage_door.onoff = True" in out


def test_reject_roundtrip(capsys):
    item = _seed_pending()
    rc, out, _ = _run(capsys, ["reject", item.id])
    assert rc == 0
    assert f"rejected: {item.id}" in out
    assert "(nothing was executed)" in out
    assert ConfirmationQueue().get(item.id).status == "rejected"


# -- doctor -----------------------------------------------------------------------


def test_doctor_runs_and_reports(capsys, monkeypatch):
    # Keep the report independent of whatever the host happens to have
    # configured: no config file (fixture points at a missing one) and
    # no vendor credentials in the environment.
    for var in ("HA_URL", "HA_TOKEN", "OMNIBUTLER_GATEWAY_TOKEN",
                "OMNIBUTLER_NOTIFY_WEBHOOK_URL", "XIAOMI_CLOUD_USERNAME",
                "XIAOMI_CLOUD_PASSWORD", "TUYA_ACCESS_ID", "TUYA_ACCESS_SECRET"):
        monkeypatch.delenv(var, raising=False)
    rc, out, _ = _run(capsys, ["doctor"])
    assert rc in (0, 1)  # 1 only if a check fails; the report must still print
    assert out.startswith("OmniButler health check")
    assert "config" in out
    assert "scenes" in out
    assert re.search(r"\d+ ok, \d+ warning\(s\), \d+ failure\(s\)", out)


# -- streams ----------------------------------------------------------------------


def test_streams_empty(capsys):
    rc, out, _ = _run(capsys, ["streams"])
    assert rc == 0
    assert "no data streams yet" in out


def _seed_stream():
    store = StreamStore()
    store.append(
        "phone-steps", 5231, ts=1_700_000_000.0,
        stream=DataStream(id="phone-steps", kind="health.steps",
                          source="phone", unit="count"),
    )


def test_streams_list_and_history(capsys):
    _seed_stream()
    rc, out, _ = _run(capsys, ["streams"])
    assert rc == 0
    assert "phone-steps (health.steps, source=phone)" in out
    assert "latest: 5231" in out

    rc, out, _ = _run(capsys, ["streams", "phone-steps"])
    assert rc == 0
    assert "5231" in out

    rc, out, _ = _run(capsys, ["streams", "no-such-stream"])
    assert rc == 0
    assert "no data for stream 'no-such-stream'" in out


# -- discover / onboard -------------------------------------------------------------


def test_discover_smoke(capsys):
    rc, out, _ = _run(capsys, ["discover"])
    assert rc == 0
    assert "mock driver:" in out
    assert "living_ac" in out
    assert "homeassistant driver:" in out


def test_onboard_smoke(capsys, tmp_path):
    rc, out, _ = _run(capsys, ["onboard"])
    assert rc == 0
    assert "id=garage_door" in out

    draft = tmp_path / "draft.json"
    rc, out, _ = _run(capsys, ["onboard", "--write-draft", str(draft)])
    assert rc == 0
    assert "config draft written to" in out
    assert isinstance(json.loads(draft.read_text(encoding="utf-8")), dict)


# -- setup / setup-secret -------------------------------------------------------------


def test_setup_guide_command(capsys):
    rc, out, _ = _run(capsys, ["setup", "miio"])
    assert rc == 0
    assert "小米设备（miIO token）" in out


def test_setup_secret_stores_and_refuses_empty(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "tok-abc-123")
    rc, out, _ = _run(capsys, ["setup-secret", "miio.devices.0.token"])
    assert rc == 0
    assert "stored miio.devices.0.token in" in out
    assert "value not shown" in out
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["miio"]["devices"][0]["token"] == "tok-abc-123"

    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "")
    rc, _, err = _run(capsys, ["setup-secret", "miio.devices.0.token"])
    assert rc == 1
    assert "nothing entered; aborted" in err


# -- fetch-keys (cloud functions stubbed: no network, no prompts) ----------------------


def _fake_xiaomi_devices():
    return [{"name": "Living AC", "model": "xiaomi.acpartner.r2105",
             "did": "123456789", "ip": "192.168.1.50",
             "token": "0123456789abcdef0123456789abcdef"}]


def test_fetch_keys_xiaomi_prints_and_stores(capsys, monkeypatch, tmp_path):
    import omnibutler.cloud_keys as cloud_keys

    monkeypatch.setattr(cloud_keys, "fetch_xiaomi_tokens",
                        lambda username, password: _fake_xiaomi_devices())
    monkeypatch.setenv("XIAOMI_USERNAME", "user@example.com")
    monkeypatch.setenv("XIAOMI_PASSWORD", "hidden")

    rc, out, _ = _run(capsys, ["fetch-keys", "xiaomi"])
    assert rc == 0
    assert "Living AC (xiaomi.acpartner.r2105) ip=192.168.1.50" in out
    assert "token=...cdef" in out
    assert "re-run with --store" in out

    rc, out, _ = _run(capsys, ["fetch-keys", "xiaomi", "--store"])
    assert rc == 0
    assert "stored 1 device(s) in" in out
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["miio"]["devices"][0]["host"] == "192.168.1.50"


def test_fetch_keys_xiaomi_no_devices(capsys, monkeypatch):
    import omnibutler.cloud_keys as cloud_keys

    monkeypatch.setattr(cloud_keys, "fetch_xiaomi_tokens",
                        lambda username, password: [])
    monkeypatch.setenv("XIAOMI_USERNAME", "user@example.com")
    monkeypatch.setenv("XIAOMI_PASSWORD", "hidden")
    rc, out, _ = _run(capsys, ["fetch-keys", "xiaomi"])
    assert rc == 0
    assert "no devices with a local token were returned" in out


def test_fetch_keys_tuya_prints(capsys, monkeypatch):
    import omnibutler.cloud_keys as cloud_keys

    monkeypatch.setattr(
        cloud_keys, "fetch_tuya_local_keys",
        lambda access_id, access_secret, uid: [
            {"name": "Hall Plug", "device_id": "bf123abc",
             "local_key": "0123456789abcdef"}])
    monkeypatch.setenv("TUYA_ACCESS_ID", "id")
    monkeypatch.setenv("TUYA_ACCESS_SECRET", "secret")
    monkeypatch.setenv("TUYA_UID", "uid")
    rc, out, _ = _run(capsys, ["fetch-keys", "tuya"])
    assert rc == 0
    assert "Hall Plug id=bf123abc" in out
    assert "local_key=...cdef" in out


def test_fetch_keys_cloud_error_is_classified(capsys, monkeypatch):
    import omnibutler.cloud_keys as cloud_keys

    def _boom(username, password):
        raise CloudKeyError("auth_failed", "the cloud said no")

    monkeypatch.setattr(cloud_keys, "fetch_xiaomi_tokens", _boom)
    monkeypatch.setenv("XIAOMI_USERNAME", "user@example.com")
    monkeypatch.setenv("XIAOMI_PASSWORD", "hidden")
    rc, _, err = _run(capsys, ["fetch-keys", "xiaomi"])
    assert rc == 1
    assert "error (auth_failed): the cloud said no" in err


def test_merge_config_devices_updates_in_place(tmp_path):
    path = cli._merge_config_devices(
        "miio", [{"host": "192.168.1.50", "token": "aaa"}])
    cli._merge_config_devices(
        "miio", [{"host": "192.168.1.50", "token": "bbb"},
                 {"host": "192.168.1.51", "token": "ccc"}])
    data = json.loads(path.read_text(encoding="utf-8"))
    devices = data["miio"]["devices"]
    assert [d["host"] for d in devices] == ["192.168.1.50", "192.168.1.51"]
    assert devices[0]["token"] == "bbb"  # same host updated, not duplicated
    assert path == tmp_path / "config.json"


# -- audit / backup ---------------------------------------------------------------


def test_audit_command(capsys):
    # No audit file yet (the fixture points TOB_AUDIT_PATH at a fresh tmp path).
    rc, out, _ = _run(capsys, ["audit"])
    assert rc == 0
    assert "(no audit log yet at" in out

    _run(capsys, ["set", "living_ac", "onoff", "true"])  # leaves an audit entry
    rc, out, _ = _run(capsys, ["audit"])
    assert rc == 0
    assert "living_ac" in out
    assert "cli" in out  # the agent column

    rc, out, _ = _run(capsys, ["audit", "--device", "bedroom_ac"])
    assert rc == 0
    assert "(no audit entries match" in out

    rc, _, err = _run(capsys, ["audit", "--last", "0"])
    assert rc == 1
    assert "--last must be at least 1" in err


def test_backup_command(capsys, tmp_path):
    _seed_pending()  # a state file (confirmations.json) to back up
    dest = tmp_path / "backup.tar.gz"
    rc, out, _ = _run(capsys, ["backup", str(dest)])
    assert rc == 0
    assert f"backup written to {dest}" in out
    assert "state/confirmations.json" in out
    assert "warning: the backup includes your config file" in out
    assert dest.exists() and dest.stat().st_size > 0

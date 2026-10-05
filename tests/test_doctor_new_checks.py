"""Doctor's v0.5 checks: config version, Matter controller, Zigbee2MQTT
broker, and the phone-gateway token.

Everything stays on loopback: reachable endpoints are real listening
sockets on 127.0.0.1, unreachable ones are freshly closed ports. No
secret value may ever appear in a result or the formatted report -
the gateway token check reports set / not set only.
"""

import socket
from pathlib import Path

import pytest

from omnibutler.doctor import check_all, format_report

EXAMPLES_SCENES = Path(__file__).resolve().parent.parent / "examples" / "scenes"

_ENV_VARS = [
    "HA_URL", "HA_TOKEN", "MIIO_DEVICES", "MIIO_HOST", "MIIO_TOKEN",
    "TUYA_DEVICES_JSON", "MATTER_SERVER_URL", "Z2M_MQTT_URL",
    "OMNIBUTLER_GATEWAY_TOKEN", "OMNIBUTLER_CONFIG",
]

GATEWAY_TOKEN_VALUE = "gw-secret-value-12345"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def audit_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TOB_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    return tmp_path


def _by_name(results):
    return {r.name: r for r in results}


def _listening_socket():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def _closed_port() -> int:
    sock, port = _listening_socket()
    sock.close()
    return port


# -- config version ------------------------------------------------------------


def test_config_version_line_ok_without_version(audit_dir):
    results = check_all({}, environ={}, scenes_dir=EXAMPLES_SCENES)
    assert _by_name(results)["config-version"].status == "ok"


def test_config_version_line_ok_with_current_version(audit_dir):
    results = check_all({"version": 1}, environ={}, scenes_dir=EXAMPLES_SCENES)
    result = _by_name(results)["config-version"]
    assert result.status == "ok"
    assert "1" in result.detail


def test_config_version_line_fails_for_newer_config(audit_dir):
    results = check_all({"version": 99}, environ={}, scenes_dir=EXAMPLES_SCENES)
    result = _by_name(results)["config-version"]
    assert result.status == "fail"
    assert "newer" in result.detail


def test_config_version_line_fails_for_newer_file_on_disk(audit_dir, tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"version": 99}', encoding="utf-8")
    results = check_all(None, config_path=path, environ={},
                        scenes_dir=EXAMPLES_SCENES)
    by = _by_name(results)
    assert by["config"].status == "fail"  # the loader refuses it too
    assert by["config-version"].status == "fail"


def test_config_version_line_ok_for_unversioned_file_on_disk(audit_dir, tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"ha": {}}', encoding="utf-8")
    results = check_all(None, config_path=path, environ={},
                        scenes_dir=EXAMPLES_SCENES)
    assert _by_name(results)["config-version"].status == "ok"


# -- Matter ----------------------------------------------------------------------


def test_matter_not_configured_is_a_warn(audit_dir):
    results = check_all({}, environ={}, scenes_dir=EXAMPLES_SCENES)
    result = _by_name(results)["matter"]
    assert result.status == "warn"
    assert "not configured" in result.detail


def test_matter_configured_but_unreachable_fails(audit_dir):
    config = {"matter": {"server_url": f"ws://127.0.0.1:{_closed_port()}/ws"}}
    results = check_all(config, environ={}, scenes_dir=EXAMPLES_SCENES,
                        timeout=1.0)
    assert _by_name(results)["matter"].status == "fail"


def test_matter_reachable_via_env_is_ok(audit_dir):
    sock, port = _listening_socket()
    try:
        results = check_all(
            {}, environ={"MATTER_SERVER_URL": f"ws://127.0.0.1:{port}/ws"},
            scenes_dir=EXAMPLES_SCENES, timeout=1.0)
    finally:
        sock.close()
    assert _by_name(results)["matter"].status == "ok"


def test_matter_address_without_host_fails_cleanly(audit_dir):
    config = {"matter": {"server_url": "ws://"}}
    results = check_all(config, environ={}, scenes_dir=EXAMPLES_SCENES,
                        timeout=1.0)
    assert _by_name(results)["matter"].status == "fail"


# -- Zigbee2MQTT -------------------------------------------------------------------


def test_zigbee_not_configured_is_a_warn(audit_dir):
    results = check_all({}, environ={}, scenes_dir=EXAMPLES_SCENES)
    result = _by_name(results)["zigbee2mqtt"]
    assert result.status == "warn"
    assert "not configured" in result.detail


def test_zigbee_broker_unreachable_fails(audit_dir):
    results = check_all(
        {}, environ={"Z2M_MQTT_URL": f"mqtt://127.0.0.1:{_closed_port()}"},
        scenes_dir=EXAMPLES_SCENES, timeout=1.0)
    assert _by_name(results)["zigbee2mqtt"].status == "fail"


def test_zigbee_broker_reachable_from_config_is_ok(audit_dir):
    sock, port = _listening_socket()
    try:
        config = {"zigbee2mqtt": {"mqtt_url": f"mqtt://127.0.0.1:{port}"}}
        results = check_all(config, environ={}, scenes_dir=EXAMPLES_SCENES,
                            timeout=1.0)
    finally:
        sock.close()
    assert _by_name(results)["zigbee2mqtt"].status == "ok"


# -- gateway token -------------------------------------------------------------------


def test_gateway_token_unset_is_a_warn(audit_dir):
    results = check_all({}, environ={}, scenes_dir=EXAMPLES_SCENES)
    assert _by_name(results)["gateway-token"].status == "warn"


def test_gateway_token_set_is_ok_and_never_echoed(audit_dir):
    results = check_all(
        {}, environ={"OMNIBUTLER_GATEWAY_TOKEN": GATEWAY_TOKEN_VALUE},
        scenes_dir=EXAMPLES_SCENES)
    result = _by_name(results)["gateway-token"]
    assert result.status == "ok"
    assert GATEWAY_TOKEN_VALUE not in result.detail
    assert GATEWAY_TOKEN_VALUE not in format_report(results)
    for line in results:
        assert GATEWAY_TOKEN_VALUE not in line.detail

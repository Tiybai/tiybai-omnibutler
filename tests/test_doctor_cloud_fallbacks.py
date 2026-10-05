"""Doctor's cloud-fallback credentials check: presence only."""
from __future__ import annotations

from omnibutler.doctor import check_all, format_report


def _cloud_line(results):
    return next(r for r in results if r.name == "cloud-fallbacks")


def test_warns_when_no_cloud_fallback_configured(tmp_path):
    results = check_all({}, environ={}, scenes_dir=tmp_path)
    line = _cloud_line(results)
    assert line.status == "warn"
    assert "tuya_cloud" in line.detail and "xiaomi_cloud" in line.detail


def test_ok_with_tuya_env_credentials(tmp_path):
    env = {"TUYA_CLOUD_ACCESS_ID": "id-value-123",
           "TUYA_CLOUD_ACCESS_SECRET": "secret-value-456"}
    results = check_all({}, environ=env, scenes_dir=tmp_path)
    line = _cloud_line(results)
    assert line.status == "ok"
    assert "tuya_cloud" in line.detail
    report = format_report(results)
    assert "secret-value-456" not in report
    assert "id-value-123" not in report


def test_ok_with_xiaomi_config_section(tmp_path):
    config = {"xiaomi_cloud": {"username": "user@example.com",
                               "password": "env:SOME_PASSWORD"}}
    results = check_all(config, environ={}, scenes_dir=tmp_path)
    line = _cloud_line(results)
    assert line.status == "ok"
    assert "xiaomi_cloud" in line.detail


def test_partial_credentials_do_not_count(tmp_path):
    env = {"TUYA_CLOUD_ACCESS_ID": "only-the-id"}
    results = check_all({}, environ=env, scenes_dir=tmp_path)
    assert _cloud_line(results).status == "warn"

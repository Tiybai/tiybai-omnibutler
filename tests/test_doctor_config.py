"""Local config, doctor health checks and the setup guides.

Everything here runs offline: HA is faked by replacing
``urllib.request.urlopen`` (same pattern as test_ha_scenes_v2.py), miIO
devices are only ever misconfigured in ways that fail before any packet
is sent, and all config files live in tmp dirs. A recurring theme: no
secret value may ever appear in a check result, a report, or an error.
"""

import json
import os
import re
import stat
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from omnibutler import doctor
from omnibutler.config import (
    ConfigError,
    default_config_path,
    describe_secret,
    device_entries,
    ha_settings,
    load_config,
    resolve_secret,
)
from omnibutler.doctor import check_all, format_report
from omnibutler.setup_guide import guide_text, store_secret

EXAMPLES_SCENES = Path(__file__).resolve().parent.parent / "examples" / "scenes"

_ENV_VARS = [
    "HA_URL", "HA_TOKEN", "HA_TIMEOUT", "HA_ENTITY_PREFIXES",
    "MIIO_DEVICES", "MIIO_HOST", "MIIO_TOKEN", "MIIO_MODEL",
    "TUYA_DEVICES_JSON", "OMNIBUTLER_CONFIG", "TOB_AUDIT_PATH",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# -- HA fakes (mirrors tests/test_ha_scenes_v2.py) ----------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self._body


def _by_name(results):
    return {r.name: r for r in results}


# -- config loading ------------------------------------------------------------


def test_load_config_missing_file_is_empty(tmp_path):
    assert load_config(tmp_path / "nope.json") == {}


def test_load_config_bad_json_reports_path_not_contents(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"ha": {"token": "supersecretvalue", }}', encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert str(path) in str(excinfo.value)
    assert "supersecretvalue" not in str(excinfo.value)


def test_load_config_top_level_must_be_object(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('["ha"]', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_default_config_path_env_override(monkeypatch, tmp_path):
    target = tmp_path / "elsewhere.json"
    monkeypatch.setenv("OMNIBUTLER_CONFIG", str(target))
    assert default_config_path() == target


def test_load_config_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"ha": {"url": "http://ha:8123"}}), encoding="utf-8")
    assert load_config(path) == {"ha": {"url": "http://ha:8123"}}


# -- secret references -----------------------------------------------------------


def test_resolve_secret_variants():
    env = {"MY_KEY": "the-value"}
    assert resolve_secret("env:MY_KEY", environ=env) == "the-value"
    assert resolve_secret("env:MISSING", environ=env) is None
    assert resolve_secret("env:MY_KEY", environ={}) is None
    assert resolve_secret("literal", environ=env) == "literal"
    assert resolve_secret("", environ=env) is None
    assert resolve_secret(None, environ=env) is None
    assert resolve_secret(42, environ=env) is None


def test_describe_secret_never_leaks_the_value():
    env = {"MY_KEY": "the-value"}
    assert describe_secret("env:MY_KEY", environ=env) == "env:MY_KEY (set)"
    assert describe_secret("env:MY_KEY", environ={}) == "env:MY_KEY (NOT set)"
    literal = describe_secret("the-value", environ=env)
    assert literal == "literal value (set)"
    assert "the-value" not in literal
    assert describe_secret(None, environ=env) == "not set"


def test_ha_settings_token_env_and_env_fallback(monkeypatch):
    config = {"ha": {"url": "http://ha:8123/", "token_env": "MY_HA_TOKEN"}}
    settings = ha_settings(config, environ={"MY_HA_TOKEN": "abc"})
    assert settings["url"] == "http://ha:8123"
    assert settings["token"] == "abc"
    assert settings["token_description"] == "env:MY_HA_TOKEN (set)"

    monkeypatch.setenv("HA_URL", "http://env-ha:8123")
    monkeypatch.setenv("HA_TOKEN", "env-token")
    fallback = ha_settings({}, environ=None)
    assert fallback["url"] == "http://env-ha:8123"
    assert fallback["token"] == "env-token"
    assert fallback["token_description"] == "env:HA_TOKEN (set)"


def test_device_entries_resolve_env_refs():
    config = {"miio": {"devices": [
        {"id": "ac", "host": "192.168.1.31", "token": "env:AC_TOK"},
        {"id": "purifier", "host": "192.168.1.32", "token": "env:GONE"},
    ]}}
    entries = device_entries(config, "miio", "token", environ={"AC_TOK": "aa" * 16})
    assert entries[0]["token"] == "aa" * 16
    assert entries[0]["token_description"] == "env:AC_TOK (set)"
    assert entries[1]["token"] is None
    assert entries[1]["token_description"] == "env:GONE (NOT set)"


# -- store_secret -----------------------------------------------------------------


def test_store_secret_creates_0600_and_nested_lists(tmp_path):
    path = tmp_path / "nested" / "config.json"
    store_secret(path, "miio.devices.0.token", "ab" * 16)
    if os.name == "posix":  # Windows chmod cannot express 0600 (ACLs instead)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["miio"]["devices"][0]["token"] == "ab" * 16

    store_secret(path, "tuya.devices.1.local_key", "key-2")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tuya"]["devices"][1]["local_key"] == "key-2"
    assert data["miio"]["devices"][0]["token"] == "ab" * 16  # preserved
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_store_secret_replaces_without_keeping_old_value(tmp_path):
    path = tmp_path / "config.json"
    store_secret(path, "ha.token", "old-token-value")
    store_secret(path, "ha.token", "new-token-value")
    text = path.read_text(encoding="utf-8")
    assert "new-token-value" in text
    assert "old-token-value" not in text


def test_store_secret_refuses_to_clobber_bad_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ConfigError):
        store_secret(path, "ha.token", "x")
    assert path.read_text(encoding="utf-8") == "{broken"


# -- doctor: unconfigured / classification -----------------------------------------


def test_doctor_unconfigured_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("TOB_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    results = check_all({}, environ={}, scenes_dir=EXAMPLES_SCENES)
    by = _by_name(results)
    assert by["home-assistant"].status == "warn"
    assert by["miio"].status == "warn"
    assert by["tuya"].status == "warn"
    assert by["state-dir"].status == "ok"
    assert by["scenes"].status == "ok"
    assert all(r.status in ("ok", "warn", "fail") for r in results)


def test_doctor_bad_config_file_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("TOB_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    bad = tmp_path / "config.json"
    bad.write_text("{oops", encoding="utf-8")
    results = check_all(None, config_path=bad, environ={}, scenes_dir=EXAMPLES_SCENES)
    by = _by_name(results)
    assert by["config"].status == "fail"
    assert "home-assistant" in by  # the rest of the report still ran


# -- doctor: Home Assistant ----------------------------------------------------------


def test_doctor_ha_reachable_with_env_ref_token(monkeypatch, tmp_path):
    monkeypatch.setenv("TOB_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda request, timeout=None: _FakeResponse({"message": "API running."}),
    )
    config = {"ha": {"url": "http://ha.local:8123", "token": "env:HA_TOK"}}
    results = check_all(config, environ={"HA_TOK": "real-secret-token"},
                        scenes_dir=EXAMPLES_SCENES)
    ha = _by_name(results)["home-assistant"]
    assert ha.status == "ok"
    assert "real-secret-token" not in format_report(results)


def test_doctor_ha_401_means_token_problem(monkeypatch, tmp_path):
    monkeypatch.setenv("TOB_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    def _unauthorized(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", _unauthorized)
    config = {"ha": {"url": "http://ha.local:8123", "token": "deadbeef"}}
    results = check_all(config, environ={}, scenes_dir=EXAMPLES_SCENES)
    ha = _by_name(results)["home-assistant"]
    assert ha.status == "fail"
    assert "401" in ha.detail and "token" in ha.detail
    assert "deadbeef" not in ha.detail


def test_doctor_ha_unreachable_is_network_problem(monkeypatch, tmp_path):
    monkeypatch.setenv("TOB_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    def _down(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _down)
    config = {"ha": {"url": "http://ha.local:8123", "token": "tok"}}
    results = check_all(config, environ={}, scenes_dir=EXAMPLES_SCENES, timeout=0.5)
    ha = _by_name(results)["home-assistant"]
    assert ha.status == "fail"
    assert "reach" in ha.detail


def test_doctor_ha_url_without_token_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("TOB_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    config = {"ha": {"url": "http://ha.local:8123", "token": "env:NOPE"}}
    results = check_all(config, environ={}, scenes_dir=EXAMPLES_SCENES)
    ha = _by_name(results)["home-assistant"]
    assert ha.status == "fail"
    assert "env:NOPE (NOT set)" in ha.detail


# -- doctor: miIO -----------------------------------------------------------------------


def test_doctor_miio_token_env_ref_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("TOB_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    config = {"miio": {"devices": [
        {"id": "ac1", "host": "192.168.1.31", "token": "env:MIIO_TOK"},
    ]}}
    results = check_all(config, environ={}, scenes_dir=EXAMPLES_SCENES)
    result = _by_name(results)["miio:ac1"]
    assert result.status == "fail"
    assert "env:MIIO_TOK (NOT set)" in result.detail


def test_doctor_miio_malformed_token_never_echoed(monkeypatch, tmp_path):
    monkeypatch.setenv("TOB_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    config = {"miio": {"devices": [
        {"id": "ac1", "host": "192.168.1.31", "token": "not-a-real-token"},
    ]}}
    results = check_all(config, environ={}, scenes_dir=EXAMPLES_SCENES)
    result = _by_name(results)["miio:ac1"]
    assert result.status == "fail"
    assert "32 hex" in result.detail
    assert "not-a-real-token" not in format_report(results)


def test_doctor_miio_missing_host(monkeypatch, tmp_path):
    monkeypatch.setenv("TOB_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    config = {"miio": {"devices": [{"id": "ac1", "token": "ab" * 16}]}}
    results = check_all(config, environ={}, scenes_dir=EXAMPLES_SCENES)
    assert _by_name(results)["miio:ac1"].status == "fail"


# -- doctor: Tuya -------------------------------------------------------------------------


def test_doctor_tuya_missing_local_key(monkeypatch, tmp_path):
    monkeypatch.setenv("TOB_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    config = {"tuya": {"devices": [
        {"device_id": "bf123", "ip": "192.168.1.50"},
    ]}}
    results = check_all(config, environ={}, scenes_dir=EXAMPLES_SCENES)
    tuya = _by_name(results)["tuya"]
    assert tuya.status == "fail"
    assert "local_key" in tuya.detail


def test_doctor_tuya_complete_config(monkeypatch, tmp_path):
    monkeypatch.setenv("TOB_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    config = {"tuya": {"devices": [
        {"device_id": "bf123", "ip": "192.168.1.50", "local_key": "env:PLUG_KEY"},
    ]}}
    results = check_all(config, environ={"PLUG_KEY": "secret-key"},
                        scenes_dir=EXAMPLES_SCENES)
    tuya = _by_name(results)["tuya"]
    # tinytuya may or may not be installed: ok when it is, warn when not -
    # either way the config itself is complete and the key stays secret.
    assert tuya.status in ("ok", "warn")
    assert "secret-key" not in format_report(results)


# -- doctor: scenes --------------------------------------------------------------------------


def test_doctor_scenes_broken_file_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("TOB_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    scenes = tmp_path / "scenes"
    scenes.mkdir()
    (scenes / "broken.yaml").write_text("{{{", encoding="utf-8")
    results = check_all({}, environ={}, scenes_dir=scenes)
    scene_result = _by_name(results)["scenes"]
    assert scene_result.status == "fail"
    assert "broken.yaml" in scene_result.detail


def test_doctor_scenes_missing_dir_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("TOB_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    results = check_all({}, environ={}, scenes_dir=tmp_path / "no-such-dir")
    assert _by_name(results)["scenes"].status == "warn"


# -- format_report --------------------------------------------------------------------------------


def test_format_report_shape():
    results = [
        doctor.CheckResult("home-assistant", "ok", "reachable"),
        doctor.CheckResult("tuya", "warn", "not set up"),
        doctor.CheckResult("miio:ac1", "fail", "no token"),
    ]
    report = format_report(results)
    assert "[OK]" in report and "[WARN]" in report and "[FAIL]" in report
    assert "1 ok, 1 warning(s), 1 failure(s)" in report


# -- setup guides ------------------------------------------------------------------


def test_guides_cover_the_key_steps_without_real_keys():
    miio = guide_text("miio")
    assert "token" in miio and "32" in miio and "本地" in miio
    tuya = guide_text("tuya")
    assert "local_key" in tuya and "IoT" in tuya and "本地" in tuya
    ha = guide_text("ha")
    assert "长期访问令牌" in ha and "HA_TOKEN" in ha
    for text in (miio, tuya, ha):
        # No actual 32-hex-char key material anywhere in the guides.
        assert not re.search(r"\b[0-9a-f]{32}\b", text)
    assert guide_text("Xiaomi") == miio
    assert guide_text("homeassistant") == ha
    with pytest.raises(ValueError):
        guide_text("gree")

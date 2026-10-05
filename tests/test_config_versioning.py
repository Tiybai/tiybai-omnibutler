"""Config format versioning (docs/config-versioning.md, landed in v0.5).

The rules under test: a missing ``version`` means 1 and old files keep
loading; a newer-than-current version is a hard ConfigError, never a
silent misread; loading never rewrites the file; the versioned write
path (``save_config``) stamps the current version and keeps a one-time
backup before the first write-back of a pre-versioning file.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from omnibutler.config import (
    CURRENT_CONFIG_VERSION,
    MIGRATIONS,
    ConfigError,
    config_version,
    load_config,
    migrate_config,
    save_config,
)


def _write(path: Path, data) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# -- version reading ---------------------------------------------------------


def test_current_version_is_1():
    assert CURRENT_CONFIG_VERSION == 1


def test_missing_version_means_1():
    assert config_version({}) == 1
    assert config_version({"ha": {"url": "http://x"}}) == 1


def test_explicit_version_1_reads_as_1():
    assert config_version({"version": 1}) == 1


def test_newer_version_is_a_hard_error():
    with pytest.raises(ConfigError) as excinfo:
        config_version({"version": 2})
    assert "newer" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["1", 1.0, True, None, 0, -3])
def test_unusable_version_values_are_hard_errors(bad):
    with pytest.raises(ConfigError):
        config_version({"version": bad})


# -- loading -----------------------------------------------------------------


def test_old_config_without_version_still_loads(tmp_path):
    path = _write(tmp_path / "config.json",
                  {"ha": {"url": "http://192.168.1.10:8123"}})
    assert load_config(path) == {"ha": {"url": "http://192.168.1.10:8123"}}


def test_load_never_rewrites_the_file(tmp_path):
    path = _write(tmp_path / "config.json", {"ha": {"url": "http://x"}})
    before = path.read_bytes()
    load_config(path)
    assert path.read_bytes() == before
    assert not (tmp_path / "config.json.v1.bak").exists()


def test_load_config_from_newer_version_refuses(tmp_path):
    path = _write(tmp_path / "config.json", {"version": 99, "ha": {}})
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "newer" in str(excinfo.value)


def test_migrate_config_is_identity_at_v1():
    data = {"ha": {"url": "http://x"}}
    assert migrate_config(dict(data)) == data


# -- migration registry shape --------------------------------------------------


def test_migration_registry_shape():
    assert isinstance(MIGRATIONS, dict)
    for key, fn in MIGRATIONS.items():
        assert isinstance(key, int)
        assert key < CURRENT_CONFIG_VERSION
        assert callable(fn)
    # At version 1 there is nothing to migrate yet; the chain-walk in
    # migrate_config must still leave a v1 config untouched.
    assert migrate_config({"version": 1}) == {"version": 1}


# -- versioned writes ------------------------------------------------------------


def test_save_config_stamps_version_and_0600(tmp_path):
    path = tmp_path / "config.json"
    data = {"ha": {"url": "http://192.168.1.10:8123"}}
    save_config(path, data)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["version"] == CURRENT_CONFIG_VERSION
    assert written["ha"]["url"] == "http://192.168.1.10:8123"
    if os.name == "posix":  # Windows chmod cannot express 0600 (ACLs instead)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "version" not in data  # the caller's dict is not mutated


def test_save_config_roundtrips_through_load(tmp_path):
    path = tmp_path / "config.json"
    save_config(path, {"miio": {"devices": []}})
    assert load_config(path)["version"] == CURRENT_CONFIG_VERSION


def test_first_writeback_of_unversioned_file_keeps_backup(tmp_path):
    path = _write(tmp_path / "config.json", {"ha": {"url": "http://old"}})
    original = path.read_bytes()
    save_config(path, {"ha": {"url": "http://new"}})
    backup = tmp_path / "config.json.v1.bak"
    assert backup.exists()
    assert backup.read_bytes() == original


def test_backup_is_one_time_and_never_overwritten(tmp_path):
    path = _write(tmp_path / "config.json", {"ha": {"url": "http://first"}})
    save_config(path, {"ha": {"url": "http://second"}})
    backup = tmp_path / "config.json.v1.bak"
    first_backup = backup.read_bytes()
    # The user restores an (unversioned) file by hand and something
    # writes config again: the pristine first backup must survive.
    _write(path, {"ha": {"url": "http://restored"}})
    save_config(path, {"ha": {"url": "http://third"}})
    assert backup.read_bytes() == first_backup


def test_writeback_of_versioned_file_makes_no_backup(tmp_path):
    path = _write(tmp_path / "config.json",
                  {"version": 1, "ha": {"url": "http://x"}})
    save_config(path, {"ha": {"url": "http://y"}})
    assert not (tmp_path / "config.json.v1.bak").exists()


def test_save_config_refuses_to_clobber_bad_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ConfigError):
        save_config(path, {"ha": {}})
    assert path.read_text(encoding="utf-8") == "{broken"

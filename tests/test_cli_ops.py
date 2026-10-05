"""Tests for the ops commands: tob audit / tob backup / tob restore."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from omnibutler.backup import BackupError, create_backup, restore_backup
from omnibutler.cli import main
from omnibutler.core.audit import AuditLog

# ---------------------------------------------------------------------------
# tob audit
# ---------------------------------------------------------------------------

def _entry(i: int, device: str = "d1", action: str = "set_property") -> dict:
    return {
        "ts": 1_757_000_000 + i,
        "time": f"2026-10-05T21:00:{i:02d}+0800",
        "agent": "cli",
        "device": device,
        "action": action,
        "params": {"i": i},
        "ok": True,
        "result": None,
        "error": None,
    }


def _write_rotated_log(path: Path) -> list[dict]:
    """Five entries spread over .2 / .1 / live, oldest in .2."""
    entries = [_entry(i) for i in range(5)]
    groups = {f"{path.name}.2": entries[0:2], f"{path.name}.1": entries[2:4],
              path.name: entries[4:5]}
    for name, group in groups.items():
        (path.parent / name).write_text(
            "".join(json.dumps(e) + "\n" for e in group), encoding="utf-8")
    return entries


def test_audit_reads_across_rotations_in_order(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    _write_rotated_log(path)
    assert main(["--audit", str(path), "audit"]) == 0
    out = capsys.readouterr().out
    positions = [out.index(f'"i": {i}') for i in range(5)]
    assert positions == sorted(positions)  # oldest first, rotations spanned


def test_audit_last_and_summary(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    _write_rotated_log(path)
    assert main(["--audit", str(path), "audit", "--last", "2"]) == 0
    out = capsys.readouterr().out
    assert '"i": 3' in out and '"i": 4' in out
    assert '"i": 2' not in out
    assert "2 shown of 5 matching, 5 total" in out


def test_audit_filters_device_and_action(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    entries = [_entry(0, device="lamp"), _entry(1, device="ac"),
               _entry(2, device="lamp", action="call_action"),
               _entry(3, device="lamp")]
    path.write_text("".join(json.dumps(e) + "\n" for e in entries),
                    encoding="utf-8")
    assert main(["--audit", str(path), "audit", "--device", "lamp"]) == 0
    out = capsys.readouterr().out
    assert '"i": 1' not in out  # the ac entry is filtered out
    assert out.count("set_property") == 2  # lamp's two set_property entries
    # adding the action filter narrows to the one call_action entry:
    assert main(["--audit", str(path), "audit", "--device", "lamp",
                 "--action", "call_action"]) == 0
    out = capsys.readouterr().out
    assert "call_action" in out and "set_property" not in out


def test_audit_writer_rotation_roundtrip(tmp_path):
    """AuditLog itself rotates; read_all (what the command uses) keeps order."""
    log = AuditLog(path=tmp_path / "audit.jsonl", max_bytes=350, keep=6)
    for i in range(6):
        log.record(agent="cli", device_id="d1", action="set_property",
                   params={"i": i})
    assert (tmp_path / "audit.jsonl.1").exists()  # rotation really happened
    entries = log.read_all()
    assert [e["params"]["i"] for e in entries] == list(range(6))


def test_audit_missing_file_is_friendly(tmp_path, capsys):
    assert main(["--audit", str(tmp_path / "nope.jsonl"), "audit"]) == 0
    out = capsys.readouterr().out
    assert "no audit log yet" in out


def test_audit_empty_file_and_bad_last(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    path.write_text("", encoding="utf-8")
    assert main(["--audit", str(path), "audit"]) == 0
    assert "audit log is empty" in capsys.readouterr().out
    assert main(["--audit", str(path), "audit", "--last", "0"]) == 1
    assert "--last" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# tob backup / restore
# ---------------------------------------------------------------------------

@pytest.fixture()
def home(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    config = state / "config.json"
    monkeypatch.setenv("OMNIBUTLER_STATE_DIR", str(state))
    monkeypatch.setenv("OMNIBUTLER_CONFIG", str(config))
    config.write_text('{"version": 1, "miio": {}}', encoding="utf-8")
    (state / "confirmations.json").write_text('{"items": []}',
                                               encoding="utf-8")
    (state / "audit.jsonl").write_text('{"action": "x"}\n', encoding="utf-8")
    (state / "streams.jsonl").write_text('{"value": 1}\n', encoding="utf-8")
    (state / "daemon.lock").write_text('{"pid": 1}', encoding="utf-8")
    (state / "confirmations.json.tmp-42").write_text("partial",
                                                     encoding="utf-8")
    return state, config


def _members(archive: Path) -> set[str]:
    with tarfile.open(archive) as tar:
        return set(tar.getnames())


def test_backup_contents_and_warning(home, tmp_path, capsys):
    out_path = tmp_path / "b.tar.gz"
    assert main(["backup", str(out_path)]) == 0
    out = capsys.readouterr().out
    assert "backup written to" in out
    assert "warning" in out and "secrets" in out  # secrets warning is said
    assert _members(out_path) == {
        "config.json",
        "state/confirmations.json",
        "state/audit.jsonl",
        "state/streams.jsonl",
    }  # no daemon.lock, no *.tmp-* files


def test_backup_default_filename(home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["backup"]) == 0
    made = list(tmp_path.glob("omnibutler-backup-*.tar.gz"))
    assert len(made) == 1


def test_backup_nothing_to_back_up(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBUTLER_STATE_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("OMNIBUTLER_CONFIG", str(tmp_path / "no-config.json"))
    with pytest.raises(BackupError):
        create_backup(tmp_path / "b.tar.gz", tmp_path / "no-config.json",
                      tmp_path / "empty")


def test_restore_roundtrip_keeps_bak(home, tmp_path, capsys):
    state, config = home
    archive = tmp_path / "b.tar.gz"
    assert main(["backup", str(archive)]) == 0
    capsys.readouterr()
    original = (state / "confirmations.json").read_text(encoding="utf-8")
    # Life goes on: state changes, a stream file is lost, junk appears.
    (state / "confirmations.json").write_text('{"items": ["changed"]}',
                                              encoding="utf-8")
    (state / "streams.jsonl").unlink()
    (state / "junk.txt").write_text("junk", encoding="utf-8")

    assert main(["restore", str(archive)]) == 0
    out = capsys.readouterr().out
    assert "previous state kept at" in out
    # Restored content wins; the pre-restore state survives in the .bak.
    assert (state / "confirmations.json").read_text(
        encoding="utf-8") == original
    assert (state / "streams.jsonl").exists()
    assert not (state / "junk.txt").exists()
    bak = tmp_path / "state.bak"
    assert (bak / "confirmations.json").read_text(
        encoding="utf-8") == '{"items": ["changed"]}'
    assert (bak / "junk.txt").exists()
    assert config.read_text(encoding="utf-8") == '{"version": 1, "miio": {}}'


def test_restore_into_empty_environment(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    config = tmp_path / "elsewhere" / "config.json"
    monkeypatch.setenv("OMNIBUTLER_STATE_DIR", str(state))
    monkeypatch.setenv("OMNIBUTLER_CONFIG", str(config))
    archive = tmp_path / "b.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for arcname, payload in (("config.json", b"{}"),
                                 ("state/audit.jsonl", b"{}\n")):
            info = tarfile.TarInfo(arcname)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    assert main(["restore", str(archive)]) == 0
    out = capsys.readouterr().out
    assert "previous state kept at" not in out  # nothing existed to keep
    assert config.read_text(encoding="utf-8") == "{}"
    assert (state / "audit.jsonl").exists()


def _hostile_archive(path: Path, names: list[str]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name in names:
            payload = b"pwned"
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))


@pytest.mark.parametrize("bad_name", [
    "../evil.txt", "/abs/evil.txt", "state/../../evil.txt",
    "state/../evil.txt", "other/evil.txt", "C:/evil.txt",
])
def test_restore_rejects_path_traversal(home, tmp_path, bad_name):
    state, _config = home
    archive = tmp_path / "hostile.tar.gz"
    _hostile_archive(archive, [bad_name])
    with pytest.raises(BackupError):
        restore_backup(archive, state / "config.json", state)
    # Nothing moved, nothing written outside or inside.
    assert not (tmp_path / "state.bak").exists()
    assert not (tmp_path / "evil.txt").exists()
    assert (state / "confirmations.json").exists()


def test_restore_rejects_link_members(home, tmp_path):
    state, _config = home
    archive = tmp_path / "link.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("state/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(BackupError):
        restore_backup(archive, state / "config.json", state)
    assert not (state / "link").exists()


def test_restore_missing_archive_is_an_error(home, tmp_path):
    assert main(["restore", str(tmp_path / "gone.tar.gz")]) == 1

"""Size-based retention for the JSONL state files (audit log + streams).

Both files are append-only and would otherwise grow without bound on a
long-lived host. These tests pin down the shared policy: rotate by
size, keep a bounded number of backups, history reads span the backups,
limits come from the constructor or the OMNIBUTLER_LOG_* env vars, and a
rotation failure never breaks a write.
"""

from __future__ import annotations

from pathlib import Path

from omnibutler.core.audit import AuditLog
from omnibutler.core.streams import DataStream, StreamStore

PAD = "x" * 60  # makes each audit line ~230 bytes, so caps stay small


def _record(log: AuditLog, i: int) -> dict:
    return log.record("tester", "dev", "set", params={"i": i, "pad": PAD})


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".*"))


def test_audit_rotates_when_cap_would_be_exceeded(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path=path, max_bytes=300, keep=3)
    for i in range(10):
        _record(log, i)
    assert (tmp_path / "audit.jsonl.1").exists()
    # The live file never holds more than the cap plus one record that
    # itself fits under the cap.
    assert path.stat().st_size <= 300 + 250
    entries = log.read_all()
    assert entries[-1]["params"]["i"] == 9


def test_audit_keep_limit_deletes_oldest(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path=path, max_bytes=300, keep=2)
    for i in range(30):
        _record(log, i)
    assert (tmp_path / "audit.jsonl.1").exists()
    assert (tmp_path / "audit.jsonl.2").exists()
    assert not (tmp_path / "audit.jsonl.3").exists()
    kept = [e["params"]["i"] for e in log.read_all()]
    assert kept[-1] == 29
    assert 0 not in kept  # the oldest history is gone, not just hidden
    assert kept == sorted(kept)  # survivors stay in write order


def test_audit_read_all_spans_backups_without_loss(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path=path, max_bytes=500, keep=10)
    for i in range(8):
        _record(log, i)
    assert _backups(path)  # rotation really happened
    entries = log.read_all()
    assert [e["params"]["i"] for e in entries] == list(range(8))
    # A fresh reader with the same retention settings sees it all too.
    fresh = AuditLog(path=path, max_bytes=500, keep=10)
    assert [e["params"]["i"] for e in fresh.read_all()] == list(range(8))


def test_streams_history_survives_rotation_and_reload(tmp_path):
    path = tmp_path / "streams.jsonl"
    store = StreamStore(path=path, max_bytes=450, keep=8)
    stream = DataStream(id="phone-steps", kind="health.steps", source="phone")
    for i in range(10):
        store.append("phone-steps", i, ts=float(i + 1), stream=stream)
    assert _backups(path)  # rotation really happened

    reloaded = StreamStore(path=path)
    history = reloaded.history("phone-steps")
    assert [p.value for p in history] == list(range(10))
    assert [p.ts for p in history] == [float(i + 1) for i in range(10)]
    assert reloaded.latest("phone-steps").value == 9
    assert len(reloaded) == 10


def test_env_vars_override_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBUTLER_LOG_MAX_MB", "0.001")  # ~1 KiB
    monkeypatch.setenv("OMNIBUTLER_LOG_KEEP", "2")
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path=path)
    assert log.max_bytes == int(0.001 * 1024 * 1024)
    assert log.keep == 2
    for i in range(30):
        _record(log, i)
    assert (tmp_path / "audit.jsonl.2").exists()
    assert not (tmp_path / "audit.jsonl.3").exists()
    # Streams resolve the same env vars through the shared helper.
    store = StreamStore(path=tmp_path / "streams.jsonl")
    assert store.max_bytes == log.max_bytes
    assert store.keep == 2


def test_invalid_env_vars_fall_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBUTLER_LOG_MAX_MB", "banana")
    monkeypatch.setenv("OMNIBUTLER_LOG_KEEP", "-3")
    log = AuditLog(path=tmp_path / "audit.jsonl")
    assert log.max_bytes == 10 * 1024 * 1024
    assert log.keep == 5


def test_rotation_failure_never_breaks_a_write(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    real_replace = Path.replace

    def broken_replace(self, target):
        if str(target).startswith(str(path)):
            raise OSError("simulated rotation failure")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", broken_replace)
    log = AuditLog(path=path, max_bytes=300, keep=3)
    for i in range(5):  # several writes would each try to rotate
        entry = _record(log, i)
        assert entry["params"]["i"] == i
    # Nothing rotated, but every write landed and is readable.
    assert not _backups(path)
    assert [e["params"]["i"] for e in log.read_all()] == list(range(5))

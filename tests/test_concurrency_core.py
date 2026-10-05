"""Concurrency and file-discipline tests for the core (line B1).

The confirmation queue and the config file are shared between
processes (daemon, CLI, MCP server) and between threads inside the
daemon. These tests pin the discipline that keeps the safety guardrail
sound under that sharing: locked mutations, atomic claims, exactly-one
execution, corrupt-file evidence, retention pruning, and cheap cached
refresh. Two queue objects on one file stand in for two processes;
threads stand in for the daemon's event sources.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from omnibutler.approvals_web import apply_human_decision
from omnibutler.config import load_config, save_config
from omnibutler.core.confirmations import ConfirmationQueue
from omnibutler.core.events import Event
from omnibutler.core.filelock import FileLock, FileLockTimeout
from omnibutler.core.manager import DeviceManager
from omnibutler.daemon import Daemon
from omnibutler.drivers.mock import MockDriver
from omnibutler.runtime import Runtime
from omnibutler.scenes.engine import SceneEngine
from omnibutler.scenes.loader import parse_scene

THREADS = 8


def _run_concurrently(fn, count=THREADS):
    """Run ``fn(index)`` from ``count`` threads released together.

    Returns (results, errors) - errors are collected, never swallowed
    silently: callers assert on them.
    """
    barrier = threading.Barrier(count)
    results: list = [None] * count
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = fn(index)
        except BaseException as exc:  # surfaced via errors below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, errors


def _add(queue: ConfirmationQueue, name: str = "open"):
    return queue.add(
        device_id="garage_door", kind="call_action", name=name, params={},
        requested_by="test", scene="test-scene", risk="high",
    )


# -- FileLock -------------------------------------------------------------------

def test_filelock_excludes_and_times_out(tmp_path):
    path = tmp_path / "x.lock"
    held = FileLock(path).acquire()
    try:
        with pytest.raises(FileLockTimeout):
            FileLock(path, timeout=0.3).acquire()
    finally:
        held.release()
    # After release the lock is free again, and re-acquiring through
    # the same instance is a no-op rather than a self-deadlock.
    with FileLock(path, timeout=1.0) as lock:
        assert lock.acquire() is lock


# -- queue: concurrent adds -------------------------------------------------------

def test_concurrent_adds_across_instances_lose_nothing(tmp_path):
    path = tmp_path / "confirmations.json"
    queues = [ConfirmationQueue(path=path), ConfirmationQueue(path=path)]

    def add_batch(index: int) -> None:
        queue = queues[index % len(queues)]
        for n in range(25):
            _add(queue, name=f"open-{index}-{n}")

    _, errors = _run_concurrently(add_batch, count=4)
    assert errors == []
    items = ConfirmationQueue(path=path).all()
    assert len(items) == 100
    assert len({item.id for item in items}) == 100  # no id collisions
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert len(raw["items"]) == 100


# -- queue: claim -----------------------------------------------------------------

def test_claim_is_atomic_terminal_and_shared(tmp_path):
    path = tmp_path / "confirmations.json"
    queue = ConfirmationQueue(path=path)
    other = ConfirmationQueue(path=path)  # stands in for another process
    item = _add(queue)

    assert queue.claim(item.id) is True
    assert queue.get(item.id).status == "confirmed"
    # The transition is persisted and final from every viewpoint.
    assert other.get(item.id).status == "confirmed"
    assert queue.claim(item.id) is False
    assert other.claim(item.id) is False
    assert queue.claim("cfm-9999") is False

    rejected = _add(queue)
    assert other.claim(rejected.id, status="rejected") is True
    assert queue.get(rejected.id).status == "rejected"
    assert queue.claim(rejected.id) is False


def test_concurrent_claims_have_exactly_one_winner(tmp_path):
    path = tmp_path / "confirmations.json"
    queues = [ConfirmationQueue(path=path) for _ in range(3)]
    item = _add(queues[0])

    results, errors = _run_concurrently(
        lambda i: queues[i % len(queues)].claim(item.id)
    )
    assert errors == []
    assert results.count(True) == 1
    assert ConfirmationQueue(path=path).get(item.id).status == "confirmed"


# -- engine: double approval executes once -----------------------------------------

def _counting_manager(manager, monkeypatch):
    calls: list[tuple] = []
    lock = threading.Lock()

    def counting_call_action(device_id, name, params, agent=""):
        with lock:
            calls.append((device_id, name))
        return {"ok": True}

    monkeypatch.setattr(manager, "call_action", counting_call_action)
    return calls


def test_double_confirm_across_entry_points_executes_once(
    manager, engine, monkeypatch
):
    calls = _counting_manager(manager, monkeypatch)
    queue = engine.confirmations
    item = _add(queue)

    def approve(index: int):
        if index % 2 == 0:
            # The approvals-page path.
            return apply_human_decision(
                engine, queue, manager, item.id,
                approve=True, agent="web:human",
            )
        # The CLI path.
        return engine.confirm(item.id, agent="cli:human")

    _, errors = _run_concurrently(approve, count=4)
    assert errors == []
    assert len(calls) == 1  # the action itself ran exactly once
    assert queue.get(item.id).status == "confirmed"
    # A late second decision finds nothing left to do.
    assert engine.confirm(item.id) is None
    assert engine.reject(item.id) is False


def test_reject_race_also_executes_never(manager, engine, monkeypatch):
    calls = _counting_manager(manager, monkeypatch)
    queue = engine.confirmations
    item = _add(queue)

    def decide(index: int):
        if index % 2 == 0:
            return engine.reject(item.id)
        return engine.confirm(item.id, agent="cli:human")

    results, errors = _run_concurrently(decide, count=4)
    assert errors == []
    status = queue.get(item.id).status
    if status == "rejected":
        assert len(calls) == 0
        assert results.count(True) == 1  # exactly one reject won
    else:
        assert status == "confirmed"
        assert len(calls) == 1


def test_failed_execution_stays_confirmed_and_is_audited(
    manager, engine, monkeypatch
):
    def boom(device_id, name, params, agent=""):
        raise RuntimeError("driver exploded")

    monkeypatch.setattr(manager, "call_action", boom)
    queue = engine.confirmations
    item = _add(queue)

    with pytest.raises(RuntimeError, match="driver exploded"):
        engine.confirm(item.id, agent="cli:human")
    # Never back to pending: the action must not become runnable again.
    assert queue.get(item.id).status == "confirmed"
    assert engine.confirm(item.id) is None
    failures = [
        entry for entry in manager.audit.read_all()
        if entry["action"] == "confirmation:execute_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["ok"] is False
    assert "driver exploded" in failures[0]["error"]


# -- queue: corruption, pruning, perms, cache, tmp hygiene -------------------------

def test_corrupt_queue_file_is_quarantined_not_swallowed(tmp_path, capsys):
    path = tmp_path / "confirmations.json"
    path.write_bytes(b"{this is not json")
    queue = ConfirmationQueue(path=path)

    assert queue.pending() == []
    quarantined = list(tmp_path.glob("confirmations.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"{this is not json"  # evidence kept
    assert "corrupt" in capsys.readouterr().err
    # The queue works again from a clean slate.
    item = _add(queue)
    assert queue.get(item.id) is not None


def test_terminal_entries_pruned_after_30_days_pending_kept(tmp_path):
    path = tmp_path / "confirmations.json"
    old = time.time() - 31 * 24 * 60 * 60

    def entry(item_id: str, status: str, created_at: float) -> dict:
        return {
            "id": item_id, "device": "garage_door", "kind": "call_action",
            "name": "open", "value": None, "params": {}, "requested_by": "t",
            "scene": None, "risk": "high", "status": status,
            "created_at": created_at,
        }

    path.write_text(json.dumps({"version": 1, "items": [
        entry("cfm-0001", "confirmed", old),      # old + resolved -> pruned
        entry("cfm-0002", "pending", old),        # old but pending -> kept
        entry("cfm-0003", "rejected", time.time()),  # fresh -> kept
    ]}), encoding="utf-8")
    queue = ConfirmationQueue(path=path)
    _add(queue)  # any save runs the pruning
    raw = json.loads(path.read_text(encoding="utf-8"))
    kept = {item["id"] for item in raw["items"]}
    assert "cfm-0001" not in kept
    assert "cfm-0002" in kept
    assert "cfm-0003" in kept


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes do not apply")
def test_saved_queue_file_is_owner_only(tmp_path):
    queue = ConfirmationQueue(path=tmp_path / "confirmations.json")
    _add(queue)
    assert (tmp_path / "confirmations.json").stat().st_mode & 0o777 == 0o600


def test_refresh_skips_reread_while_signature_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "confirmations.json"
    queue = ConfirmationQueue(path=path)
    _add(queue)

    reads: list[Path] = []
    original = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        if self == path:
            reads.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    queue.pending()
    queue.pending()
    queue.all()
    assert reads == []  # unchanged file: stat only, no re-parse

    # A peer's write changes the signature: the next read sees it.
    other = ConfirmationQueue(path=path)
    _add(other)
    assert len(queue.pending()) == 2


def test_stale_tmp_files_from_dead_writers_are_cleaned(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    stale = tmp_path / f"confirmations.json.tmp-{proc.pid}"
    stale.write_text("partial", encoding="utf-8")
    own = tmp_path / f"confirmations.json.tmp-{os.getpid()}"
    own.write_text("mine - possibly mid-save", encoding="utf-8")

    ConfirmationQueue(path=tmp_path / "confirmations.json")
    assert not stale.exists()  # dead writer's leftover: cleaned
    assert own.exists()        # a live pid's temp is never touched


# -- config -------------------------------------------------------------------------

def test_save_config_survives_missing_fchmod(tmp_path, monkeypatch):
    # Windows' os module has no fchmod; deleting it here stands in for
    # that platform, where save_config used to crash on every save.
    monkeypatch.delattr(os, "fchmod", raising=False)
    target = tmp_path / "config.json"
    save_config(target, {"ha": {"url": "http://192.168.1.10:8123"}})
    assert load_config(target)["ha"]["url"] == "http://192.168.1.10:8123"
    if os.name != "nt":  # the post-replace chmod still applied the mode
        assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes do not apply")
def test_config_version_backup_is_owner_only(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"ha": {"url": "http://old"}}),
                      encoding="utf-8")
    save_config(target, {"ha": {"url": "http://new"}})
    backup = tmp_path / "config.json.v1.bak"
    assert backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8"))["ha"]["url"] == "http://old"
    assert backup.stat().st_mode & 0o777 == 0o600


def test_concurrent_config_saves_never_tear(tmp_path):
    target = tmp_path / "config.json"
    save_config(target, {"marker": -1})

    def save_batch(index: int) -> None:
        for _ in range(10):
            save_config(target, {"marker": index})

    _, errors = _run_concurrently(save_batch, count=4)
    assert errors == []
    final = load_config(target)  # parses: no torn/interleaved writes
    assert final["marker"] in range(4)
    assert final["version"] == 1


# -- engine: concurrent events ------------------------------------------------------

def _delay_engine(manager) -> SceneEngine:
    engine = SceneEngine(manager)
    engine.add_scene(parse_scene({
        "name": "light-timer",
        "trigger": {"type": "geofence", "zone": "home", "transition": "enter"},
        "actions": [
            {"device": "living_light", "set": {"onoff": True}},
            {"delay": 300},
            {"device": "living_light", "set": {"onoff": False}},
        ],
    }))
    return engine


def test_concurrent_handle_event_keeps_one_delayed_segment(manager, monkeypatch):
    now = [10_000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    engine = _delay_engine(manager)
    event = Event(type="geofence", data={"zone": "home", "transition": "enter"})

    _, errors = _run_concurrently(lambda i: engine.handle_event(event))
    assert errors == []
    # Retrigger-replace held under concurrency: one segment, not eight.
    assert engine.delayed_pending == 1

    report = engine.process_due(now[0] + 10_000)
    assert report.evaluated == ["light-timer"]
    assert engine.delayed_pending == 0
    assert manager.get_state("living_light")["onoff"] is False


def test_process_due_racing_handle_event_stays_consistent(manager, monkeypatch):
    now = [10_000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    engine = _delay_engine(manager)
    event = Event(type="geofence", data={"zone": "home", "transition": "enter"})
    engine.handle_event(event)

    def fire(_index: int) -> None:
        for _ in range(20):
            engine.handle_event(event)
            engine.process_due(now[0])

    _, errors = _run_concurrently(fire, count=4)
    assert errors == []
    assert engine.delayed_pending in (0, 1)  # never stacked duplicates


# -- daemon: concurrent diff ---------------------------------------------------------

def _daemon(tmp_path) -> Daemon:
    from omnibutler.core.audit import AuditLog

    audit = AuditLog(path=tmp_path / "audit.jsonl")
    driver = MockDriver()
    manager = DeviceManager(drivers={driver.name: driver}, audit=audit)
    confirmations = ConfirmationQueue(path=tmp_path / "confirmations.json")
    engine = SceneEngine(manager, confirmations)
    runtime = Runtime(manager=manager, engine=engine,
                      confirmations=confirmations, bus=manager.bus,
                      driver_name=driver.name)
    return Daemon(runtime, poll_interval=3600, tick_seconds=1)


def test_concurrent_diff_state_dispatches_each_change_once(tmp_path):
    daemon = _daemon(tmp_path)
    daemon._diff_state("living_light", {"onoff": False})  # baseline
    assert daemon.stats["state_changes"] == 0

    # Poller and push thread report the same new state at once: the
    # change must be counted and dispatched exactly once.
    _, errors = _run_concurrently(
        lambda i: daemon._diff_state("living_light", {"onoff": True})
    )
    assert errors == []
    assert daemon.stats["state_changes"] == 1
    assert daemon._snapshot["living_light"] == {"onoff": True}

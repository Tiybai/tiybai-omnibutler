"""Tests for the daemon single-instance lock (instance_lock + daemon)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from omnibutler.core.confirmations import default_state_dir
from omnibutler.daemon import Daemon
from omnibutler.instance_lock import (
    LOCK_FILENAME,
    DaemonAlreadyRunning,
    InstanceLock,
    pid_alive,
)
from omnibutler.runtime import build_runtime


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _write_lock(state_dir, pid, token="other-token") -> None:
    (state_dir / LOCK_FILENAME).write_text(
        json.dumps({"pid": pid, "token": token, "started_at": 0}),
        encoding="utf-8")


# -- pid_alive -------------------------------------------------------------

def test_pid_alive_for_self_and_dead():
    assert pid_alive(os.getpid())
    assert not pid_alive(_dead_pid())
    assert not pid_alive(0)
    assert not pid_alive(-3)


# -- InstanceLock ------------------------------------------------------------

def test_acquire_creates_lock_with_our_pid(tmp_path):
    lock = InstanceLock(tmp_path)
    lock.acquire()
    data = json.loads(lock.path.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert lock.owner_pid() == os.getpid()
    lock.release()
    assert not lock.path.exists()


def test_second_acquire_refused_with_pid_and_path(tmp_path):
    first = InstanceLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(DaemonAlreadyRunning) as excinfo:
            InstanceLock(tmp_path).acquire()
        message = str(excinfo.value)
        assert str(os.getpid()) in message  # names the holder
        assert LOCK_FILENAME in message     # and the lock path
    finally:
        first.release()


def test_stale_lock_is_taken_over(tmp_path, capsys):
    _write_lock(tmp_path, _dead_pid())
    lock = InstanceLock(tmp_path)
    lock.acquire()
    assert lock.owner_pid() == os.getpid()
    assert "stale" in capsys.readouterr().err
    lock.release()


def test_corrupt_lock_is_taken_over(tmp_path):
    (tmp_path / LOCK_FILENAME).write_text("this is not json",
                                          encoding="utf-8")
    lock = InstanceLock(tmp_path)
    lock.acquire()
    assert lock.owner_pid() == os.getpid()
    lock.release()


def test_release_never_removes_a_successors_lock(tmp_path):
    lock = InstanceLock(tmp_path)
    lock.acquire()
    # A successor took over (different token): our release must not
    # delete *its* lock file.
    _write_lock(tmp_path, os.getpid(), token="successor-token")
    lock.release()
    assert lock.path.exists()
    lock.path.unlink()


def test_context_manager(tmp_path):
    with InstanceLock(tmp_path) as lock:
        assert lock.path.exists()
    assert not lock.path.exists()


# -- daemon integration --------------------------------------------------------

def test_daemon_refuses_to_start_while_lock_is_held():
    state_dir = default_state_dir()  # conftest points this at a tmp dir
    held = InstanceLock(state_dir)
    held.acquire()
    try:
        daemon = Daemon(build_runtime(driver="mock"), tick_seconds=0.01)
        with pytest.raises(DaemonAlreadyRunning):
            daemon.run(max_ticks=1)
    finally:
        held.release()


def test_daemon_releases_lock_on_exit():
    state_dir = default_state_dir()
    daemon = Daemon(build_runtime(driver="mock"), tick_seconds=0.01)
    stats = daemon.run(max_ticks=2)
    assert stats["ticks"] == 2
    assert not (state_dir / LOCK_FILENAME).exists()


def test_daemon_takes_over_stale_lock():
    state_dir = default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    _write_lock(state_dir, _dead_pid())
    daemon = Daemon(build_runtime(driver="mock"), tick_seconds=0.01)
    stats = daemon.run(max_ticks=1)
    assert stats["ticks"] == 1
    assert not (state_dir / LOCK_FILENAME).exists()

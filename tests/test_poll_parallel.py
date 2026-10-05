"""Daemon polling, v0.10 line C: per-device error isolation and
driver-parallel rounds.

Pins three behaviours of _maybe_poll / _poll_driver:

* one device whose get_state raises costs only that device its round -
  the rest of its driver is still polled and diffed;
* drivers are polled in parallel on a bounded pool (devices within a
  driver stay serial), so a round of slow drivers finishes in roughly
  the slowest driver's time, not the sum - with state results identical
  to the serial loop;
* ``OMNIBUTLER_POLL_SERIAL=1`` restores the serial loop.
"""

from __future__ import annotations

import datetime
import time

from omnibutler.core.audit import AuditLog
from omnibutler.core.confirmations import ConfirmationQueue
from omnibutler.core.manager import DeviceManager
from omnibutler.core.models import Device
from omnibutler.daemon import Daemon
from omnibutler.drivers.base import Driver
from omnibutler.runtime import Runtime
from omnibutler.scenes.engine import SceneEngine


class SlowDriver(Driver):
    """A fake driver whose get_state sleeps, then reports a counter."""

    def __init__(self, name, device_ids, delay=0.25, fail_ids=()):
        self.name = name
        self._delay = delay
        self._fail = set(fail_ids)
        self._counts: dict[str, int] = {}
        self._devices = [
            Device(id=d, name=d, driver=name) for d in device_ids
        ]

    def discover(self):
        return self.list_devices()

    def list_devices(self):
        return list(self._devices)

    def get_state(self, device_id):
        if self._delay:
            time.sleep(self._delay)
        if device_id in self._fail:
            raise RuntimeError(f"{device_id} is dead")
        self._counts[device_id] = self._counts.get(device_id, 0) + 1
        return {"counter": self._counts[device_id]}

    def set_property(self, device_id, property_name, value):
        return {}

    def call_action(self, device_id, action, params):
        return {}


def _daemon(tmp_path, drivers) -> Daemon:
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    manager = DeviceManager(
        drivers={d.name: d for d in drivers}, audit=audit)
    confirmations = ConfirmationQueue(path=tmp_path / "confirmations.json")
    engine = SceneEngine(manager, confirmations)
    runtime = Runtime(manager=manager, engine=engine,
                      confirmations=confirmations, bus=manager.bus,
                      driver_name=drivers[0].name)
    return Daemon(runtime, poll_interval=0)


def _poll_twice(daemon: Daemon) -> float:
    """Two poll rounds (baseline + change); returns the second's seconds."""
    now = datetime.datetime.now()
    daemon._maybe_poll(now)
    start = time.perf_counter()
    daemon._maybe_poll(now)
    return time.perf_counter() - start


def test_one_dead_device_does_not_blind_its_driver(tmp_path):
    driver = SlowDriver("slow", ["dead", "alive"], delay=0, fail_ids={"dead"})
    daemon = _daemon(tmp_path, [driver])
    _poll_twice(daemon)
    # The healthy device was read and diffed on both rounds...
    assert daemon._snapshot["alive"] == {"counter": 2}
    assert daemon.stats["state_changes"] == 1  # alive: 1 -> 2
    # ...while the dead one cost one audited error per round and never
    # entered the snapshot - and nothing propagated.
    assert "dead" not in daemon._snapshot
    assert daemon.stats["errors"] == 2
    errors = [e for e in daemon.audit.read_all()
              if e["action"] == "daemon:poll_error"]
    assert len(errors) == 2
    assert all(e["params"] == {"device": "dead"} for e in errors)


def test_drivers_poll_in_parallel_with_identical_results(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIBUTLER_POLL_SERIAL", raising=False)

    def build():
        return [SlowDriver(f"d{i}", [f"dev{i}"], delay=0.25)
                for i in range(4)]

    # Two identical setups: one forced serial, one default (parallel).
    monkeypatch.setenv("OMNIBUTLER_POLL_SERIAL", "1")
    serial_daemon = _daemon(tmp_path, build())
    serial_s = _poll_twice(serial_daemon)
    monkeypatch.delenv("OMNIBUTLER_POLL_SERIAL")
    parallel_drivers = build()
    parallel_daemon = _daemon(tmp_path, parallel_drivers)
    parallel_s = _poll_twice(parallel_daemon)

    # 4 drivers x 0.25 s: the serial round is the sum (~1 s), the
    # parallel round is roughly the slowest driver (~0.25 s).
    assert serial_s >= 0.9
    assert parallel_s < 0.6 * serial_s
    # And the outcomes are exactly the serial ones.
    assert parallel_daemon._snapshot == serial_daemon._snapshot
    assert parallel_daemon.stats["state_changes"] == \
        serial_daemon.stats["state_changes"] == 4
    assert all(parallel_daemon._snapshot[f"dev{i}"] == {"counter": 2}
               for i in range(4))


def test_poll_serial_env_keeps_round_serial(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBUTLER_POLL_SERIAL", "1")
    drivers = [SlowDriver(f"s{i}", [f"sdev{i}"], delay=0.2) for i in range(2)]
    daemon = _daemon(tmp_path, drivers)
    elapsed = _poll_twice(daemon)
    assert elapsed >= 0.35  # 2 x 0.2 s, one after another
    assert daemon._snapshot["sdev0"] == {"counter": 2}
    assert daemon._snapshot["sdev1"] == {"counter": 2}

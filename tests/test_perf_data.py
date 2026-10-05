"""Data-layer performance fixes (v0.10, line C) - the semantics pins.

Each fix in this batch was benchmark-driven (streaming audit reads,
lazy stream-store loading, O(1) in-order appends, Broadlink code-store
atomicity/self-healing, the Matter node-list cache). The wall-clock
numbers live in the line report; these tests pin the behaviour that
must hold while the fast paths are in place:

* audit iteration yields exactly what read_all used to, torn lines
  are skipped *and counted*, and ``tob audit`` survives them;
* a StreamStore pays its replay on first use, never at construction -
  and an append that happens before any read cannot lose the history
  already on disk;
* appends keep a series sorted (in-order fast path, out-of-order
  insert) and agree with a from-disk reload;
* a corrupt Broadlink codes file is quarantined, not fatal, and saves
  are atomic;
* Matter get_state reuses the node list within the TTL, while writes,
  commissions and discover() always see fresh data.
"""

from __future__ import annotations

import copy
import json
import sys
import types
from pathlib import Path

import pytest

from omnibutler.core.audit import AuditLog
from omnibutler.core.streams import DataStream, StreamStore

# ---------------------------------------------------------------------------
# 1. Audit streaming reads
# ---------------------------------------------------------------------------


def _filled_log(tmp_path: Path, n: int = 30) -> AuditLog:
    path = tmp_path / "audit.jsonl"
    # keep=5 matches the CLI's default AuditLog, so tests that go
    # through `tob audit` see every retained backup.
    log = AuditLog(path=path, max_bytes=2000, keep=5)
    for i in range(n):
        log.record("tester", "dev", "set", params={"i": i, "pad": "x" * 40})
    assert (tmp_path / "audit.jsonl.1").exists()  # rotation really happened
    return log


def test_iter_entries_matches_read_all_across_rotations(tmp_path):
    log = _filled_log(tmp_path)
    streamed = list(log.iter_entries())
    assert streamed == log.read_all()
    assert [e["params"]["i"] for e in streamed] == list(range(30))
    assert log.last_read_skipped == 0


def test_iter_entries_is_a_streaming_iterator(tmp_path):
    log = _filled_log(tmp_path)
    it = log.iter_entries()
    assert iter(it) is it  # an iterator, not a materialised list
    assert next(it)["params"]["i"] == 0
    assert [e["params"]["i"] for e in it] == list(range(1, 30))


def test_torn_lines_are_skipped_and_counted(tmp_path, capsys):
    log = _filled_log(tmp_path)
    # A write cut off mid-line (killed process) at the tail of the live
    # file, a non-object JSON line, and garbage inside a rotated backup.
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": 1, "agent": "tor')  # torn, no newline
        fh.write("\n42\n")  # valid JSON, not an entry object
    with (tmp_path / "audit.jsonl.1").open("a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
    entries = AuditLog(path=log.path, max_bytes=2000, keep=5).read_all()
    assert [e["params"]["i"] for e in entries] == list(range(30))
    fresh = AuditLog(path=log.path, max_bytes=2000, keep=5)
    fresh.read_all()
    assert fresh.last_read_skipped == 3
    assert "skipped 3" in capsys.readouterr().err


def test_cmd_audit_survives_a_torn_tail(tmp_path, capsys):
    from omnibutler.cli import main

    log = _filled_log(tmp_path)
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": 1, "agent": "tor')
    assert main(["--audit", str(log.path), "audit", "--last", "3"]) == 0
    out = capsys.readouterr().out
    assert '"i": 29' in out
    # tail()'s total counts retained lines, so the torn line is in the
    # 31 even though only 30 entries parse (see AuditLog.tail).
    assert "(3 shown of 31 matching, 31 total" in out


def test_tail_collects_newest_first_across_rotations(tmp_path):
    log = _filled_log(tmp_path)
    shown, total = log.tail(3)
    assert [e["params"]["i"] for e in shown] == [27, 28, 29]  # oldest first
    assert total == 30
    shown, total = log.tail(100)  # more than the log holds
    assert [e["params"]["i"] for e in shown] == list(range(30))
    assert total == 30
    shown, total = log.tail(0)
    assert shown == [] and total == 30


def test_tail_skips_and_counts_a_torn_live_tail(tmp_path, capsys):
    log = _filled_log(tmp_path)
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": 1, "agent": "tor')  # torn tail of the live file
    shown, _total = log.tail(2)
    assert [e["params"]["i"] for e in shown] == [28, 29]
    assert log.last_read_skipped == 1
    assert "skipped 1" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 2 + 3. StreamStore lazy loading and append ordering
# ---------------------------------------------------------------------------

STEPS = DataStream(id="phone-steps", kind="health.steps", source="phone")


def _seed(path: Path, n: int = 5) -> StreamStore:
    store = StreamStore(path=path)
    for i in range(n):
        store.append("phone-steps", i, ts=float(i + 1), stream=STEPS)
    return store


def test_construction_does_not_load(tmp_path):
    path = tmp_path / "streams.jsonl"
    _seed(path, 5)
    store = StreamStore(path=path)
    assert store._loaded is False
    # A line appended behind the store's back (another process writing)
    # is still seen by the first read - the replay really is deferred
    # to first use, not skipped.
    record = {"stream": STEPS.to_dict(), "stream_id": "phone-steps",
              "ts": 99.0, "value": 99, "meta": {}}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    assert store.latest("phone-steps").value == 99
    assert store._loaded is True
    assert len(store) == 6
    assert [s.id for s in store.streams()] == ["phone-steps"]


def test_append_before_any_read_preserves_on_disk_history(tmp_path):
    """The pure-write path: append must load first, never write blind.

    If append skipped the load, the in-memory mirror would hold only
    the new point while the file held all six - and latest()/history()
    would disagree with a fresh reload.
    """
    path = tmp_path / "streams.jsonl"
    _seed(path, 5)
    store = StreamStore(path=path)  # constructed, never read
    store.append("phone-steps", 50, ts=50.0)  # descriptor comes from disk
    values = [p.value for p in store.history("phone-steps")]
    assert values == [0, 1, 2, 3, 4, 50]
    fresh = StreamStore(path=path)
    assert [p.value for p in fresh.history("phone-steps")] == values
    assert len(path.read_text(encoding="utf-8").splitlines()) == 6


def test_register_before_first_read_keeps_both(tmp_path):
    path = tmp_path / "streams.jsonl"
    _seed(path, 3)
    store = StreamStore(path=path)
    other = DataStream(id="watch-hr", kind="health.heart_rate", source="watch")
    store.register(other)  # a write-side call before any read
    store.append("watch-hr", 61, ts=10.0)
    assert {s.id for s in store.streams()} == {"phone-steps", "watch-hr"}
    assert len(store) == 4


def test_long_lived_store_mixed_cycles_stay_consistent(tmp_path):
    """The daemon/gateway pattern: one store, interleaved reads/writes."""
    path = tmp_path / "streams.jsonl"
    store = StreamStore(path=path)
    store.register(STEPS)
    for round_ in range(3):
        for i in range(4):
            store.append("phone-steps", round_ * 10 + i,
                         ts=float(round_ * 10 + i + 1))
        assert store.latest("phone-steps").value == round_ * 10 + 3
    history = store.history("phone-steps")
    assert [p.ts for p in history] == sorted(p.ts for p in history)
    fresh = StreamStore(path=path)
    assert [p.value for p in fresh.history("phone-steps")] == \
        [p.value for p in history]


def test_append_keeps_series_sorted_fast_and_slow_paths(tmp_path):
    store = StreamStore(path=tmp_path / "streams.jsonl")
    store.register(STEPS)
    for ts in (10.0, 20.0, 30.0):
        store.append("phone-steps", ts, ts=ts)  # in-order fast path
    store.append("phone-steps", 15.0, ts=15.0)  # out-of-order insert
    store.append("phone-steps", 30.0, ts=30.0)  # equal to the last ts
    store.append("phone-steps", 40.0, ts=40.0)  # in-order again
    expected = [10.0, 15.0, 20.0, 30.0, 30.0, 40.0]
    assert [p.ts for p in store.history("phone-steps")] == expected
    # A from-disk reload (the batch-sort path) agrees exactly.
    fresh = StreamStore(path=store.path)
    assert [p.ts for p in fresh.history("phone-steps")] == expected


# ---------------------------------------------------------------------------
# 4. Broadlink code store: atomic saves, corrupt-file self-healing
# ---------------------------------------------------------------------------

from omnibutler.drivers.broadlink import BroadlinkDriver  # noqa: E402


def _codes_driver(tmp_path: Path) -> BroadlinkDriver:
    return BroadlinkDriver(devices=[], codes_file=tmp_path / "codes.json")


def test_corrupt_codes_file_is_quarantined_not_fatal(tmp_path, capsys):
    codes = tmp_path / "codes.json"
    codes.write_text("{not json", encoding="utf-8")
    driver = _codes_driver(tmp_path)
    assert driver._load_codes() == {}  # empty table, no exception
    assert not codes.exists()  # moved aside, not silently overwritten
    quarantined = tmp_path / "codes.json.corrupt"
    assert quarantined.read_text(encoding="utf-8") == "{not json"
    err = capsys.readouterr().err
    assert str(codes) in err and "empty code table" in err
    # The driver stays usable: a later save writes a fresh, valid file.
    driver._codes = {"blaster": {"tv_on": "JgAB"}}
    driver._save_codes()
    assert json.loads(codes.read_text(encoding="utf-8")) == \
        {"blaster": {"tv_on": "JgAB"}}


def test_non_dict_codes_file_is_quarantined(tmp_path):
    codes = tmp_path / "codes.json"
    codes.write_text("[1, 2, 3]", encoding="utf-8")  # valid JSON, wrong shape
    driver = _codes_driver(tmp_path)
    assert driver._load_codes() == {}
    assert (tmp_path / "codes.json.corrupt").exists()


def test_second_corruption_keeps_the_first_quarantine(tmp_path):
    codes = tmp_path / "codes.json"
    codes.write_text("bad one", encoding="utf-8")
    _codes_driver(tmp_path)._load_codes()
    codes.write_text("bad two", encoding="utf-8")
    _codes_driver(tmp_path)._load_codes()
    assert (tmp_path / "codes.json.corrupt").read_text(encoding="utf-8") \
        == "bad one"
    assert (tmp_path / "codes.json.corrupt.1").read_text(encoding="utf-8") \
        == "bad two"


def test_save_codes_is_atomic_and_leaves_no_tmp_files(tmp_path):
    driver = _codes_driver(tmp_path)
    driver._codes = {"a": {"x": "AA=="}}
    driver._save_codes()
    driver._codes = {"a": {"x": "BB=="}}
    driver._save_codes()  # a replace, never a partial in-place rewrite
    assert json.loads((tmp_path / "codes.json").read_text(encoding="utf-8")) \
        == {"a": {"x": "BB=="}}
    assert list(tmp_path.glob("*.tmp-*")) == []


# ---------------------------------------------------------------------------
# 5b. Matter node-list cache
# ---------------------------------------------------------------------------


def _matter_nodes() -> list[dict]:
    return [{
        "node_id": 1,
        "available": True,
        "attributes": {
            "0/40/1": "Acme",
            "0/40/3": "Smart Bulb",
            "0/40/5": "Ceiling bulb",
            "1/6/0": True,   # OnOff, endpoint 1
            "1/8/0": 128,   # LevelControl
        },
    }]


@pytest.fixture()
def matter(monkeypatch):
    """A MatterDriver wired to a counting fake _rpc (no real sockets)."""
    monkeypatch.setitem(sys.modules, "websockets", types.ModuleType("websockets"))
    from omnibutler.drivers.matter import MatterDriver

    driver = MatterDriver(
        server_url="ws://127.0.0.1:5599/ws",
        nodes=[{"node_id": 1, "name": "Lamp", "room": "living"}],
    )
    server_nodes = _matter_nodes()
    calls: list[str] = []

    def fake_rpc(command, args=None):
        calls.append(command)
        if command == "get_nodes":
            return copy.deepcopy(server_nodes)
        if command == "send_device_command" and args["command_name"] == "Off":
            server_nodes[0]["attributes"]["1/6/0"] = False
        return {}

    driver._rpc = fake_rpc
    return driver, calls, server_nodes


def test_matter_get_state_reuses_the_cached_node_list(matter):
    driver, calls, _server = matter
    driver.discover()
    assert calls.count("get_nodes") == 1
    assert driver.get_state("matter-1-1")["onoff"] is True
    assert driver.get_state("matter-1-1")["onoff"] is True
    # Both reads were served by the one fetch discover() made.
    assert calls.count("get_nodes") == 1


def test_matter_write_invalidates_the_node_cache(matter):
    driver, calls, _server = matter
    driver.discover()
    driver.set_property("matter-1-1", "onoff", False)
    # The write invalidated the cache, so this read refetches and sees
    # the server-side change instead of the stale cached attributes.
    assert driver.get_state("matter-1-1")["onoff"] is False
    assert calls.count("get_nodes") == 2


def test_matter_discover_and_ttl_expiry_refetch(matter):
    driver, calls, _server = matter
    driver.discover()
    driver.discover()  # discovery is the explicit refresh: always fresh
    assert calls.count("get_nodes") == 2
    driver._nodes_cache_at -= 3600  # pretend the TTL has long passed
    driver.get_state("matter-1-1")
    assert calls.count("get_nodes") == 3

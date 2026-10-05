"""Data streams (core/streams.py) and the phone gateway (gateway.py).

Streams: register/append/latest/history/kind query, JSONL persistence
across store instances, and the store's refusal modes. Gateway: the auth
gate over a real socket, ingest round-trips, and the end-to-end geofence
chain - a phone-shaped POST /event must drive a real scene on the mock
driver through the EventBus, with the exact event shape the scene
engine matches on ({zone, transition}).
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from omnibutler.core.events import EventBus
from omnibutler.core.streams import DataStream, StreamStore
from omnibutler.gateway import (
    TOKEN_ENV_VAR,
    GatewayError,
    build_event,
    create_http_server,
    ingest_points,
    serve,
)

TOKEN = "test-gateway-token-not-a-real-secret"

STEPS = DataStream(id="phone-steps", kind="health.steps",
                   source="phone", unit="count")
SLEEP = DataStream(id="watch-sleep", kind="health.sleep",
                   source="watch", unit="minutes")


# -- StreamStore -----------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    return StreamStore(path=tmp_path / "streams.jsonl")


def _fill(store):
    store.register(STEPS)
    store.register(SLEEP)
    store.append("phone-steps", 100, ts=1000.0)
    store.append("phone-steps", 250, ts=2000.0, meta={"origin": "healthkit"})
    store.append("phone-steps", 400, ts=3000.0)
    store.append("watch-sleep", 432, ts=2500.0)
    return store


def test_append_latest_and_history(store):
    _fill(store)
    latest = store.latest("phone-steps")
    assert latest.value == 400 and latest.ts == 3000.0
    history = store.history("phone-steps")
    assert [p.value for p in history] == [100, 250, 400]
    assert history[1].meta == {"origin": "healthkit"}
    windowed = store.history("phone-steps", since=1500.0, until=2500.0)
    assert [p.value for p in windowed] == [250]
    assert store.latest("watch-sleep").value == 432
    assert store.latest("no-such-stream") is None
    assert len(store) == 4


def test_query_by_kind(store):
    _fill(store)
    assert [s.id for s in store.streams()] == ["phone-steps", "watch-sleep"]
    assert [s.id for s in store.streams(kind="health.steps")] == ["phone-steps"]
    points = store.query(kind="health.sleep")
    assert len(points) == 1 and points[0].stream_id == "watch-sleep"
    merged = store.query(since=2000.0)
    assert [p.ts for p in merged] == [2000.0, 2500.0, 3000.0]


def test_append_registers_via_descriptor_and_sorts(store):
    point = store.append("late-stream", 7, ts=500.0,
                         stream=DataStream(id="late-stream", kind="presence"))
    assert point.ts == 500.0
    store.append("late-stream", 9, ts=100.0)  # out-of-order ts stays sorted
    assert [p.value for p in store.history("late-stream")] == [9, 7]


def test_append_unknown_stream_raises(store):
    with pytest.raises(KeyError):
        store.append("ghost", 1)


def test_unserialisable_value_stores_nothing(store):
    store.register(STEPS)
    with pytest.raises(TypeError):
        store.append("phone-steps", {"bad": object()})
    assert len(store) == 0
    assert store.latest("phone-steps") is None


def test_stream_descriptor_validation():
    with pytest.raises(ValueError):
        DataStream(id="", kind="health.steps")
    with pytest.raises(ValueError):
        DataStream(id="x", kind="")


def test_persistence_round_trip(tmp_path):
    path = tmp_path / "streams.jsonl"
    first = _fill(StreamStore(path=path))
    assert len(first) == 4

    second = StreamStore(path=path)  # replay the same JSONL file
    assert [s.id for s in second.streams()] == ["phone-steps", "watch-sleep"]
    stream = second.get_stream("phone-steps")
    assert (stream.kind, stream.source, stream.unit) == (
        "health.steps", "phone", "count")
    assert [p.value for p in second.history("phone-steps")] == [100, 250, 400]
    assert second.history("phone-steps")[1].meta == {"origin": "healthkit"}
    # The reopened store keeps appending to the same file.
    second.append("phone-steps", 500, ts=4000.0)
    assert StreamStore(path=path).latest("phone-steps").value == 500


def test_torn_lines_do_not_kill_reload(tmp_path):
    path = tmp_path / "streams.jsonl"
    _fill(StreamStore(path=path))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"stream": {"id": "phone-steps", "kind": "health.')
        fh.write("\nnot json at all\n")
    reloaded = StreamStore(path=path)
    assert len(reloaded) == 4


# -- payload helpers ---------------------------------------------------------------


def test_build_event_geofence_shape_is_engine_shape():
    event = build_event({"type": "geofence", "zone": "home",
                         "transition": "enter", "ts": 1234.5})
    assert event.type == "geofence"
    assert event.source == "phone"
    # SceneEngine._trigger_matches reads exactly these two keys.
    assert event.data == {"zone": "home", "transition": "enter"}
    assert event.timestamp == 1234.5


def test_build_event_passthrough_and_validation():
    event = build_event({"type": "presence", "person": "zhou", "present": True})
    assert event.type == "presence" and event.source == "phone"
    assert event.data == {"person": "zhou", "present": True}
    for bad in (
        {"type": "geofence", "transition": "enter"},            # no zone
        {"type": "geofence", "zone": "home"},                   # no transition
        {"type": "geofence", "zone": "home", "transition": "sideways"},
        {"zone": "home", "transition": "enter"},                # no type
        {"type": ""},
        "not an object",
    ):
        with pytest.raises(ValueError):
            build_event(bad)


def test_ingest_points_both_payload_forms(store):
    accepted = ingest_points(store, {
        "stream": {"id": "phone-steps", "kind": "health.steps",
                   "source": "phone", "unit": "count"},
        "points": [{"ts": 1000.0, "value": 100}, {"ts": 2000.0, "value": 260}],
    })
    assert accepted == 2
    accepted = ingest_points(store, {"points": [
        {"stream": {"id": "watch-sleep", "kind": "health.sleep"},
         "ts": 2000.0, "value": 432},
        {"stream_id": "phone-steps", "ts": 3000.0, "value": 400},
    ]})
    assert accepted == 2
    assert [p.value for p in store.history("phone-steps")] == [100, 260, 400]
    assert store.latest("watch-sleep").value == 432


def test_ingest_points_rejects_bad_batches_atomically(store):
    for bad in (
        {"points": [{"stream": {"id": "s", "kind": "k"}}]},        # no value
        {"points": [{"stream_id": "ghost", "value": 1}]},          # unknown
        {"points": [{"value": 1}]},                                # no stream
        {"points": [{"stream": {"id": "s", "kind": "k"},
                     "value": 1, "ts": "soon"}]},                  # bad ts
        {"points": "not-a-list"},
        {"no_points": []},
        ["not", "an", "object"],
    ):
        with pytest.raises(ValueError):
            ingest_points(store, bad)
    assert len(store) == 0


# -- HTTP gateway over a real socket ---------------------------------------------


@pytest.fixture()
def bus():
    return EventBus()


@pytest.fixture()
def gateway(bus, store):
    httpd = create_http_server(bus, store, host="127.0.0.1", port=0,
                               token=TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(url, data=None, headers=None, method=None):
    req = urllib.request.Request(
        url, data=data, headers=headers or {},
        method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, None


def _post(base, path, payload, token=TOKEN):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return _request(base + path, data=json.dumps(payload).encode(),
                    headers=headers)


def test_health_needs_no_auth(gateway):
    status, payload = _request(gateway + "/health")
    assert status == 200
    assert payload == {"status": "ok"}


def test_auth_gate_three_states(gateway):
    payload = {"type": "presence", "present": True}
    status, _ = _post(gateway, "/event", payload, token=None)
    assert status == 401
    status, _ = _post(gateway, "/event", payload, token="nope")
    assert status == 401
    status, body = _post(gateway, "/event", payload)
    assert status == 200
    assert body == {"status": "published", "type": "presence"}


def test_refuses_to_start_without_token(bus, store, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    with pytest.raises(GatewayError) as exc:
        create_http_server(bus, store, host="127.0.0.1", port=0)
    assert TOKEN_ENV_VAR in str(exc.value)
    with pytest.raises(GatewayError):
        serve(bus, store, host="127.0.0.1", port=0)


def test_token_can_come_from_env(bus, store, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV_VAR, TOKEN)
    httpd = create_http_server(bus, store, "127.0.0.1", 0)
    try:
        assert httpd.server_address[1] > 0
    finally:
        httpd.server_close()


def test_ingest_over_http(gateway, store):
    status, body = _post(gateway, "/ingest", {
        "stream": {"id": "phone-steps", "kind": "health.steps",
                   "source": "phone", "unit": "count"},
        "points": [{"ts": 1000.0, "value": 100}, {"ts": 2000.0, "value": 260}],
    })
    assert status == 200
    assert body == {"status": "ok", "accepted": 2}
    assert store.latest("phone-steps").value == 260


def test_bad_payloads_are_400(gateway):
    # geofence without a transition
    status, _ = _post(gateway, "/event",
                      {"type": "geofence", "zone": "home"})
    assert status == 400
    # ingest point without a value
    status, _ = _post(gateway, "/ingest", {
        "stream": {"id": "s", "kind": "k"}, "points": [{"ts": 1.0}]})
    assert status == 400
    # body that is not JSON at all
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {TOKEN}"}
    status, _ = _request(gateway + "/event", data=b"{not json",
                         headers=headers)
    assert status == 400
    # body that is JSON but not an object
    status, _ = _post(gateway, "/event", ["geofence"])
    assert status == 400
    # unknown path
    status, _ = _post(gateway, "/nope", {"type": "presence"})
    assert status == 404


def test_event_reaches_bus_with_phone_source(gateway, bus):
    seen = []
    bus.subscribe("presence", seen.append)
    status, _ = _post(gateway, "/event",
                      {"type": "presence", "person": "zhou", "present": True})
    assert status == 200
    assert len(seen) == 1
    assert seen[0].source == "phone"
    assert seen[0].data == {"person": "zhou", "present": True}


# -- the geofence chain: phone event -> bus -> scene -> device --------------------


def test_geofence_event_drives_arrive_home_scene(gateway, bus, engine, manager):
    engine.attach(bus)
    assert manager.get_state("living_ac")["onoff"] is False

    status, body = _post(gateway, "/event", {
        "type": "geofence", "zone": "home", "transition": "enter"})
    assert status == 200
    assert body == {"status": "published", "type": "geofence"}

    # arrive-home ran end to end on the mock driver.
    state = manager.get_state("living_ac")
    assert state["onoff"] is True
    assert state["target_temperature"] == 26
    assert manager.get_state("air_purifier")["onoff"] is True

    # ...and the matching exit event runs leave-home-check.
    status, _ = _post(gateway, "/event", {
        "type": "geofence", "zone": "home", "transition": "exit"})
    assert status == 200
    assert manager.get_state("living_ac")["onoff"] is False
    assert manager.get_state("air_purifier")["onoff"] is False


def test_geofence_wrong_zone_does_not_trigger(gateway, bus, engine, manager):
    engine.attach(bus)
    status, _ = _post(gateway, "/event", {
        "type": "geofence", "zone": "office", "transition": "enter"})
    assert status == 200  # valid event, just no scene listens for it
    assert manager.get_state("living_ac")["onoff"] is False

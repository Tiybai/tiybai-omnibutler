"""High-risk confirmation is a human-only, out-of-band action.

Regression tests for the guardrail hole where the MCP server exposed a
``confirm_action`` tool: the only thing stopping an agent from approving
its own queued high-risk actions was a sentence in the tool description.
Approval now happens exclusively in a terminal on the host
(``tob confirm <id>`` / ``tob reject <id>``), and the queue is persisted
to disk so the MCP server, the CLI and any other process share it.
"""

import json

import pytest

from omnibutler import cli
from omnibutler.core.audit import AuditLog
from omnibutler.core.confirmations import ConfirmationQueue
from omnibutler.core.events import Event


def _garage_event():
    return Event(type="geofence", data={"zone": "garage_gate", "transition": "enter"})


def _seed_pending() -> "object":
    """Park one garage-door confirmation in the shared (persisted) queue."""
    return ConfirmationQueue().add(
        device_id="garage_door", kind="call_action", name="open", params={},
        requested_by="scene:garage-arrival", scene="garage-arrival", risk="high",
    )


# -- MCP side: read-only, cannot approve --------------------------------------

@pytest.fixture()
def mcp_server(manager, engine):
    from omnibutler.mcp_server.server import create_server

    return create_server(manager, engine=engine,
                         confirmations=engine.confirmations)


def _rpc(server, method, params=None, msg_id=1):
    return server.handle({"jsonrpc": "2.0", "id": msg_id,
                          "method": method, "params": params or {}})


def _call(server, name, arguments):
    response = _rpc(server, "tools/call", {"name": name, "arguments": arguments})
    return (json.loads(response["result"]["content"][0]["text"]),
            response["result"]["isError"])


def test_mcp_tool_list_has_no_confirm_action(mcp_server):
    names = {t["name"] for t in _rpc(mcp_server, "tools/list")["result"]["tools"]}
    assert "confirm_action" not in names
    assert "get_pending_confirmations" in names


def test_mcp_cannot_confirm_or_reject(mcp_server, engine, manager):
    engine.handle_event(_garage_event())
    item = engine.confirmations.pending()[0]

    # The read-only view works fine.
    payload, is_error = _call(mcp_server, "get_pending_confirmations", {})
    assert not is_error and [p["id"] for p in payload] == [item.id]

    # Approving through MCP is refused with the human-only error...
    payload, is_error = _call(mcp_server, "confirm_action",
                              {"confirmation_id": item.id, "approve": True})
    assert is_error
    assert "human-only" in payload["error"]
    assert "tob confirm <id>" in payload["error"]
    # ...and so is rejecting.
    payload, is_error = _call(mcp_server, "confirm_action",
                              {"confirmation_id": item.id, "approve": False})
    assert is_error
    assert "human-only" in payload["error"]

    # Nothing executed and the item is still waiting for a human.
    assert manager.get_state("garage_door")["open_close"] is False
    assert [p.id for p in engine.confirmations.pending()] == [item.id]


# -- persistence: the queue is shared across processes -------------------------

def test_queue_survives_new_instance():
    first = ConfirmationQueue()
    item = _seed_pending()
    assert first.path.exists()
    assert first.path.name == "confirmations.json"

    # A brand-new instance (i.e. another process) sees the same item.
    second = ConfirmationQueue()
    loaded = second.get(item.id)
    assert loaded is not None
    assert loaded.status == "pending"
    assert loaded.device_id == "garage_door"
    assert loaded.name == "open"
    assert [p.id for p in second.pending()] == [item.id]

    # Ids keep climbing across instances instead of colliding.
    other = second.add(device_id="garage_door", kind="call_action", name="close")
    assert other.id != item.id


# -- CLI side: the human channel ------------------------------------------------

def test_cli_pending_lists_item(capsys):
    item = _seed_pending()
    assert cli.main(["pending"]) == 0
    out = capsys.readouterr().out
    assert item.id in out
    assert "garage_door" in out
    assert "risk=high" in out
    assert "waiting=" in out


def test_cli_confirm_executes_and_audits(tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("TOB_AUDIT_PATH", str(audit_path))
    item = _seed_pending()

    assert cli.main(["confirm", item.id]) == 0

    queue = ConfirmationQueue()
    assert queue.pending() == []
    assert queue.get(item.id).status == "confirmed"

    entries = AuditLog(path=audit_path).read_all()
    approved = [e for e in entries if e["action"] == "confirmation:approved"]
    assert len(approved) == 1
    assert approved[0]["agent"] == "cli:human"
    executed = [e for e in entries
                if e["device"] == "garage_door" and e["action"] == "action:open"]
    assert len(executed) == 1
    assert executed[0]["ok"] is True
    assert executed[0]["agent"] == "cli:human"


def test_cli_reject_then_confirm_is_refused(tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("TOB_AUDIT_PATH", str(audit_path))
    item = _seed_pending()

    assert cli.main(["reject", item.id]) == 0
    # A rejected item can no longer be confirmed.
    assert cli.main(["confirm", item.id]) == 1

    queue = ConfirmationQueue()
    assert queue.get(item.id).status == "rejected"
    entries = AuditLog(path=audit_path).read_all()
    assert "confirmation:rejected" in [e["action"] for e in entries]
    # And the door action was never executed.
    assert not [e for e in entries if e["action"] == "action:open"]


def test_cli_confirm_unknown_id_fails():
    _seed_pending()
    assert cli.main(["confirm", "cfm-9999"]) == 1
    assert cli.main(["reject", "cfm-9999"]) == 1

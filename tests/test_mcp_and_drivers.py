import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnibutler.core.errors import (
    DeviceNotFoundError,
    DriverNotConfiguredError,
    PlannedDriverError,
)
from omnibutler.drivers.homeassistant import HomeAssistantDriver
from omnibutler.drivers.miio import MiioDriver
from omnibutler.drivers.tuya import TuyaDriver

ROOT = Path(__file__).resolve().parent.parent


def test_planned_drivers_are_honest():
    driver = TuyaDriver()
    assert driver.list_devices() == []
    assert driver.discover() == []
    with pytest.raises(PlannedDriverError) as exc:
        driver.set_property("x", "onoff", True)
    assert "planned but not implemented" in str(exc.value)


def test_miio_driver_is_implemented_and_honest_when_unconfigured(monkeypatch):
    # miio shipped in v0.2: with no devices configured it is simply empty,
    # and unknown device ids are reported as such (no PlannedDriverError).
    monkeypatch.delenv("MIIO_DEVICES", raising=False)
    monkeypatch.delenv("MIIO_HOST", raising=False)
    monkeypatch.delenv("MIIO_TOKEN", raising=False)
    driver = MiioDriver()
    assert driver.list_devices() == []
    with pytest.raises(DeviceNotFoundError):
        driver.set_property("x", "onoff", True)


def test_homeassistant_requires_config(monkeypatch):
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    driver = HomeAssistantDriver(base_url="", token="")
    with pytest.raises(DriverNotConfiguredError) as exc:
        driver.list_devices()
    assert "HA_URL" in str(exc.value) and "HA_TOKEN" in str(exc.value)


def test_homeassistant_state_mapping():
    driver = HomeAssistantDriver(base_url="http://ha.local:8123", token="x")
    item = {
        "entity_id": "climate.living",
        "state": "cool",
        "attributes": {"friendly_name": "Living AC", "temperature": 24,
                       "current_temperature": 29, "hvac_mode": "cool"},
    }
    device = driver._device_from_state(item)
    assert device is not None
    assert device.name == "Living AC"
    assert device.state["target_temperature"] == 24
    assert device.state["onoff"] is True


# -- MCP ----------------------------------------------------------------------

def _rpc(server, method, params=None, msg_id=1):
    return server.handle({"jsonrpc": "2.0", "id": msg_id,
                          "method": method, "params": params or {}})


@pytest.fixture()
def mcp_server(manager, engine):
    from omnibutler.mcp_server.server import create_server

    return create_server(manager, engine=engine,
                         confirmations=engine.confirmations)


def test_mcp_initialize_and_tools_list(mcp_server):
    response = _rpc(mcp_server, "initialize")
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert response["result"]["serverInfo"]["name"] == "tiybai-omnibutler"
    tools = _rpc(mcp_server, "tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    # confirm_action is deliberately absent: approving a high-risk action is
    # a human-only, out-of-band step (`tob confirm <id>` on the host).
    assert names == {"list_devices", "get_device_state", "set_device_property",
                     "call_device_action", "list_scenes", "enable_scene",
                     "get_pending_confirmations", "list_data_streams",
                     "get_stream_data", "list_terminal_sessions",
                     "open_terminal_session", "close_terminal_session"}


def _call(mcp_server, name, arguments):
    response = _rpc(mcp_server, "tools/call",
                    {"name": name, "arguments": arguments})
    return json.loads(response["result"]["content"][0]["text"]), response["result"]["isError"]


def test_mcp_list_and_control_device(mcp_server, manager):
    payload, is_error = _call(mcp_server, "list_devices", {})
    assert not is_error and len(payload) >= 7
    payload, is_error = _call(mcp_server, "set_device_property",
                              {"device_id": "living_ac", "property": "onoff",
                               "value": True})
    assert not is_error and payload["ok"] is True
    assert manager.get_state("living_ac")["onoff"] is True


def test_mcp_high_risk_device_refused(mcp_server):
    payload, is_error = _call(mcp_server, "call_device_action",
                              {"device_id": "garage_door", "action": "open"})
    assert is_error
    assert "high-risk" in payload["error"]


def test_mcp_unknown_method(mcp_server):
    response = _rpc(mcp_server, "no/such/method")
    assert response["error"]["code"] == -32601


def test_mcp_stdio_smoke():
    """Boot the real server as a subprocess and speak JSON-RPC to it."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "omnibutler.cli", "mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, cwd=str(ROOT), env=env,
    )
    try:
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "smoke", "version": "0"}},
        }) + "\n")
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n")
        proc.stdin.flush()
        init = json.loads(proc.stdout.readline())
        listing = json.loads(proc.stdout.readline())
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=10)
    assert init["result"]["serverInfo"]["name"] == "tiybai-omnibutler"
    names = {t["name"] for t in listing["result"]["tools"]}
    assert "list_devices" in names and "confirm_action" not in names
    assert len(names) == 12

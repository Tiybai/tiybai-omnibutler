# Architecture

Tiybai OmniButler is a bridge with one job: turn the fragmented world of
smart devices into one canonical model that any AI agent can use through
MCP - and execute personal-life scenes deterministically and locally.

## Five layers

```
 AI agents            Muse - OpenClaw - Hermes - any MCP client
      |  MCP (JSON-RPC over stdio / HTTP)
 [5] northbound        omnibutler/mcp_server/   tools, guardrails
      |
 [4] scenes            omnibutler/scenes/       deterministic rules,
      |                                          confirmation queue
 [3] core              omnibutler/core/         capability model, registry,
      |                                          event bus, audit, manager
 [2] drivers           omnibutler/drivers/      one adapter per channel:
      |                                          mock - homeassistant - miio - tuya
 [1] device-data       device-data/             per-model facts and mappings
      |
 devices               AC - lights - purifier - curtains - scale - garage ...
```

Dependencies flow one way, downwards. Nothing in `core/` knows a vendor's
name; nothing in `drivers/` knows what an agent is.

### core
- `models.py` - `Capability`, `Property`, `Device`: the single vocabulary
  every driver translates into (onoff, target_temperature, pm25, position...).
- `registry.py` - index of known devices; query by room or capability.
- `events.py` - publish/subscribe bus. Events are the only way the outside
  world (state changes, schedules, geofences) reaches the scene engine.
- `audit.py` - append-only JSONL record of every control call.
- `confirmations.py` - the queue where high-risk actions wait for a human.
- `manager.py` - validation, routing, audit and event publication for every
  device operation.

### drivers
A driver adapts one access channel to the model: `discover`,
`list_devices`, `get_state`, `set_property`, `call_action`. The mock driver
ships a full virtual home so the whole stack runs with no hardware. The
Home Assistant driver reuses an existing HA installation over its REST API.
Vendor-local drivers (Xiaomi miIO, Tuya local) are implemented behind
clean-room protocol specs (see `docs/specs/` and `clean-room.md`) and are
awaiting real-hardware verification.

### device-data
Per-model fact files (capabilities, ranges, vendor mapping identifiers)
kept apart from code, so adding a device model is a data pull request, not
a code change. See `device-data/README.md`.

### scenes
Scenes are YAML rules: trigger + conditions (AND) + actions + risk level.
An AI agent may *write* a scene; a plain, deterministic engine *runs* it.
Same event in, same actions out. High-risk actions are diverted to the
confirmation queue instead of executing.

### northbound (MCP)
A dependency-free MCP server (JSON-RPC over stdio, spec 2024-11-05; an
optional token-authenticated HTTP transport shares the same handler)
exposing twelve tools: device listing/state, property writes, actions,
scene management, a read-only view of the confirmation queue, data-stream
reads, and terminal-session open/close. There
is deliberately no approve tool - approval happens only from a terminal
on the host (`tob confirm <id>`). High-risk devices are refused on the
direct tools - the confirmation queue is the only path.

## Five access modes

Devices do not all join the same way. The architecture recognises five:

| Mode | Shape | Examples |
|---|---|---|
| A. Direct local | bridge talks to the device on the LAN/BLE | local protocols, ESPHome-style devices, robot SDKs |
| B. Phone as gateway | a companion app on the phone owns BLE + health stores and relays | watches, bands, glasses, headphones |
| C. Read-only data pipeline | no control, only authorised data streams exposed as MCP resources | scales, health data, vehicle telemetry |
| D. Terminal app | the agent's front-end runs *on* the device | smart glasses, open watch faces, drones |
| E. Vendor cloud | vendor API as a fallback channel | cloud-only brands, remote access |

v0.1 implements mode A/E through the Home Assistant driver and demos mode A
with the mock driver. Modes B-D are roadmap items; the core model (entity /
data stream / terminal session) already distinguishes them.

## Runtime flow (an "arrive home" scene)

1. A phone geofence event enters the event bus.
2. The scene engine matches `arrive-home`, checks its conditions against the
   registry, and runs each action through the manager.
3. The manager validates values, routes to the owning driver, writes audit.
4. Any high-risk action would have been queued, not executed - see
   `examples/scenes/garage-arrival.yaml`.

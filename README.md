# Tiybai OmniButler

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](#roadmap)

**An open smart-device bridge that lets AI agents - Muse, OpenClaw,
Hermes, any MCP client - control the smart devices in your life, and run
personal scenes for you, deterministically and locally.**

中文简介：Tiybai OmniButler（万能管家）是一个开源智能设备桥。它把小米、涂鸦、美的等
各家互不相通的智能设备，统一成一个能力模型，再通过 MCP 开放给 AI Agent 使用；
AI 负责听懂你的意图、编排场景，真正执行的是本地的确定性规则引擎。高风险设备
（门锁、车库门、燃气）默认不交给 AI 直接控制，必须经人工确认。完整中文说明见
[README.zh-CN.md](README.zh-CN.md)。

> Not an official product of Xiaomi, Tuya, Midea, Home Assistant, Anthropic
> or any other vendor mentioned. All trademarks belong to their owners.

## Why

Smart devices in a typical home speak a dozen incompatible protocols and
live in a dozen vendor clouds. Meanwhile AI agents speak MCP. OmniButler
sits in the middle:

- **One capability model** - drivers translate MiOT-Spec services, Tuya data
  points, Matter clusters and Home Assistant domains into one vocabulary:
  `onoff`, `target_temperature`, `pm25`, `position`, ...
- **One agent interface** - a dependency-free MCP server (stdio, plus an
  optional token-authenticated HTTP transport) with seven tools. Any MCP
  client can use it. Note there is deliberately no "approve" tool:
  confirmations are human-only, from a terminal on the host.
- **Scenes that run without the AI** - YAML rules (trigger + conditions +
  actions) executed by a local deterministic engine. The AI authors and
  tunes scenes; it is never in the real-time control loop.
- **Safety guardrails in code** - high-risk actions are parked in a
  confirmation queue for a human. Every control call lands in a local,
  append-only audit log.

## Architecture

```
 agents (MCP)  ->  mcp_server  ->  scene engine  ->  core (model/registry/
                                                      events/audit/manager)
                                                      |
                                    drivers: mock | homeassistant | miio |
                                             tuya | midea | broadlink
                                                      |
                                    device-data (per-model facts)
```

See [docs/architecture.md](docs/architecture.md) for the five layers and the
five device access modes.

## Quick start (5 minutes, no hardware needed)

Requires Python 3.11+.

```bash
git clone https://github.com/Tiybai/tiybai-omnibutler.git
cd tiybai-omnibutler
pip install -e .

tob devices                 # a virtual home: 2 ACs, purifier, light, curtain, scale, garage door
tob devices --state         # ... with live state
tob set living_ac onoff true
tob set living_ac target_temperature 24
tob scenes                  # list + validate the bundled scene pack
tob simulate                # fire "arrive home": watch the scene chain execute
tob simulate --event leave  # fire "leave home": everything turns off
tob simulate --event pm25   # fire a PM2.5 spike: the purifier goes turbo
tob simulate --event garage # fire "arrive at garage gate": the door action is QUEUED for confirmation
```

Connect a real home instead of the mock one:

```bash
export HA_URL=http://192.168.1.10:8123
export HA_TOKEN=<your Home Assistant long-lived token>
tob --driver homeassistant devices
```

## Let an AI agent drive it (MCP)

```bash
tob mcp        # serves MCP on stdio
```

Point any MCP client at that command. Example client configuration:

```json
{
  "mcpServers": {
    "omnibutler": { "command": "tob", "args": ["mcp"] }
  }
}
```

Tools: `list_devices`, `get_device_state`, `set_device_property`,
`call_device_action`, `list_scenes`, `enable_scene`,
`get_pending_confirmations`. That is the whole list - there is **no**
tool to approve a queued action. Approving is a human-only step on the
host: `tob pending`, then `tob confirm <id>` (or `tob reject <id>`).

For agents that do not run on the host, `tob mcp --http` serves the same
tools over HTTP on `127.0.0.1:8765`, protected by a bearer token from
`$OMNIBUTLER_HTTP_TOKEN`. For remote access put it behind Cloudflare
Access or WireGuard; do not expose it raw.

Run it as an always-on butler with `tob run` (schedule triggers and
device-state polling), check a real setup with `tob doctor`, and get
plain-language help fetching a device key with `tob setup miio` /
`tob setup tuya`.

High-risk devices (the garage door in the demo home) refuse direct tool
calls by design. Try it: ask the agent to open the garage door, then run a
scene that requests it (`examples/scenes/garage-arrival.yaml`) and watch the
action land in `get_pending_confirmations` instead of executing.

## Scenes

Scenes are small YAML files - see `examples/scenes/`:

| Scene | Trigger | What happens |
|---|---|---|
| `arrive-home` | geofence enter | Living-room AC on at 26 C, purifier on auto |
| `leave-home-check` | geofence exit | Everything off, so nothing is left running |
| `sleep-mode` | 22:30 | Bedroom sleep temperature, curtain closes, purifier silent |
| `air-quality-guard` | PM2.5 state change | Purifier to turbo while PM2.5 > 75 |
| `garage-arrival` | geofence (garage gate) | Door action **queued for human confirmation** (high risk); light runs |

## Project status & roadmap

v0.3 (this release) - the safety story is now enforced, not just
documented: the MCP server has no way to approve its own queued actions
(approval is a host-terminal-only step, and the queue is persisted on
disk across processes). Also new: an always-on daemon (`tob run`), an
HTTP transport for remote agents (`tob mcp --http`, token required),
`tob doctor` health checks that actually verify credentials, a
local config file + plain-language key-fetching guides (`tob setup`),
and Midea (msmart-ng) and Broadlink (python-broadlink, honest
learn/send IR-RF) drivers. Xiaomi/Tuya/Midea/Broadlink drivers are
tested against fake devices; **not yet verified on real hardware**.

- [x] v0.2 - first real local drivers behind clean-room specs; device-data
      contributions open
- [x] v0.3 - human-only confirmations enforced; daemon; MCP over HTTP;
      doctor + setup guides; Midea and Broadlink drivers
- [ ] v0.4 - real-hardware verification round; phone-gateway mode
      (wearables / health read-only pipelines)
- [ ] v0.5 - Matter controller, Zigbee via external Zigbee2MQTT over MQTT
- [ ] Later - terminal mode for open smart glasses; vendor-cloud fallbacks

## Contributing a device

Device support grows one verified model at a time:

1. Add a data file in `device-data/` (facts only, with `source` and
   `provenance` - see `device-data/README.md`).
2. If a driver change is needed, follow [CONTRIBUTING.md](CONTRIBUTING.md):
   real-device verification is required, and anything informed by a
   reference implementation goes through the
   [clean-room process](docs/clean-room.md).
3. Original implementations only. We learn protocols and facts from the
   ecosystem; we do not port other projects' code.

## Security

Risk levels, key handling, allowlists, audit and remote-access rules are in
[docs/security.md](docs/security.md). Short version: keys stay on your
machine, nothing is exposed to the public internet (remote access only via
Cloudflare Access or WireGuard), and high-risk devices need a human.

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). The licence audit
for everything this project depends on or references is in
[docs/license-audit.md](docs/license-audit.md).

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
  optional token-authenticated HTTP transport) with twelve tools. Any MCP
  client can use it. Note there is deliberately no "approve" tool:
  confirmations are human-only, from a terminal on the host.
- **Scenes that run without the AI** - YAML rules (trigger + conditions +
  actions) executed by a local deterministic engine. The AI authors and
  tunes scenes; it is never in the real-time control loop.
- **Safety guardrails in code** - high-risk actions are parked in a
  confirmation queue for a human. Every control call lands in a local,
  append-only audit log. An optional webhook
  (`OMNIBUTLER_NOTIFY_WEBHOOK_URL`) pings your phone the moment
  something queues up, so a headless bridge never waits in silence.

## Architecture

```
 agents (MCP)  ->  mcp_server  ->  scene engine  ->  core (model/registry/
                                                      events/audit/manager)
                                                      |
                                    drivers: mock | homeassistant | miio |
                                             tuya | midea | broadlink |
                                             matter | zigbee2mqtt | terminals
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

tob devices                 # a virtual home: 2 ACs, purifier, light, curtain, scale, garage door, robot vacuum
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

**Approving without a terminal:** `tob approvals` serves a small local
web page (its own password, `$OMNIBUTLER_APPROVALS_TOKEN`, localhost by
default) listing what is waiting, with Approve / Reject buttons - or
run `tob run --notify` on a Mac and a native dialog pops up when
something queues. Both are human clicks outside the MCP surface; the AI
still cannot approve anything. On the Mac dialog, Reject is the default
button and silence never means yes.

Run it as an always-on butler with `tob run` (schedule triggers and
device-state polling), check a real setup with `tob doctor`, and get
plain-language help fetching a device key with `tob setup miio` /
`tob setup tuya`. A fetched key can be stored without ever appearing
on screen: `tob setup-secret miio.devices.0.token` prompts with
hidden input and writes the config file with 0600 permissions. New in v0.4: `tob onboard` scans every driver and
says in plain language what each found device still needs,
`tob fetch-keys xiaomi|tuya` fetches local keys from the vendor cloud
once (with your own account, only when no captcha/2FA blocks it), and
`tob gateway` accepts phone data streams and geofence events;
`tob streams` shows what the phone has sent, `tob discover` lists what
each driver can see on the network, and `tob state` prints one
device's full current state. Day to day, `tob audit` reads the audit
log back, and `tob backup` / `tob restore` pack config and state into
one file for moving the butler to another machine.

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

v0.10 (this release) - the audit round: four independent audits
(cross-platform, security and concurrency, data layer and
performance, docs and UI alignment) went over the whole codebase,
and every finding was fixed or consciously accepted. The
confirmation queue is now locked across processes, and a queued
action is claimed atomically before it runs - approving the same
item from the web page and the CLI can no longer execute it twice.
Saving secrets no longer crashes on Windows. The phone gateway
only accepts geofence and presence events; it can no longer forge
state changes to trigger scenes. Reading the audit log is now a
streaming pass (`tob audit --last 20` on a full 60 MB log: 1.8 s
and 362 MB of memory before, 0.13 s and ~45 MB now) and data
streams load lazily, so everyday commands stay fast no matter how
much history has piled up. Polling runs drivers in parallel, and
one dead device no longer stalls the rest of its driver. The
approvals page follows your browser language (Chinese or English)
and works properly on a phone screen. CI now also runs on Windows
and macOS runners, and `tob setup` finally lists every guide it
actually has - five topics were unreachable from the command line.
Real-hardware verification moves to v0.11 - it still needs owners
of real devices (issues #2 and #4).

v0.9 - the final sweep: scenes gain delayed actions
(`- delay: 300` - a light that turns itself off, a vacuum that starts
once you have left); the Home Assistant driver subscribes to HA's
event stream, so state changes reach scenes near-instantly instead of
on the 30-second poll; third-party drivers can ship as pip packages
(entry point group `omnibutler.drivers`), no fork needed; `tob audit`
and `tob backup` / `tob restore` cover the operator chores; the
daemon refuses to start twice against the same state directory; CI
adds a coverage floor (80%, currently 88%) and Python 3.13 to the
test matrix; device-data reaches 20 profiles and the demo home gains
a robot vacuum. Real-hardware verification moves to v0.11 - it still
needs owners of real devices (issues #2 and #4).

v0.8 - robot vacuums join the Xiaomi local driver
(start / stop / return to dock, battery level) through the standard
MIOT vacuum service, and the Xiaomi cloud fallback inherits the new
family automatically; a queued high-risk action can now push a
webhook notification (`OMNIBUTLER_NOTIFY_WEBHOOK_URL` or
`notify.webhook_url` in config - ntfy, Bark and similar services all
take the POST), so a bridge running headless no longer queues in
silence; scene schedules accept `days`, so rules like "weekdays at
07:30" finally work; and CI gains lint (ruff) and type-check (mypy)
gates, both green.

v0.7 - depth where it counts: the Xiaomi driver now
covers lights, fans and humidifiers (standard MIOT services) next to
air conditioners and purifiers - and the Xiaomi cloud fallback picks
the new families up automatically; the Tuya driver controls curtain
motors (open/close + position), matching its device-data profile;
scene state conditions accept `for_seconds`, so a rule can require
"PM2.5 above 75 for 10 minutes" instead of reacting to a blip; and
the audit log and data streams rotate by size (default 10 MiB x 5,
tunable via OMNIBUTLER_LOG_MAX_MB / OMNIBUTLER_LOG_KEEP), so a
long-running butler cannot fill the disk. `tob doctor` reports the
state directory's total size.

v0.6 - the second completeness pass: a Xiaomi cloud
fallback driver (`--driver xiaomi_cloud`) joins the Tuya one - when
a Xiaomi device cannot be reached locally (no token, not on this
network), it can be controlled through the vendor cloud using the
same login machinery as `tob fetch-keys` and the same MIOT property
mapping as the local driver. Like `tuya_cloud` it is deliberately
*not* part of `all`. device-data grows to 14 profiles (Zigbee door
and motion sensors, a generic Matter plug, a Tuya curtain motor).

v0.5 - the completeness pass: nothing half-wired is
left. The MCP surface now covers data streams and terminal sessions
(12 tools); the phone gateway can run inside the daemon
(`tob run --gateway`, one process, scenes fire exactly once);
`tob doctor` also checks the Matter controller and Zigbee2MQTT
broker for reachability and reports config-version health; config
files are versioned (missing version = v1, newer-than-supported is
a hard error, first rewrite keeps a one-time .bak); a Tuya cloud
fallback driver (`--driver tuya_cloud`) controls devices through
the vendor cloud when no local path exists - it is deliberately
*not* part of `all`, going cloud is always an explicit choice; and
`tob setup` now has plain-language guides for matter, zigbee2mqtt,
gateway and tuya_cloud. Everything is still tested against fake
devices only - **real-hardware verification remains the project's
biggest gap** (issues #2 and #4).

- [x] v0.2 - first real local drivers behind clean-room specs; device-data
      contributions open
- [x] v0.3 - human-only confirmations enforced; daemon; MCP over HTTP;
      doctor + setup guides; Midea and Broadlink drivers
- [x] v0.4 - Matter (controller client) + Zigbee2MQTT drivers; phone
      gateway + data streams; terminal sessions; onboard scan +
      cloud key fetch; Docker/service packaging
- [x] v0.5 - MCP streams/sessions tools; gateway inside the daemon;
      doctor for the new drivers; config versioning live; Tuya cloud
      fallback driver; setup guides for all drivers
- [x] v0.6 - Xiaomi cloud fallback driver (cloud fallbacks now cover
      both brands with a usable public cloud API); device-data at
      14 profiles
- [x] v0.7 - retention caps for logs and streams; Xiaomi lights /
      fans / humidifiers; Tuya curtain motors; sustained scene
      conditions (`for_seconds`)
- [x] v0.8 - Xiaomi robot vacuums; webhook notification when a
      high-risk action queues; scene schedules accept `days`;
      ruff + mypy gates in CI
- [x] v0.9 - delayed scene actions; HA event-stream subscription;
      third-party drivers via entry points; audit / backup commands;
      daemon single-instance lock; coverage gate in CI
- [x] v0.10 - the audit round: Windows/macOS in CI; confirmation
      queue locked and claimed atomically; gateway event whitelist;
      streaming audit reads and lazy stream loading; parallel
      polling; bilingual approvals page
- [ ] v0.11 - real-hardware verification round (needs owners of real
      devices - see issues #2 and #4)
- [ ] Later - PyPI release (needs the maintainer's PyPI account)

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

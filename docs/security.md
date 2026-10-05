# Security model

The bridge can touch real homes. Its security posture is conservative by
default; loosen it deliberately, per device, never globally.

## Risk levels

| Level | Examples | Policy |
|---|---|---|
| low | lights, AC, purifier, curtains, fans | Agents and scenes may control directly. |
| medium | heaters and other heating appliances, ovens, washing machines | Scenes may control with limits (bounded runtime, state read-back). First enable is a human decision. |
| high | door locks, garage doors, gas valves, alarm disarm, camera privacy mode | **Never exposed for direct agent control.** Scene/agent requests are parked in the confirmation queue; a human confirms each execution. Read-only state is allowed. |

Risk is assigned per device in the registry / device-data (`risk` field),
can be raised per scene action, and the *highest* applicable level wins.

## Keys and secrets

- Vendor tokens, device keys (e.g. per-device local keys) and HA tokens are
  read from the environment or the user's local secret store. They are
  **never** written into this repository, scene files, device-data, or the
  audit log. `.gitignore` excludes `.env`, `*token*`, `*secret*` and local
  config, and CI runs a secret scan.
- Vendor account sign-in (OAuth/QR) is performed by the user themselves.
  The bridge never asks for, stores, or transmits a vendor password.
- Per-device keys are obtained once, from the user's own account, for the
  user's own devices (see the driver notes). The bridge does not distribute
  keys and does not ship extracted vendor secrets.

## Exposure control

- The MCP surface is an allowlist: only devices present in the registry are
  visible, and high-risk devices refuse direct writes at the tool layer.
- Prefer separate read-only and control credentials where a channel
  supports them; agents should hold the narrowest token that works.
- Every control call is written to the append-only audit log (agent, device,
  action, parameters, result). The log lives on the user's machine; the
  agent cannot delete it through any exposed tool.

## Remote access

- **Never** expose the bridge, an MCP endpoint, or a Home Assistant port
  directly to the public internet (no router port forwarding).
- Remote access goes through a zero-trust tunnel only: **Cloudflare Access
  / Cloudflare Tunnel** or **WireGuard**. Authentication happens at the
  tunnel layer before any bridge traffic.
- Within the home LAN, bind services to the LAN interface only.

## Reporting

See `SECURITY.md` at the repository root for how to report a vulnerability.

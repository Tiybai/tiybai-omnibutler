# device-data format

One JSON file per device model. Files in this directory are **data, not
code**: they describe what a model can do, in the canonical capability
vocabulary of `omnibutler/core/models.py`.

## Fields

| Field | Meaning |
|---|---|
| `device_id` | Stable slug: `<brand>.<model-slug>` |
| `brand`, `model` | As printed on the product / shown in the vendor app |
| `category` | `climate`, `light`, `cover`, `sensor`, `vacuum`, ... |
| `protocol` | Access channel: `mock`, `miot`, `tuya-local`, `matter`, `zigbee`, `homeassistant`, ... |
| `connection` | `local`, `cloud`, `ble`, `virtual` |
| `capabilities` | Map of canonical property name -> `{type, writable, unit, min, max, options}` plus a `mapping` object with vendor-side identifiers (e.g. MiOT siid/piid, Tuya DP id) when known |
| `actions` | Named actions beyond property writes |
| `risk` | `low` / `medium` / `high` - high-risk models (doors, locks, gas) must stay `high` |
| `source` | **Required.** Where the facts came from: vendor spec document, your own device + packet capture, official product page. No source, no merge. |
| `provenance` | **Required.** Who contributed it, whether it was verified on a physical device (`verified_on_device`), and notes |

## Rules

1. **Facts only.** Model numbers, capability ranges, data-point ids and
   protocol constants are facts. Do not paste vendor marketing text or
   anyone's code into a data file.
2. **Cite the source** in `source` and record verification in `provenance`.
   Unverified entries are welcome but must say `verified_on_device: false`.
3. **Clean room applies to data too.** If the mapping was learned from a
   reference implementation, the entry must be produced through the spec
   workflow in `docs/clean-room.md`, not copied from a compilation whose
   licence is unclear. When in doubt, collect the mapping from your own
   device.
4. Keys, tokens and account identifiers never belong in data files.
5. **A profile can be data-only.** If no driver speaks a profile's
   protocol yet, the profile may still be merged as data reserved for a
   future driver - but its `provenance.note` must say so plainly
   (example: `yeelight-led-bulb-1s.json`, whose `yeelight-lan` protocol
   has no driver yet). A data-only profile describes facts; nothing in
   the bridge can control that device until a driver lands.

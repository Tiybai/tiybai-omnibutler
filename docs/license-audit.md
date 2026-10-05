# Licence audit

Own code: **Apache-2.0** (see `LICENSE`, `NOTICE`).

## Direct dependencies

| Dependency | Licence | Use |
|---|---|---|
| Python 3.11+ standard library | PSF | runtime |
| PyYAML >= 6.0 | MIT | scene file parsing (the only runtime dependency) |
| tinytuya >= 1.13 (optional, extra `tuya`) | MIT | Tuya local-protocol transport for `omnibutler/drivers/tuya.py`; lazily imported, only when the extra is installed |
| python-broadlink (optional, extra `broadlink`) | MIT | Broadlink RM-family IR/RF hub transport for `omnibutler/drivers/broadlink.py`; lazily imported, only when the extra is installed |
| msmart-ng (optional, extra `midea`) | MIT | Midea M-Smart local-protocol transport for `omnibutler/drivers/midea.py`; lazily imported, only when the extra is installed |
| websockets >= 12.0 (optional, extra `matter`) | BSD-3-Clause | WebSocket client transport to an external Matter controller service (matterjs-server / python-matter-server) for `omnibutler/drivers/matter.py`; lazily imported, only when the extra is installed |
| paho-mqtt >= 1.6 (optional, extra `zigbee`) | EPL-2.0 / Apache-2.0 dual-licensed — used under Apache-2.0 | MQTT client transport for `omnibutler/drivers/zigbee2mqtt.py`, which talks to an external Zigbee2MQTT process over MQTT; lazily imported, only when the extra is installed |
| pytest (dev only) | MIT | tests |

Rule: new direct dependencies must be MIT / Apache-2.0 / BSD / PSF / ISC.
Anything else needs a maintainer decision recorded in this file first.

## Not dependencies - external systems and references

These projects are **not** imported, vendored, or copied. They run as
separate processes under their own licences, or serve as specification
references under the clean-room rules in `clean-room.md`.

| Project | Licence | Relationship |
|---|---|---|
| Home Assistant | Apache-2.0 | External system; we call its REST API. HA integrations a user installs are the user's choice. |
| python-miio | GPL-3.0 | Reference only (protocol facts via spec process); the miIO driver is an original clean-room implementation shipped in v0.2, written from the spec in `docs/specs/miio-protocol.md`. |
| Zigbee2MQTT | GPL-3.0 | External process; if used, it is reached over MQTT, never linked. |
| Gadgetbridge | AGPL-3.0 | External Android app; phone-gateway mode talks to it via its public intents/APIs, never merged into this tree. |
| tuya-local device data | MIT | Data dependency candidate; entries carry their own source/provenance fields (see device-data/README.md). |
| matter.js | Apache-2.0 | Not used. The Matter driver (v0.4) is a controller *client* over WebSocket (see the websockets row above); running the controller itself (e.g. matterjs-server) is the user's deployment choice, not a dependency of this package. |
| MentraOS | Apache-2.0 | External glasses OS; terminal mode integrates via its APIs. |
| LibrePods | GPL-3.0 | Reference / external only. |
| openScale | GPL-3.0 | Reference / external only. |

Isolation principle: communication with GPL/AGPL components happens through
public interfaces (REST, MQTT, intents, subprocess boundaries), never by
copying their code into this repository.

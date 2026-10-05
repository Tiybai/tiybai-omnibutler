# miIO - local UDP protocol and MIoT property addressing

- Sources consulted: the miIO protocol facts as documented in public
  protocol write-ups and interoperability documentation (facts only: port,
  packet layout, key derivation, method names); the MIoT-Spec model
  documentation published by the Xiaomi IoT developer platform (service /
  property addressing model); our own discovery captures of Xiaomi devices
  on a home LAN. No source code of any existing client library is reproduced
  here, and none may be: this document states facts, formats and flows only.
- Author / date: Tiybai OmniButler contributors, 2026-10-05
- Status: implemented (driver v0.2; black-box verification against real
  hardware is tracked as follow-up work, see Verification)

## Facts

### Transport

- Devices listen on **UDP port 54321** at their LAN address.
- Discovery is a UDP broadcast to `255.255.255.255:54321`; devices also
  answer a unicast probe sent to their own address.
- All multi-byte integers in the packet header are **big-endian**.

### Packet layout

Every packet starts with a fixed **32-byte header**:

| Bytes | Field | Meaning |
|---|---|---|
| 0-1 | magic | Always `0x2131`. |
| 2-3 | length | Total packet length in bytes, header included. |
| 4-7 | reserved | Zero in requests we send. |
| 8-11 | device id | Unique numeric id of the device. In a discovery probe the sender writes `0xFFFFFFFF` (unknown). |
| 12-15 | stamp | Device clock: seconds counted from the device's boot / clock base. It is a liveness counter, not wall-clock time. |
| 16-31 | check field | Meaning depends on packet kind (see below). |

### Hello packet (discovery and handshake)

- A hello probe is exactly 32 bytes: magic `0x2131`, length 32, device id
  `0xFFFFFFFF`, stamp `0xFFFFFFFF`, and the remaining 16 bytes set to
  `0xFF`.
- A device answers with a 32-byte hello packet of its own. The interoperable
  outputs of the answer are the **device id** (bytes 8-11) and the current
  **stamp** (bytes 12-15). The trailing 16 bytes carry a token-derived check
  value on current firmware and are not needed to set up a session.
- One broadcast normally elicits one answer per reachable device, so a
  single probe enumerates the LAN segment.

### Encrypted data packets

- The payload of a data packet is a single JSON document, encrypted with
  **AES-128 in CBC mode** with **PKCS#7 padding** to a 16-byte block
  boundary, appended after the 32-byte header. The header length field is
  therefore `32 + len(ciphertext)`.
- Key material is derived from the per-device **token**: 16 bytes the owner
  obtains from their own Xiaomi account for their own device. Tokens are
  secrets and are never part of this specification.
  - encryption key = `MD5(token)` (16 bytes)
  - initialisation vector = `MD5(key || token)` (the MD5 digest of the key
    bytes followed by the token bytes)
- The header check field (bytes 16-31) of a data packet is
  `MD5(header bytes 0-15 || token || ciphertext)`. A receiver verifies this
  digest before decrypting; a mismatch means the packet is discarded.
- The stamp written into a request must track the device's clock: the stamp
  learned from the hello answer, advanced by the whole seconds elapsed
  since that answer was received. Devices reject requests with a stale
  stamp, so a session re-runs the hello handshake when requests start
  failing or after a long idle period.
- Each request carries a numeric **request id** that increases per request;
  the response echoes the same id.

### Payload: JSON-RPC-shaped messages

- Request document: an object with fields `id` (the request id), `method`
  (a string), and `params` (a list or object, method-dependent).
- Success response: an object with the same `id` and a `result` field.
- Error response: an object with the same `id` and an `error` object
  carrying at least a numeric `code` and a `message` string.
- `miIO.info` (no params) returns device facts: model string, firmware
  version, hardware version, network information. It is the reliable way to
  learn the exact model of a discovered device.

### MIoT property addressing

Newer devices expose their functions through the MIoT model instead of
per-class legacy methods:

- A device's functions are grouped into **services**, numbered by a service
  id (**siid**). Each service has **properties**, numbered by a property id
  (**piid**), and **actions**, numbered by an action id (**aiid**).
- A property is addressed by the triple (**did**, **siid**, **piid**), where
  `did` identifies the (sub-)device that owns the service; for the main
  device this is the device's own identifier as reported by `miIO.info`.
- `get_properties` takes a list of address objects `{did, siid, piid}` and
  returns a list of result objects `{did, siid, piid, value, code}` in the
  same order; `code` 0 means success, any other value means that property
  could not be read (unsupported, offline sub-device, ...).
- `set_properties` takes a list of `{did, siid, piid, value}` and returns a
  per-item result list with the same `code` convention.
- `action` takes `{did, siid, aiid, in: [...]}` for operations that are not
  plain property writes.
- Property value formats are declared per model in that model's MIoT-Spec
  instance document: `bool`, unsigned / signed integers, `float`, `string`,
  and enumerations encoded as integers whose meaning is the position in a
  declared **value-list** (for example an air conditioner's mode property
  is an integer indexing a list such as auto / cool / dry / heat / fan).
- Which (siid, piid) a given function lives at, the value ranges, and the
  value-lists are **per-model device facts** published in the model's
  MIoT-Spec document. They are data, not protocol constants: two models of
  the same device class may differ, and a driver must treat its mapping
  tables as overridable per-model data.

### Device facts for the families the v0.2 driver targets

Stated as the typical MIoT-Spec instance layout of these families; the
driver lets every entry be overridden per device, because individual models
vary.

**Air conditioner** (Xiaomi / Mijia MIoT air conditioners)

- Air-conditioner service, siid 2:
  - piid 1: power, bool
  - piid 2: mode, integer value-list (auto / cool / dry / heat / fan)
  - piid 3: target temperature, number in degrees Celsius (typical range
    16-32)
- Fan service (separate siid in the instance, commonly 3): fan level as an
  integer value-list from automatic through ascending speeds.

**Air purifier** (Xiaomi / Mijia MIoT air purifiers)

- Air-purifier service, siid 2:
  - piid 1: power, bool
  - piid 2: mode, integer value-list (auto / silent / turbo / manual
    family of modes)
- Environment service (commonly siid 3): PM2.5 density reading, number in
  ug/m3.
- Filter service (commonly siid 4): piid 1 filter life level remaining,
  number in percent; piid 2 filter left time in days.

## Flows

### Discovery

1. Sender broadcasts the 32-byte hello probe to `255.255.255.255:54321`.
2. Each device answers with its hello packet (device id + stamp).
3. Sender records (source IP, device id, stamp) per answer and may follow
   up with `miIO.info` per device to learn the model string.

### Session (per device)

1. Send a unicast hello probe to the device; record device id and stamp.
2. For each request: advance the stamp by the seconds elapsed since the
   hello answer, build the JSON payload, encrypt it, fill the header
   (length, device id, stamp, check digest), send to port 54321.
3. Read the response packet, verify its check digest, decrypt, parse the
   JSON document, match the response `id` to the request.

### Reading and writing a MIoT property

1. Build the address object(s) `{did, siid, piid}` from the model's mapping
   data.
2. Send `get_properties` (read) or `set_properties` (write, with `value`).
3. Interpret each result item by its `code`; a non-zero code is a per-item
   failure, not a transport failure.

## Open questions

- Exact value-lists and (siid, piid) assignments vary by model and firmware;
  they must be confirmed per model against that model's MIoT-Spec document
  or a capture of the owner's own device before the model is listed as
  verified.
- Some device classes (notably newer gateways and BLE-mesh sub-devices) are
  reached through a parent device with their own `did`; sub-device control
  is out of scope for the v0.2 driver.

## Verification

- Packet layout, key derivation and the hello flow were cross-checked
  against protocol facts documented in public interoperability write-ups.
- The v0.2 implementation is verified by an in-process fake device that
  speaks this specification (hello answers, encrypted requests and
  responses) end to end in the test suite.
- Black-box verification against physical Xiaomi hardware remains open and
  will be recorded in `docs/provenance/` when the first owner's devices are
  tested.

# miIO / MIoT driver (v0.2) - provenance

- Date / contributor: 2026-10-05, Tiybai OmniButler contributors
- What was built or added: `omnibutler/drivers/miio.py` - local miIO UDP
  transport (discovery, hello handshake, AES-128-CBC packet codec) and a
  MIoT property-mapping layer for air conditioners and air purifiers;
  `tests/test_miio_driver.py` with an in-process fake device.
- Spec used: docs/specs/miio-protocol.md
- Sources consulted (facts only):
  - Public protocol documentation of the miIO packet format, port, key
    derivation and method names (facts and formats, no code), consulted by
    the spec author on 2026-10-05.
  - MIoT-Spec model documentation published on the Xiaomi IoT developer
    platform: the (did, siid, piid) addressing model and the typical
    service layouts of the air-conditioner and air-purifier families.
  - Project research notes on the Xiaomi channel (2026-10-05) for device
    family background.
- Reference implementations read by the SPEC AUTHOR (names only):
  python-miio (GPL-3.0) - named as a protocol-facts reference in
  docs/license-audit.md; no code, structure, comments or identifiers were
  carried into the spec or the implementation.
- Implementer worked from: spec + own captures (yes - implementation was
  written from docs/specs/miio-protocol.md and the Driver interface
  contract only; the fake-device tests are the implementer's own oracle)
- Verification: in-process fake miIO device (hello + encrypted get/set
  round-trips) in the automated test suite, 2026-10-05. Physical-device
  verification is still outstanding: no Xiaomi hardware has been driven
  yet, per-model value-lists and (siid, piid) assignments must be confirmed
  on owners' devices before any model is claimed as verified.
- Licence notes: the implementation imports only the Python standard
  library plus the optional `cryptography` package (Apache-2.0 / BSD,
  used at runtime by the operator's install; imported lazily, never
  vendored). Device tokens are user secrets supplied at runtime via
  configuration or environment; none appear in the code, tests or this
  record. Mapping tables in the driver are per-model device facts and are
  overridable per device.

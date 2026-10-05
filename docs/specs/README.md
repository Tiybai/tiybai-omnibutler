# Protocol specifications

Clean-room specifications live here. A spec is the *only* artefact an
implementer may work from when a driver is informed by a reference
implementation (see `../clean-room.md`).

## Spec template

File name: `<protocol>-<topic>.md`, e.g. `miot-discovery.md`.

```markdown
# <Protocol> - <Topic>

- Sources consulted: (documents, captures, reference names - no code)
- Author / date:
- Status: draft | reviewed | implemented

## Facts
Ports, packet layouts, field meanings, magic numbers, command words,
state machines, timing - stated as facts, with units and byte order.

## Flows
Step-by-step message sequences for discovery / pairing / control / status.

## Open questions
Anything not yet established by a document or a capture.

## Verification
How the facts were checked (own device model, capture date).
```

Rules: no source code, no pseudocode that mirrors a reference
implementation's structure, no copied comments or identifier names.

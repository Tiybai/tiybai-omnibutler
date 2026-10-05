# Clean-room rules

**Status: adopted 2026-10-05.** This is an engineering convention, not a
legal opinion. A lawyer must review the licence posture before any
commercial distribution.

## Why

OmniButler must be original work under Apache-2.0. Some prior art we learn
from is GPL/AGPL; re-implementing it carelessly would either copy protected
expression or import copyleft obligations. The clean-room process is how we
learn from the ecosystem without doing either.

## What may be learned

Facts and ideas are free to learn: protocol shapes, packet formats, ports,
magic numbers, command words, handshake flows, state machines, device facts
(model numbers, capability ranges, data-point assignments).

## Red lines

- No line-by-line rewriting, transliteration, or translation of someone
  else's source code into our tree - in any language. A translation is a
  derivative work; copyleft does not evaporate because the code was retyped.
- No copying of code structure, comments, identifiers or error text from a
  reference implementation into a "fresh" file.
- No wholesale import of data compilations (device mapping databases) whose
  licence is unclear. Single facts are fine; a curated compilation is a
  work. Use it under its licence as a dependency, or re-collect the facts.
- No vendor keys, fixed cryptographic secrets extracted from vendor apps,
  or credentials of any kind in this repository.

## The standard process

1. **Spec author** (may read reference implementations, vendor documents
   and packet captures) writes a protocol specification into `docs/specs/`:
   facts, fields, flows - **no code, no pseudocode mirroring the source**.
2. **Implementer** (a different person, or a separate, clearly delimited
   session) writes the driver from the spec plus their own captures of
   their own devices. The implementer does not open the reference source.
3. **Black-box verification** against real hardware and the spec.
4. **Provenance**: the spec, the capture notes, the sources consulted, and
   the commit history are kept in `docs/provenance/`. Together they are the
   evidence chain that the implementation is independent.

## What does not need clean room

MIT / Apache-2.0 / BSD libraries are used directly, under their licences,
with notices preserved (see `license-audit.md`). Rewriting permissive code
to look original is wasteful and loses provenance. OmniButler's originality
lives in its architecture, capability model, scene engine and MCP layer;
drivers stand on permissively licensed shoulders where licences allow.

## Pull request gate

Any PR that implements functionality informed by a reference implementation
must include: the spec in `docs/specs/`, a provenance entry in
`docs/provenance/`, and a note in the PR description naming the sources.
PRs missing these are returned without review.

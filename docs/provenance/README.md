# Provenance records

Every clean-room implementation and every device-data contribution leaves a
record here, so the independence of the implementation can be shown, not
just asserted (see `../clean-room.md`).

## Record template

File name: `YYYY-MM-DD-<topic>.md`.

```markdown
# <Topic> - provenance

- Date / contributor:
- What was built or added:
- Spec used: docs/specs/<file>.md
- Sources consulted (facts only):
  - <document / product page / own-device capture, with date>
- Reference implementations read by the SPEC AUTHOR (names only):
- Implementer worked from: spec + own captures (yes/no)
- Verification: device model, firmware, what was tested
- Licence notes: anything a reviewer should double-check
```

Commit history is part of the chain: keep spec commits separate from, and
earlier than, implementation commits for the same topic.

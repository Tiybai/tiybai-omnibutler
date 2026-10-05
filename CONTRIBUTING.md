# Contributing to Tiybai OmniButler

Thanks for helping the bridge grow. Device support is this project's long
tail, and it only works if contributions are verifiable and original.

## Ground rules

1. **Original code only.** Learn protocols and facts from anywhere; write
   the implementation yourself. Anything informed by a reference
   implementation must follow the clean-room process in
   [docs/clean-room.md](docs/clean-room.md): a spec in `docs/specs/`, a
   provenance record in `docs/provenance/`, and sources named in the PR.
   PRs that skip this are returned without review.
2. **Licences stay clean.** New dependencies must be MIT / Apache-2.0 / BSD /
   PSF / ISC and recorded in [docs/license-audit.md](docs/license-audit.md).
   GPL/AGPL components are external processes reached through public
   interfaces - never vendored, never copied.
3. **No secrets, ever.** No tokens, device keys, account data, or real
   personal information in code, tests, fixtures, or data files.

## Developer Certificate of Origin (DCO)

We use the DCO instead of a CLA. Add a `Signed-off-by` line to every commit
(`git commit -s`), certifying that you wrote the contribution or otherwise
have the right to submit it under the project's licence, per
<https://developercertificate.org/>.

## Development setup

```bash
pip install -e ".[dev]"
python -m pytest -q
tob devices
tob simulate
```

## Pull request types

### Bug fix / core change
- Describe the behaviour change and how you tested it.
- Add or update tests. CI must be green.

### New device model (device-data)
PR description **must** include:

- Brand, exact model, firmware version
- How the capability mapping was obtained (vendor document / own device /
  own packet capture), matching the `source` field in the data file
- `verified_on_device: true/false` in `provenance` - if false, say what
  remains unverified
- A redacted capture excerpt or test output showing the mapping works

### New or changed driver
Everything above, plus:

- The access mode (see docs/architecture.md) and the channel's risk notes
- If a reference implementation informed the work: link the spec in
  `docs/specs/` and the provenance record in `docs/provenance/`
- Tests with the mock driver or a recorded fixture - no live vendor
  accounts in CI

### New scene for the example pack
- One file per scene in `examples/scenes/`, validated by `tob scenes`
- State the risk level plainly; any scene touching a high-risk device must
  rely on the confirmation queue, not bypass it

## Style

- Python 3.11+, PEP 8 in spirit, type hints on public APIs.
- Comments explain *why*, not *what*; keep them sparse and in English.
- Tests: pytest, no network, no real devices.

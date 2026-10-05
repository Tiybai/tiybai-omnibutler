# Publishing to PyPI - maintainer notes

How a release gets from this repo onto PyPI, and what is still missing.
End users do not need this file; see `docs/install.md`.

## Metadata readiness (checked 2026-10-05, v0.3.1)

`pyproject.toml` already carries everything PyPI requires:

- `name` - `tiybai-omnibutler` (the import package stays `omnibutler`,
  the CLI stays `tob`)
- `version`, `description`, `readme` (README.md), `requires-python`
- `license` - Apache-2.0, with the full `LICENSE` file in the repo root
- `authors`, `keywords`, `classifiers`, `[project.urls]`
  (Homepage / Repository / Issues / Changelog)
- Optional drivers are extras (`miio`, `tuya`, `broadlink`, `midea`), so
  the base install is light: PyYAML only.

Verified locally: `pip wheel --no-deps .` builds cleanly and the wheel
metadata contains the description, license, classifiers and URLs.

## Known packaging gaps (fix before or soon after the first upload)

1. **Example scenes are not in the wheel.** The wheel ships only the
   `omnibutler` package; the bundled scenes live in `examples/scenes/`
   at repo root and `runtime.py` looks for them relative to the repo.
   A PyPI install therefore starts with no demo scenes (the code
   tolerates the missing directory). Fix by moving the scenes inside
   the package as package data - that is a code change, tracked
   separately from this document.
2. **Config format is unversioned.** See `docs/config-versioning.md`
   for the v1 plan; not a PyPI blocker, but easier to land before a
   large installed base exists.
3. **License metadata style.** `license = { text = "Apache-2.0" }` is
   valid and renders fine; switching to the PEP 639 SPDX form
   (`license = "Apache-2.0"` + `license-files`) is a cleanup for when
   the minimum hatchling version is raised.

## Release steps

```bash
# 1. Bump `version` in pyproject.toml AND `__version__` in
#    omnibutler/__init__.py (they must match), tag the release in git.
# 2. Run the full test suite.
python -m pytest

# 3. Build sdist + wheel into dist/.
pip install build          # one-time
python -m build

# 4. Check the artifacts render and verify.
twine check dist/*

# 5. Upload to TestPyPI first, install from there once, then PyPI.
twine upload --repository testpypi dist/*
twine upload dist/*
```

Recommended instead of a long-lived API token: **Trusted Publishing**
(GitHub Actions OIDC) - configure the project on PyPI once, then the
release workflow needs no stored secret at all.

## What only the project owner can do

These steps need the user's own accounts and cannot be done by a
contributor or an automated agent:

- Create the **PyPI account** (and TestPyPI account), enable 2FA.
- Confirm the name `tiybai-omnibutler` is still available at first
  upload (names are first-come, first-served; the first upload *is*
  the registration).
- Create the API token (or set up Trusted Publishing) and store it in
  the Secure Vault / CI secrets - never in the repo, never in chat.
- Approve the first upload.

Everything else - metadata, artifacts, this checklist - is ready.

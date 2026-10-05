#!/usr/bin/env python3
"""Dependency license gate.

Policy (docs/license-audit.md): this project's own code is Apache-2.0.
Runtime dependencies must be permissive (MIT / Apache / BSD / ISC /
PSF) or file-level weak copyleft that we only *link* as an installed
library (EPL-2.0, MPL-2.0 - e.g. paho-mqtt is dual EPL-2.0/EDL).
Anything in the GPL family, or a license this script does not know,
fails the gate and needs a human decision recorded in
docs/license-audit.md before the allowlist grows.

Usage:
    pip install pip-licenses
    python scripts/check_licenses.py

Reads the *installed* environment, so run it after installing the
package with the extras you want to gate (CI installs all of them).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ALLOWED_MARKERS = (
    "mit", "apache", "bsd", "isc", "python software foundation",
    "psf",
    "eclipse public license", "eclipse distribution license",
    "mozilla public license", "public domain", "unlicense",
    "zlib", "boost software license",
)
BLOCKED_MARKERS = ("gpl", "agpl", "lgpl", "copyleft", "commons clause")


def _pip_licenses_command(bindir: str) -> list[str]:
    """The command that runs pip-licenses for the given interpreter dir.

    The console script sits next to the interpreter; on Windows it
    carries an .exe suffix, so try both names before the -m fallback.
    """
    for name in ("pip-licenses", "pip-licenses.exe"):
        candidate = os.path.join(bindir, name)
        if os.path.exists(candidate):
            return [candidate]
    return [sys.executable, "-m", "piplicenses"]


def main() -> int:
    cmd = _pip_licenses_command(os.path.dirname(sys.executable))
    try:
        out = subprocess.run(
            [*cmd, "--format=json", "--with-system"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("pip-licenses is not available - install it first "
              "(pip install pip-licenses)", file=sys.stderr)
        return 2
    packages = json.loads(out)
    failures = []
    for pkg in packages:
        name = pkg.get("Name", "?")
        license_text = (pkg.get("License") or "").strip()
        lowered = license_text.lower()
        if name.lower() == "tiybai-omnibutler":
            continue  # the project itself
        if any(marker in lowered for marker in BLOCKED_MARKERS):
            failures.append((name, license_text, "blocked family"))
        elif not any(marker in lowered for marker in ALLOWED_MARKERS):
            failures.append((name, license_text, "unknown - needs review"))
    if failures:
        print("License gate FAILED:")
        for name, license_text, why in failures:
            print(f"  {name}: {license_text!r} ({why})")
        print("Decide in docs/license-audit.md before changing this gate.")
        return 1
    print(f"License gate passed for {len(packages)} installed packages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

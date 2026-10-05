"""Platform-support pins (v0.10 line A).

Windows cannot be exercised from the Linux test host, so the Win32
liveness probe is tested against a fake ``ctypes`` module injected
into ``sys.modules``; the POSIX probe is re-pinned here so the
platform split can never silently regress. The CI matrix and the
platform doc get cheap structural assertions for the same reason.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml

from omnibutler import instance_lock
from omnibutler.instance_lock import pid_alive

REPO_ROOT = Path(__file__).resolve().parent.parent


# -- fake ctypes / kernel32 ----------------------------------------------

class _FakeULong:
    def __init__(self, value: int = 0) -> None:
        self.value = value


class _FakeKernel32:
    """Just the three calls _pid_alive_windows uses."""

    def __init__(self, *, handle: int = 4321, exit_code: int = 259,
                 exit_ok: bool = True) -> None:
        self._handle = handle
        self._exit_code = exit_code
        self._exit_ok = exit_ok
        self.closed: list[int] = []

    def OpenProcess(self, _access: int, _inherit: bool, _pid: int) -> int:
        return self._handle

    def GetExitCodeProcess(self, _handle: int, ref: _FakeULong) -> bool:
        if self._exit_ok:
            ref.value = self._exit_code
        return self._exit_ok

    def CloseHandle(self, handle: int) -> bool:
        self.closed.append(handle)
        return True


def _fake_ctypes(kernel32: _FakeKernel32 | None = None, *,
                 last_error: int = 0, windll_error: bool = False):
    module = types.ModuleType("ctypes")
    if windll_error:
        def _boom(*_args, **_kwargs):
            raise OSError("no kernel32 here")
        module.WinDLL = _boom  # type: ignore[attr-defined]
    else:
        module.WinDLL = lambda *_a, **_k: kernel32  # type: ignore[attr-defined]
    module.c_ulong = _FakeULong  # type: ignore[attr-defined]
    module.byref = lambda obj: obj  # type: ignore[attr-defined]
    module.get_last_error = lambda: last_error  # type: ignore[attr-defined]
    return module


def _as_windows(monkeypatch: pytest.MonkeyPatch, ctypes_module) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    if ctypes_module is None:
        monkeypatch.setitem(sys.modules, "ctypes", None)
    else:
        monkeypatch.setitem(sys.modules, "ctypes", ctypes_module)


# -- pid_alive: the Windows branch ----------------------------------------

def test_win32_probe_still_active_is_alive(monkeypatch):
    _as_windows(monkeypatch, _fake_ctypes(_FakeKernel32(exit_code=259)))
    assert pid_alive(1234) is True


def test_win32_probe_exited_is_dead(monkeypatch):
    kernel32 = _FakeKernel32(exit_code=0)
    _as_windows(monkeypatch, _fake_ctypes(kernel32))
    assert pid_alive(1234) is False
    assert kernel32.closed == [4321]  # handle closed on the way out


def test_win32_open_fails_not_found_is_dead(monkeypatch):
    kernel32 = _FakeKernel32(handle=0)
    _as_windows(monkeypatch, _fake_ctypes(kernel32, last_error=87))
    assert pid_alive(1234) is False


def test_win32_open_fails_access_denied_is_alive(monkeypatch):
    kernel32 = _FakeKernel32(handle=0)
    _as_windows(monkeypatch, _fake_ctypes(kernel32, last_error=5))
    assert pid_alive(1234) is True


def test_win32_exit_code_query_fails_is_alive(monkeypatch):
    _as_windows(monkeypatch, _fake_ctypes(_FakeKernel32(exit_ok=False)))
    assert pid_alive(1234) is True


def test_win32_no_ctypes_is_alive(monkeypatch):
    _as_windows(monkeypatch, None)  # import ctypes raises ImportError
    assert pid_alive(1234) is True


def test_win32_windll_unavailable_is_alive(monkeypatch):
    _as_windows(monkeypatch, _fake_ctypes(windll_error=True))
    assert pid_alive(1234) is True


def test_win32_never_uses_posix_kill(monkeypatch):
    # A pid that is dead on this host must still read alive when the
    # Win32 probe says so - proving the platform split, not os.kill.
    _as_windows(monkeypatch, _fake_ctypes(_FakeKernel32(exit_code=259)))
    assert pid_alive(999_999) is True


# -- pid_alive: POSIX branch unchanged -------------------------------------

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="forcing the POSIX kill(0) probe on a Windows host is exactly "
    "the hazard the Win32 branch exists to avoid (os.kill there can "
    "broadcast a console Ctrl+C and interrupt the test runner)",
)
def test_posix_probe_unchanged(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert pid_alive(os.getpid()) is True
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert pid_alive(proc.pid) is False
    assert pid_alive(0) is False
    assert pid_alive(-7) is False


def test_lock_module_still_exports_probe():
    assert instance_lock.pid_alive is pid_alive


# -- check_licenses: the .exe candidate ------------------------------------

def _license_script(body: str) -> str:
    return f"#!/bin/sh\nprintf '%s' '{body}'\n"


def _run_license_main(monkeypatch, tmp_path: Path, names: list[str]):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python").write_text("", encoding="utf-8")
    for name in names:
        script = bindir / name
        script.write_text(_license_script("[]"), encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(sys, "executable", str(bindir / "python"))
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import check_licenses

        return check_licenses.main()
    finally:
        sys.path.remove(str(REPO_ROOT / "scripts"))


def test_license_gate_finds_exe_candidate(monkeypatch, tmp_path, capsys):
    # Only the Windows-named script exists: the gate must pick it up
    # (the -m fallback would fail against the fake interpreter path).
    rc = _run_license_main(monkeypatch, tmp_path, ["pip-licenses.exe"])
    assert rc == 0
    assert "License gate passed for 0 installed packages." in \
        capsys.readouterr().out


def test_license_gate_still_finds_plain_candidate(monkeypatch, tmp_path):
    rc = _run_license_main(monkeypatch, tmp_path, ["pip-licenses"])
    assert rc == 0


# -- CI matrix shape --------------------------------------------------------

def test_ci_matrix_covers_three_os_without_exploding():
    ci = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml")
        .read_text(encoding="utf-8"))
    test_job = ci["jobs"]["test"]
    assert test_job["runs-on"] == "${{ matrix.os }}"
    matrix = test_job["strategy"]["matrix"]
    combos = {
        (os_name, py)
        for os_name in matrix["os"]
        for py in matrix["python-version"]
    }
    for entry in matrix.get("include", []):
        combos.add((entry["os"], entry["python-version"]))
    assert combos == {
        ("ubuntu-latest", "3.11"),
        ("ubuntu-latest", "3.12"),
        ("ubuntu-latest", "3.13"),
        ("windows-latest", "3.12"),
        ("macos-latest", "3.12"),
    }
    # The quality gate stays a single Ubuntu job.
    assert ci["jobs"]["quality"]["runs-on"] == "ubuntu-latest"


# -- platform doc anchors ----------------------------------------------------

def test_platform_doc_covers_all_five_platforms():
    doc = (REPO_ROOT / "docs" / "platform-support.md") \
        .read_text(encoding="utf-8")
    for anchor in ("Windows", "macOS", "Linux", "Task Scheduler",
                   "Termux", "termux-wake-lock", "iOS",
                   "8767", "8766", "geofence", "Bearer"):
        assert anchor in doc, f"platform doc lost its {anchor!r} section"
    assert "There is no\nofficial mobile app" in doc

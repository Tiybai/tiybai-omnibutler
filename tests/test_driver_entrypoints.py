"""Third-party drivers via entry points (omnibutler.runtime).

A fake entry point stands in for an installed package: monkeypatching
``runtime.entry_points`` is the seam, exactly like a real distribution
metadata scan, minus the pip install.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnibutler import runtime
from omnibutler.drivers.base import Driver
from omnibutler.drivers.miio import MiioDriver

EXAMPLES_SCENES = Path(__file__).resolve().parent.parent / "examples" / "scenes"


class FakeBrandDriver(Driver):
    name = "fakebrand"

    def discover(self):
        return []

    def list_devices(self):
        return []

    def get_state(self, device_id):
        return {}

    def set_property(self, device_id, property_name, value):
        return {}

    def call_action(self, device_id, action, params):
        return {}


class NotADriver:
    """Has some of the shape, but not all of it."""

    name = "almost"

    def discover(self):
        return []

    def list_devices(self):
        return []


class ExplodingDriver(FakeBrandDriver):
    name = "exploding"

    def __init__(self):
        raise RuntimeError("no config, no driver")


class FakeEntryPoint:
    def __init__(self, name, target, loader):
        self.name = name
        self.value = target
        self._loader = loader

    def load(self):
        return self._loader()


def _raise(exc):
    raise exc


def install_eps(monkeypatch, eps):
    monkeypatch.setattr(runtime, "entry_points", lambda group=None: list(eps))


def good_ep(name="fakebrand", cls=FakeBrandDriver):
    return FakeEntryPoint(name, f"fakepkg.driver:{cls.__name__}", lambda: cls)


# -- discovery / merge ---------------------------------------------------


def test_driver_names_merge_builtins_first(monkeypatch):
    install_eps(monkeypatch, [good_ep()])
    names = runtime.driver_names()
    assert names[: len(runtime.DRIVER_NAMES)] == runtime.DRIVER_NAMES
    assert names[-1] == "fakebrand"
    assert "fakebrand" not in runtime.DRIVER_NAMES


def test_statuses_report_good_and_broken(monkeypatch):
    install_eps(monkeypatch, [
        good_ep(),
        FakeEntryPoint("brokenload", "gone.mod:Cls",
                       lambda: _raise(ImportError("no module named gone"))),
        FakeEntryPoint("almost", "fakepkg.driver:NotADriver", lambda: NotADriver),
        FakeEntryPoint("anumber", "fakepkg.const:ANSWER", lambda: 42),
    ])
    statuses = {d.name: d for d in runtime.third_party_drivers()}
    assert statuses["fakebrand"].ok
    assert statuses["fakebrand"].target == "fakepkg.driver:FakeBrandDriver"
    assert not statuses["brokenload"].ok
    assert "failed to load" in statuses["brokenload"].error
    assert "no module named gone" in statuses["brokenload"].error
    assert not statuses["almost"].ok
    assert "get_state" in statuses["almost"].error
    assert not statuses["anumber"].ok
    # Only the good one is offered as a factory.
    assert set(runtime.third_party_factories()) == {"fakebrand"}


def test_factory_function_and_instance_targets(monkeypatch):
    instance = FakeBrandDriver()
    install_eps(monkeypatch, [
        FakeEntryPoint("madebyfunc", "fakepkg:make", lambda: (lambda: FakeBrandDriver())),
        FakeEntryPoint("readyinstance", "fakepkg:DRIVER", lambda: instance),
    ])
    factories = runtime.third_party_factories()
    assert isinstance(factories["madebyfunc"](), FakeBrandDriver)
    assert factories["readyinstance"]() is instance


def test_duplicate_names_first_wins(monkeypatch):
    install_eps(monkeypatch, [good_ep(), good_ep()])
    statuses = runtime.third_party_drivers()
    assert statuses[0].ok
    assert not statuses[1].ok
    assert "duplicate" in statuses[1].error


def test_builtin_name_conflict_builtin_wins(monkeypatch, capsys):
    install_eps(monkeypatch, [good_ep(name="miio", cls=FakeBrandDriver)])
    statuses = {d.name: d for d in runtime.third_party_drivers()}
    assert not statuses["miio"].ok
    assert "built-in" in statuses["miio"].error
    assert "built-in driver" in capsys.readouterr().err
    assert "miio" not in runtime.third_party_factories()
    built = runtime._build_drivers("miio")
    assert isinstance(built["miio"], MiioDriver)


# -- runtime building ------------------------------------------------------


def test_runtime_builds_third_party_by_name(monkeypatch):
    install_eps(monkeypatch, [good_ep()])
    built = runtime._build_drivers("fakebrand")
    assert isinstance(built["fakebrand"], FakeBrandDriver)
    rt = runtime.build_runtime(driver="fakebrand", load_default_scenes=False)
    assert rt.driver_name == "fakebrand"
    assert isinstance(rt.manager.drivers["fakebrand"], FakeBrandDriver)


def test_all_includes_third_party_and_skips_exploding(monkeypatch, capsys):
    install_eps(monkeypatch, [
        good_ep(),
        FakeEntryPoint("exploding", "fakepkg.driver:ExplodingDriver",
                       lambda: ExplodingDriver),
    ])
    built = runtime._build_drivers("all")
    assert isinstance(built["fakebrand"], FakeBrandDriver)
    assert "exploding" not in built
    assert "skipped in 'all'" in capsys.readouterr().err


def test_selected_broken_driver_fails_loudly(monkeypatch):
    install_eps(monkeypatch, [
        FakeEntryPoint("brokenload", "gone.mod:Cls",
                       lambda: _raise(ImportError("no module named gone"))),
    ])
    with pytest.raises(ValueError, match="unavailable"):
        runtime._build_drivers("brokenload")


def test_unknown_driver_error_lists_merged_names(monkeypatch):
    install_eps(monkeypatch, [good_ep()])
    with pytest.raises(ValueError) as excinfo:
        runtime._build_drivers("nosuchdriver")
    message = str(excinfo.value)
    assert "mock" in message
    assert "fakebrand" in message


# -- CLI + doctor surfaces ---------------------------------------------------


def test_cli_choices_include_third_party(monkeypatch):
    install_eps(monkeypatch, [good_ep()])
    from omnibutler.cli import build_parser

    parser = build_parser()
    action = next(a for a in parser._actions if a.dest == "driver")
    assert "fakebrand" in action.choices
    assert "all" in action.choices


def test_doctor_lists_third_party_status(monkeypatch):
    install_eps(monkeypatch, [
        good_ep(),
        FakeEntryPoint("brokenload", "gone.mod:Cls",
                       lambda: _raise(ImportError("no module named gone"))),
    ])
    from omnibutler.doctor import check_all, format_report

    results = check_all({}, environ={}, scenes_dir=EXAMPLES_SCENES)
    result = next(r for r in results if r.name == "third-party drivers")
    assert "fakebrand (loaded)" in result.detail
    assert "brokenload (NOT loaded:" in result.detail
    assert result.status == "warn"
    assert "third-party drivers" in format_report(results)


def test_doctor_without_third_party(monkeypatch):
    install_eps(monkeypatch, [])
    from omnibutler.doctor import check_all

    results = check_all({}, environ={}, scenes_dir=EXAMPLES_SCENES)
    result = next(r for r in results if r.name == "third-party drivers")
    assert result.status == "ok"
    assert "none installed" in result.detail

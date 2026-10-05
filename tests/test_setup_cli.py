"""CLI-level coverage for ``tob setup`` (guide-function tests live in
tests/test_setup_guides.py / test_setup_guides_new.py).

The setup subcommand used to hard-code five argparse choices while
setup_guide grew to eight topics, so guides the README advertises
(matter, zigbee2mqtt, gateway, the two cloud fallbacks) were rejected
by argparse before the guide code ever ran. These tests walk every
canonical topic through the real CLI entry point and pin the parser
choices to the guide registry so the two cannot drift apart again.
"""

import argparse

import pytest

from omnibutler import cli
from omnibutler.setup_guide import guide_text, guide_topics


def _run(capsys, argv):
    rc = cli.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


@pytest.mark.parametrize("topic", guide_topics())
def test_setup_cli_prints_every_guide(capsys, topic):
    rc, out, _err = _run(capsys, ["setup", topic])
    assert rc == 0
    assert guide_text(topic) in out


def test_setup_choices_come_from_the_guide_registry():
    parser = cli.build_parser()
    subparsers = next(
        action for action in parser._actions
        if isinstance(action, argparse._SubParsersAction))
    setup_parser = subparsers.choices["setup"]
    brand = next(a for a in setup_parser._actions if a.dest == "brand")
    assert list(brand.choices) == guide_topics()
    # The topics the CLI once refused are exactly the ones that were
    # missing from the old hard-coded list.
    assert {"matter", "zigbee2mqtt", "gateway",
            "tuya_cloud", "xiaomi_cloud"} <= set(brand.choices)


def test_setup_cli_rejects_unknown_topic():
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["setup", "no-such-topic"])
    assert excinfo.value.code == 2

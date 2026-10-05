"""Setup guides for the v0.4 additions: matter, zigbee2mqtt, gateway,
tuya_cloud.

The guides are user-facing instructions for fetching/configuring
credentials on the user's own accounts and machines. The key names
they cite must be the ones the code actually reads:

- matter: ``MATTER_SERVER_URL`` (drivers/matter.py)
- zigbee2mqtt: ``Z2M_MQTT_URL`` (drivers/zigbee2mqtt.py)
- gateway: ``OMNIBUTLER_GATEWAY_TOKEN`` (gateway.py TOKEN_ENV_VAR)
- tuya_cloud: ``TUYA_ACCESS_ID`` / ``TUYA_ACCESS_SECRET`` /
  ``TUYA_UID`` (read by ``tob fetch-keys tuya`` in cli.py) for the
  one-time key fetch, and ``TUYA_CLOUD_ACCESS_ID`` /
  ``TUYA_CLOUD_ACCESS_SECRET`` (drivers/tuya_cloud.py) for the
  cloud fallback control driver
"""

from __future__ import annotations

import re

import pytest

from omnibutler.setup_guide import guide_text


def test_matter_guide():
    text = guide_text("matter")
    assert text.strip()
    assert "MATTER_SERVER_URL" in text
    assert 'pip install "tiybai-omnibutler[matter]"' in text
    # Honest about the architecture: a controller does the pairing,
    # the bridge only forwards the code.
    assert "commission" in text


def test_zigbee2mqtt_guide_and_aliases():
    text = guide_text("zigbee2mqtt")
    assert text.strip()
    assert "Z2M_MQTT_URL" in text
    assert 'pip install "tiybai-omnibutler[zigbee]"' in text
    assert guide_text("zigbee") == text
    assert guide_text("z2m") == text
    assert guide_text("Zigbee2MQTT") == text  # case-insensitive


def test_gateway_guide():
    text = guide_text("gateway")
    assert text.strip()
    assert "OMNIBUTLER_GATEWAY_TOKEN" in text
    assert "tob gateway" in text
    assert "/ingest" in text and "/event" in text


def test_tuya_cloud_guide_and_alias():
    text = guide_text("tuya_cloud")
    assert text.strip()
    assert "TUYA_ACCESS_ID" in text
    assert "TUYA_ACCESS_SECRET" in text
    assert "TUYA_UID" in text
    assert "fetch-keys tuya" in text
    # ... and the cloud fallback control driver, with its own names.
    assert "TUYA_CLOUD_ACCESS_ID" in text
    assert "TUYA_CLOUD_ACCESS_SECRET" in text
    assert "--driver tuya_cloud" in text
    assert "env:" in text  # secrets referenced, never written in clear
    assert guide_text("tuya-cloud") == text


def test_old_brands_unchanged():
    assert "token" in guide_text("miio")
    assert guide_text("xiaomi") == guide_text("miio")
    assert "local_key" in guide_text("tuya")
    assert guide_text("homeassistant") == guide_text("ha")
    # tuya_cloud is a different guide from the local tuya one.
    assert guide_text("tuya_cloud") != guide_text("tuya")


def test_unknown_brand_still_errors():
    with pytest.raises(ValueError):
        guide_text("gree")
    with pytest.raises(ValueError):
        guide_text("")


# A real key would appear as a long unbroken run of hex/base64-ish
# characters. Guide texts must only ever carry placeholders.
_LONG_SECRET_RUN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_HEX_RUN = re.compile(r"\b[0-9a-fA-F]{24,}\b")


@pytest.mark.parametrize("brand", [
    "miio", "tuya", "tuya_cloud", "ha", "matter", "zigbee2mqtt",
    "gateway",
])
def test_guides_contain_no_real_key_looking_strings(brand):
    text = guide_text(brand)
    # Drop URLs and env-var names (long by design, not secrets) before
    # scanning for key-shaped runs.
    scrubbed = re.sub(r"ws://\S+", "", text)
    scrubbed = re.sub(r"[A-Z][A-Z0-9_]{7,}", "", scrubbed)
    assert not _LONG_SECRET_RUN.search(scrubbed), brand
    assert not _HEX_RUN.search(scrubbed), brand

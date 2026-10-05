"""Onboarding scan: turn driver discovery into a filled-in config draft.

Each driver can *find* devices (``discover()``), but finding is not
controlling: a Xiaomi device answering the miIO broadcast still refuses
every command until the operator supplies its per-device token, and a
Tuya or Midea device likewise needs its local credentials fetched once
from the operator's own vendor account. Until now that gap meant hand-
writing ``~/.omnibutler/config.json`` entries from scratch.

This module closes the loop in three steps, without touching drivers:

1. :func:`scan` runs every driver's ``discover()`` and annotates each
   sighting with what it still needs before it can be controlled
   (``needs``). One driver failing - not configured, optional library
   missing, network unreachable - is recorded as a plain-language scan
   note for that driver, never as a crash of the whole scan.
2. :func:`config_draft` renders the sightings that need onboarding as
   a config dict in exactly the shape of ``config.example.json``:
   secret fields are ``"env:VARNAME"`` references (never invented
   values) and every entry carries a ``"_note"`` saying, in plain
   language, which fields the operator still has to fill in and where
   each key comes from.
3. :func:`format_report` renders the whole result as a plain-language
   summary: what was found, what can be controlled right away, what is
   missing which key, and what to do next.

How ``needs`` is decided (from the drivers' own code, not guessed):

- A *bare sighting* is a device with no capability model at all - no
  properties and no actions. That is the shape the miIO driver gives a
  device that answered its broadcast but is not in its configuration
  (configured devices come back with family properties and
  turn_on/turn_off actions). Bare sightings from key-based drivers are
  the ones missing a key: miio -> ``"token"``, tuya -> ``"local_key"``,
  midea -> ``"token+key"``.
- Devices that come back with a capability model from those drivers
  are, by construction, already configured with their key (the Tuya
  and Midea drivers refuse to build a device without one), so they
  need nothing: ``needs is None``.
- Broadlink hubs pair without any key, Home Assistant entities come
  from an already-authenticated server, Matter devices are
  controllable once commissioned, Zigbee devices arrive already
  paired through Zigbee2MQTT, and terminal (mock) devices are local
  demos, so those drivers' sightings need nothing either.
- A driver this module does not know is reported as ``"unknown"`` -
  it will not invent a key requirement for a channel it cannot speak
  for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from omnibutler.core.models import Device
from omnibutler.drivers.base import Driver

#: needs values that mean "one or more keys must be fetched first".
NEEDS_TOKEN = "token"
NEEDS_LOCAL_KEY = "local_key"
NEEDS_TOKEN_AND_KEY = "token+key"
NEEDS_UNKNOWN = "unknown"

#: Key a bare (not yet configured) sighting still needs, per driver.
_KEY_NEEDS: dict[str, str] = {
    "miio": NEEDS_TOKEN,
    "tuya": NEEDS_LOCAL_KEY,
    "midea": NEEDS_TOKEN_AND_KEY,
}

#: Drivers whose sightings never need a per-device key fetched by hand.
_OPEN_DRIVERS = frozenset({"broadlink", "homeassistant", "ha", "matter",
                           "mock", "terminal_mock", "zigbee2mqtt"})

#: Plain-language explanation of each key, reused by draft notes/report.
_KEY_EXPLAINED: dict[str, str] = {
    NEEDS_TOKEN: "小米的 token（每台设备一把）",
    NEEDS_LOCAL_KEY: "涂鸦的 local_key（每台设备一把）",
    NEEDS_TOKEN_AND_KEY: "美的的 token 和 key（两把，成对使用）",
}

#: Where the operator fetches each key, in one plain sentence.
_KEY_HOW_TO: dict[str, str] = {
    NEEDS_TOKEN: "跑 `tob setup miio`，照着从你自己的小米账号里拿一次",
    NEEDS_LOCAL_KEY: "跑 `tob setup tuya`，在你自己的涂鸦 IoT 平台项目里查",
    NEEDS_TOKEN_AND_KEY: "从你自己的美的（美居）账号里拿",
}


@dataclass
class FoundDevice:
    """One device sighting from a scan, with its onboarding gap."""

    device: Device
    driver: str
    needs: str | None  # None = controllable as-is

    @property
    def id(self) -> str:
        return self.device.id

    @property
    def name(self) -> str:
        return self.device.name

    @property
    def room(self) -> str:
        return self.device.room

    @property
    def brand(self) -> str:
        return self.device.brand

    @property
    def model(self) -> str:
        return self.device.model


class ScanResult(list):
    """A ``list[FoundDevice]`` that also carries the scan's notes.

    Notes are plain-language strings, one per driver that could not be
    scanned this time (not configured, library missing, unreachable),
    so a caller that only keeps the list still keeps the whole story.
    """

    def __init__(self, found: Iterable[FoundDevice] = (), notes: Iterable[str] = ()):
        super().__init__(found)
        self.notes: list[str] = list(notes)


def _is_bare_sighting(device: Device) -> bool:
    """True when a sighting carries no capability model at all.

    That is how the miIO driver reports a device that answered its
    broadcast but is not configured; configured devices always come
    back with properties or actions.
    """
    return not device.properties and not device.actions


def _classify(driver_name: str, device: Device) -> str | None:
    """What *device*, found via *driver_name*, still needs to be usable."""
    name = (driver_name or device.driver or "").strip().lower()
    if name in _KEY_NEEDS:
        return _KEY_NEEDS[name] if _is_bare_sighting(device) else None
    if name in _OPEN_DRIVERS:
        return None
    return NEEDS_UNKNOWN


def scan(drivers: Mapping[str, Driver]) -> ScanResult:
    """Run every driver's ``discover()`` and annotate what was found.

    A driver that raises (not configured, optional dependency missing,
    network unreachable, ...) does not fail the scan: the failure is
    recorded in ``ScanResult.notes`` in plain language and the other
    drivers' results still come back.
    """
    found: list[FoundDevice] = []
    notes: list[str] = []
    for key, driver in drivers.items():
        label = (key or getattr(driver, "name", "") or "driver").strip()
        try:
            devices = driver.discover()
        except Exception as exc:  # one bad channel must not sink the scan
            detail = str(exc).strip() or type(exc).__name__
            if len(detail) > 200:
                detail = detail[:197] + "..."
            notes.append(
                f"{label}：这一路这次没扫成（{detail}）。"
                "它下面的设备这次没有数进来，不代表你家就没有。"
            )
            continue
        for device in devices or []:
            found.append(
                FoundDevice(
                    device=device,
                    driver=label,
                    needs=_classify(label, device),
                )
            )
    return ScanResult(found, notes)


# ---------------------------------------------------------------------------
# Config draft
# ---------------------------------------------------------------------------

_DRAFT_ABOUT = (
    "Draft generated by OmniButler's onboarding scan - not a finished "
    "config. Fill in every field called out in each entry's \"_note\", "
    "put the real key values in the environment variables the \"env:\" "
    "references point at (or replace a reference with the literal "
    "value), then save as ~/.omnibutler/config.json (or $OMNIBUTLER_CONFIG) "
    "and run `tob doctor` to verify each device for real. Secret fields "
    "are references on purpose: the real keys never have to live in "
    "this file. See config.example.json for the same shape, filled in."
)


def _env_slug(driver: str, device_id: str) -> str:
    """A stable env-var fragment for one device: 'miio-12345' -> '12345'."""
    text = device_id.strip()
    prefix = f"{driver}-"
    if text.lower().startswith(prefix):
        text = text[len(prefix):]
    slug = re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_").upper()
    return slug or "DEVICE"


def _colon_mac(device_id: str) -> str:
    """'broadlink-aabbccddeeff' -> 'aa:bb:cc:dd:ee:ff' ('' if not that shape)."""
    match = re.fullmatch(r"broadlink-([0-9a-fA-F]{12})", device_id.strip())
    if not match:
        return ""
    hex_text = match.group(1).lower()
    return ":".join(hex_text[i:i + 2] for i in range(0, 12, 2))


def _miio_entry(item: FoundDevice) -> dict[str, Any]:
    var = f"MIIO_{_env_slug('miio', item.id)}_TOKEN"
    return {
        "id": item.id,
        "host": "",
        "name": item.name,
        "room": item.room,
        "model": item.model,
        "token": f"env:{var}",
        "_note": (
            "还差两样：① host 填这台设备在局域网里的 IP（路由器后台的"
            "设备列表或米家 App 里能看到）；② token 是这台设备自己的"
            f"钥匙，{_KEY_HOW_TO[NEEDS_TOKEN]}，拿到后把真值放进环境变量 "
            f"{var}（或者直接把 token 这一行的 env: 引用换成 token 本身）。"
        ),
    }


def _tuya_entry(item: FoundDevice) -> dict[str, Any]:
    var = f"TUYA_{_env_slug('tuya', item.id)}_LOCAL_KEY"
    return {
        "id": item.id,
        "device_id": "",
        "ip": "",
        "name": item.name,
        "room": item.room,
        "local_key": f"env:{var}",
        "_note": (
            "还差三样：① device_id 和 ② ip：device_id 在你自己的涂鸦 "
            "IoT 平台项目里查（见 `tob setup tuya`），ip 是这台设备在"
            "局域网里的地址；③ local_key 是这台设备自己的钥匙，同样在 "
            f"IoT 平台查，拿到后把真值放进环境变量 {var}（或者直接把 "
            "local_key 这一行的 env: 引用换成钥匙本身）。"
        ),
    }


def _midea_entry(item: FoundDevice) -> dict[str, Any]:
    slug = _env_slug("midea", item.id)
    token_var, key_var = f"MIDEA_{slug}_TOKEN", f"MIDEA_{slug}_KEY"
    return {
        "id": item.id,
        "ip": "",
        "device_id": "",
        "name": item.name,
        "room": item.room,
        "token": f"env:{token_var}",
        "key": f"env:{key_var}",
        "_note": (
            "还差三样：① ip 填这台设备在局域网里的地址；② token 和 "
            f"③ key 是美的 V3 的一对凭据，{_KEY_HOW_TO[NEEDS_TOKEN_AND_KEY]}，"
            f"拿到后分别放进环境变量 {token_var} 和 {key_var}（或者直接"
            "把这两行的 env: 引用换成真值）。device_id 知道就填、不知道"
            "可以先空着。"
        ),
    }


def _broadlink_entry(item: FoundDevice) -> dict[str, Any]:
    return {
        "id": item.id,
        "host": "",
        "mac": _colon_mac(item.id),
        "name": item.name,
        "room": item.room,
        "model": item.model,
        "_note": (
            "Broadlink 不用钥匙，发现了就能控。这一条是为了让它被长期"
            "纳管：host 填这台集线器在局域网里的 IP，mac 对一下机身"
            "标签是不是这个地址，对上就可以直接存进配置。"
        ),
    }


_ENTRY_BUILDERS = {
    NEEDS_TOKEN: _miio_entry,
    NEEDS_LOCAL_KEY: _tuya_entry,
    NEEDS_TOKEN_AND_KEY: _midea_entry,
}

_SECTION_OF_DRIVER = {
    "miio": "miio",
    "tuya": "tuya",
    "midea": "midea",
    "broadlink": "broadlink",
}


def config_draft(found: Iterable[FoundDevice]) -> dict[str, Any]:
    """Render sightings that need onboarding as a config-file draft.

    The shape mirrors ``config.example.json``: one section per driver,
    each with a ``devices`` list. Included are every device that still
    needs a key (its secret fields are ``env:`` placeholders plus a
    ``_note`` saying where the key comes from) and every Broadlink hub
    (keyless, but a config entry is what keeps it managed across
    restarts). Devices that are already controllable through config-
    backed drivers, Home Assistant entities and unknown-driver
    sightings are left out - the report explains them instead.
    """
    sections: dict[str, list[dict[str, Any]]] = {}
    for item in found:
        section = _SECTION_OF_DRIVER.get(item.driver.strip().lower())
        if section is None:
            continue
        if item.needs in _ENTRY_BUILDERS:
            entry = _ENTRY_BUILDERS[item.needs](item)
        elif item.driver.strip().lower() == "broadlink" and item.needs is None:
            entry = _broadlink_entry(item)
        else:
            continue
        sections.setdefault(section, []).append(entry)
    draft: dict[str, Any] = {"_about": _DRAFT_ABOUT}
    for section in ("miio", "tuya", "midea", "broadlink"):
        if sections.get(section):
            draft[section] = {"devices": sections[section]}
    return draft


# ---------------------------------------------------------------------------
# Plain-language report
# ---------------------------------------------------------------------------

def _describe(item: FoundDevice) -> str:
    bits = [item.driver]
    if item.room and item.room != "unknown":
        bits.append(item.room)
    if item.model:
        bits.append(item.model)
    return f"{item.name}（{'，'.join(bits)}，id={item.id}）"


def format_report(
    found: Iterable[FoundDevice],
    notes: Iterable[str] | None = None,
) -> str:
    """Render a scan result as a plain-language summary.

    When *notes* is omitted, the notes carried by a :class:`ScanResult`
    are used, so ``format_report(scan(drivers))`` tells the whole story.
    """
    items = list(found)
    if notes is None:
        notes = getattr(found, "notes", [])
    note_list = list(notes or [])

    ready = [i for i in items if i.needs is None]
    needy = [i for i in items if i.needs in _KEY_EXPLAINED]
    unknown = [i for i in items if i.needs == NEEDS_UNKNOWN]

    lines = ["OmniButler 扫描结果", "=" * 19, ""]
    if not items:
        lines.append("这次一台设备都没发现。")
        lines.append(
            "先确认：设备都通着电、连上了家里的 Wi-Fi，这台电脑和设备"
            "在同一个局域网里。"
        )
    else:
        lines.append(f"这次一共发现 {len(items)} 台设备：")
        lines.append("")
        if ready:
            lines.append(f"可以直接控制的（{len(ready)} 台）：")
            for item in ready:
                lines.append(f"  - {_describe(item)}")
            lines.append("")
        if needy:
            lines.append(f"发现了、但还差钥匙的（{len(needy)} 台）：")
            for item in needy:
                lines.append(f"  - {_describe(item)}")
                lines.append(
                    f"    差的是{_KEY_EXPLAINED[item.needs]}。"
                    f"拿钥匙：{_KEY_HOW_TO[item.needs]}，存进配置就能控。"
                )
            lines.append("")
        if unknown:
            lines.append(f"暂时判断不了的（{len(unknown)} 台）：")
            for item in unknown:
                lines.append(f"  - {_describe(item)}")
            lines.append(
                "    这几台来自还不认识的驱动，这次先不下结论，也没有"
                "给它们编钥匙要求。"
            )
            lines.append("")

    if note_list:
        lines.append("有几路这次没扫成：")
        for note in note_list:
            lines.append(f"  - {note}")
        lines.append("")

    lines.append("下一步：")
    if needy or any(
        i.driver.strip().lower() == "broadlink" and i.needs is None for i in items
    ):
        lines.append(
            "  1. 用扫描生成的配置草稿（config_draft）存成 "
            "~/.omnibutler/config.json；"
        )
        lines.append(
            "  2. 照草稿里每条 _note 写的一步步补齐地址和钥匙"
            "（钥匙只从你自己的账号里拿，不要发给任何人）；"
        )
        lines.append("  3. 跑 `tob doctor`，它会真连一次验证每台设备。")
    elif items:
        lines.append("  发现的设备都能直接控制，不用补任何钥匙。")
    else:
        lines.append("  先把上面没扫成的原因处理掉，再重新扫一次。")
    return "\n".join(lines)

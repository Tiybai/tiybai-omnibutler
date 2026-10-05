"""Health checks ("doctor") for an OmniButler installation.

``check_all`` answers, in plain language, the questions an operator
actually asks after setup: is Home Assistant reachable and is its token
still good? Are the Xiaomi devices answering - and if not, is it the
network or the token? Is the Tuya extra installed and does every device
have its local_key? Can the bridge write its state directory? Do all
scene files still parse?

Everything reported here is safe to show: results describe secrets only
as set / not set (see :mod:`omnibutler.config`) and error details never
contain token or key values.

The source of settings can be:

* ``None`` - the local config file (see :mod:`omnibutler.config`),
  layered over the drivers' environment variables, exactly like the
  drivers themselves resolve their configuration;
* a config ``dict`` as returned by :func:`omnibutler.config.load_config`;
* a built ``Runtime`` (duck-typed: anything with ``.manager``) - its
  drivers and audit path are inspected, and device settings fall back
  to the same config/env resolution.
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from omnibutler import config as config_module
from omnibutler.core.audit import default_audit_path
from omnibutler.core.errors import DriverNotConfiguredError, OmniButlerError
from omnibutler.drivers.homeassistant import (
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
    HomeAssistantDriver,
    HomeAssistantError,
    HomeAssistantTimeoutError,
)
from omnibutler.drivers.miio import MIIO_PORT, MiioDeviceConfig, _Session
from omnibutler.drivers.tuya import TuyaDriver

OK, WARN, FAIL = "ok", "warn", "fail"
_STATUSES = (OK, WARN, FAIL)

#: Per-request timeout for doctor probes: long enough for a sleepy LAN
#: device, short enough that a dead one does not stall the whole report.
DEFAULT_TIMEOUT = 2.0


@dataclass
class CheckResult:
    """One line of the health report."""

    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(
                f"CheckResult status must be one of {_STATUSES}, got {self.status!r}"
            )


# ---------------------------------------------------------------------------
# Settings resolution
# ---------------------------------------------------------------------------


def _miio_entries(
    config: Mapping[str, Any], environ: Mapping[str, str]
) -> list[dict[str, Any]]:
    entries = config_module.device_entries(config, "miio", "token", environ=environ)
    if entries:
        return entries
    # No config-file devices: mirror the driver's own environment config.
    from omnibutler.drivers.miio import _configs_from_env

    try:
        env_configs = _configs_from_env()
    except DriverNotConfiguredError:
        return []
    return [
        {
            "id": c.id,
            "host": c.host,
            "token": c.token,
            "token_description": "environment (set)",
            "model": c.model,
        }
        for c in env_configs
    ]


def _tuya_entries(
    config: Mapping[str, Any], environ: Mapping[str, str]
) -> tuple[list[dict[str, Any]], str | None]:
    """(entries, config_error) - error text is value-free by construction."""
    entries = config_module.device_entries(
        config, "tuya", "local_key", environ=environ
    )
    if entries:
        return entries, None
    try:
        raw_entries = TuyaDriver._devices_from_env()
    except DriverNotConfiguredError as exc:
        return [], str(exc)
    resolved = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            resolved.append({"_invalid_entry": raw})
            continue
        entry = dict(raw)
        key = entry.get("local_key")
        entry["local_key"] = key if isinstance(key, str) and key else None
        entry["local_key_description"] = (
            "environment (set)" if entry["local_key"] else "not set"
        )
        resolved.append(entry)
    return resolved, None


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_home_assistant(settings: Mapping[str, Any], timeout: float) -> CheckResult:
    name = "home-assistant"
    url = settings.get("url")
    token = settings.get("token")
    if not url and not token:
        return CheckResult(
            name, WARN,
            "not configured - set ha.url and ha.token in the config file "
            "(or HA_URL / HA_TOKEN) if you want to control devices through "
            "Home Assistant",
        )
    if url and not token:
        return CheckResult(
            name, FAIL,
            f"HA URL is set ({url}) but no token resolves "
            f"({settings.get('token_description')}); HA refuses every call "
            "without one - add a long-lived access token",
        )
    if token and not url:
        return CheckResult(
            name, FAIL,
            "an HA token is configured but no HA URL - set ha.url (or "
            "HA_URL) to your Home Assistant address",
        )
    driver = HomeAssistantDriver(url, token, timeout=timeout)
    try:
        result = driver._request("GET", "/api/")
    except HomeAssistantAuthError:
        return CheckResult(
            name, FAIL,
            f"HA at {url} answered 401/403 - the token was rejected "
            "(invalid or expired). Create a fresh long-lived access token "
            "in your HA profile and store the new one",
        )
    except HomeAssistantTimeoutError:
        return CheckResult(
            name, FAIL,
            f"no answer from {url} within {timeout:.0f}s (timed out) - is "
            "Home Assistant running, and is this machine on the same network?",
        )
    except HomeAssistantConnectionError as exc:
        return CheckResult(
            name, FAIL,
            f"cannot reach Home Assistant at {url} ({exc}) - check the "
            "address and that HA is running",
        )
    except HomeAssistantError as exc:
        return CheckResult(name, FAIL, f"Home Assistant returned an error: {exc}")
    message = result.get("message") if isinstance(result, dict) else None
    suffix = f" ({message})" if message else ""
    return CheckResult(name, OK, f"reachable at {url}{suffix}; token accepted")


def _check_miio_crypto() -> CheckResult:
    if importlib.util.find_spec("cryptography") is not None:
        return CheckResult("miio-crypto", OK, "the 'cryptography' package is installed")
    return CheckResult(
        "miio-crypto", FAIL,
        "the 'cryptography' package is NOT installed, so miIO packets "
        "cannot be encrypted - install it with: pip install cryptography",
    )


def _check_miio_device(entry: Mapping[str, Any], timeout: float, crypto_ok: bool) -> CheckResult:
    label = str(entry.get("id") or entry.get("name") or "(unnamed)")
    name = f"miio:{label}"
    if "_invalid_entry" in entry:
        return CheckResult(name, FAIL, "this device entry is not a JSON object")
    host = str(entry.get("host") or "").strip()
    if not host:
        return CheckResult(name, FAIL, "no host (device IP) configured")
    token = entry.get("token")
    if not token:
        return CheckResult(
            name, FAIL,
            f"no token available ({entry.get('token_description', 'not set')}) - "
            "add this device's own miIO token; see the setup guide",
        )
    device_config = MiioDeviceConfig(
        id=label, host=host, token=str(token), model=str(entry.get("model", ""))
    )
    try:
        token_bytes = device_config.token_bytes()
    except DriverNotConfiguredError:
        return CheckResult(
            name, FAIL,
            "the configured token is not 16 bytes written as 32 hex "
            "characters - re-copy it carefully from your token source",
        )
    if not crypto_ok:
        return CheckResult(
            name, WARN,
            f"configured for {host}, but not verified: the 'cryptography' "
            "package is missing (see the miio-crypto check)",
        )
    session = _Session(host, token_bytes, MIIO_PORT, timeout)
    try:
        session.handshake()
    except OmniButlerError as exc:
        return CheckResult(
            name, FAIL,
            f"network problem: {exc} - the token was not even reached, "
            "so check power, Wi-Fi and the IP address first",
        )
    try:
        info = session.request("miIO.info", [])
    except OmniButlerError as exc:
        hint = (
            " The reply failed its checksum, which is the classic sign of "
            "a wrong token." if "checksum" in str(exc) else ""
        )
        return CheckResult(
            name, FAIL,
            f"the device at {host} answers hello probes but the encrypted "
            "session failed - the token is probably wrong for this device "
            "(tokens change when a device is re-paired in Mi Home)." + hint,
        )
    model = info.get("model") if isinstance(info, dict) else None
    firmware = info.get("fw_ver") if isinstance(info, dict) else None
    detail = f"miIO.info answered from {host} - token works"
    if model:
        detail += f"; model {model}"
    if firmware:
        detail += f"; firmware {firmware}"
    return CheckResult(name, OK, detail)


def _check_miio(entries: list[dict[str, Any]], timeout: float) -> list[CheckResult]:
    crypto = _check_miio_crypto()
    results = [crypto]
    if not entries:
        results.append(CheckResult(
            "miio", WARN,
            "no miIO devices configured - add them under \"miio.devices\" "
            "in the config file (or MIIO_DEVICES / MIIO_HOST + MIIO_TOKEN)",
        ))
        return results
    crypto_ok = crypto.status == OK
    results.extend(_check_miio_device(e, timeout, crypto_ok) for e in entries)
    return results


def _check_tuya(
    entries: list[dict[str, Any]], config_error: str | None
) -> CheckResult:
    name = "tuya"
    if config_error:
        return CheckResult(name, FAIL, f"Tuya environment config is unusable: {config_error}")
    installed = importlib.util.find_spec("tinytuya") is not None
    if not entries:
        if installed:
            return CheckResult(
                name, WARN,
                "tinytuya is installed but no Tuya devices are configured - "
                "add them under \"tuya.devices\" in the config file (or "
                "TUYA_DEVICES_JSON)",
            )
        return CheckResult(
            name, WARN,
            "not set up: tinytuya is not installed and no Tuya devices are "
            "configured (Tuya is an optional extra: "
            "pip install \"tiybai-omnibutler[tuya]\")",
        )
    problems: list[str] = []
    for index, entry in enumerate(entries, start=1):
        if "_invalid_entry" in entry:
            problems.append(f"entry #{index} is not a JSON object")
            continue
        label = str(entry.get("device_id") or entry.get("id") or f"#{index}")
        missing = [k for k in ("device_id", "ip", "local_key") if not entry.get(k)]
        if missing:
            note = ""
            if "local_key" in missing:
                note = f" ({entry.get('local_key_description', 'not set')})"
            problems.append(f"device {label}: missing {', '.join(missing)}{note}")
    if problems:
        return CheckResult(name, FAIL, "; ".join(problems))
    if not installed:
        return CheckResult(
            name, WARN,
            f"{len(entries)} Tuya device(s) fully configured (local_key "
            "present on each), but tinytuya is NOT installed, so they "
            "cannot be controlled yet: pip install \"tiybai-omnibutler[tuya]\"",
        )
    return CheckResult(
        name, OK,
        f"tinytuya installed; {len(entries)} Tuya device(s) configured, "
        "each with a local_key",
    )


def _check_state_dir(runtime: Any | None) -> CheckResult:
    name = "state-dir"
    audit_path: Path | None = None
    if runtime is not None:
        audit = getattr(getattr(runtime, "manager", None), "audit", None)
        if audit is not None and getattr(audit, "path", None):
            audit_path = Path(audit.path)
    directory = (audit_path or default_audit_path()).parent
    probe = directory / f".doctor-probe-{os.getpid()}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.read_text(encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult(
            name, FAIL,
            f"state directory {directory} is not writable "
            f"({exc.strerror or exc}) - the audit log and queued "
            "confirmations cannot be stored there",
        )
    return CheckResult(
        name, OK,
        f"{directory} is writable (audit log lives here; high-risk "
        "actions wait in the confirmation queue before anything runs)",
    )


def _check_scenes(scenes_dir: str | Path | None) -> CheckResult:
    name = "scenes"
    if scenes_dir is None:
        from omnibutler.runtime import DEFAULT_SCENES_DIR

        scenes_dir = DEFAULT_SCENES_DIR
    directory = Path(scenes_dir)
    if not directory.is_dir():
        return CheckResult(name, WARN, f"no scenes directory at {directory}")
    files = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    if not files:
        return CheckResult(
            name, WARN, f"scenes directory {directory} holds no .yaml scene files"
        )
    from omnibutler.scenes.loader import load_scene_file

    errors: list[str] = []
    for path in files:
        try:
            load_scene_file(path)
        except Exception as exc:  # loader messages name the file + reason
            errors.append(str(exc))
    if errors:
        return CheckResult(
            name, FAIL,
            f"{len(errors)} of {len(files)} scene file(s) failed to parse: "
            + "; ".join(errors),
        )
    return CheckResult(name, OK, f"all {len(files)} scene file(s) in {directory} parse cleanly")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def check_all(
    source: Any = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    scenes_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[CheckResult]:
    """Run every health check and return the results in report order.

    ``source`` is a config dict, a built Runtime, or None (load the
    local config file). Network probes use ``timeout`` seconds per
    request, so a dead device adds seconds, not minutes.
    """
    environ = os.environ if environ is None else environ
    runtime = source if hasattr(source, "manager") else None
    results: list[CheckResult] = []

    config: dict[str, Any] = {}
    if isinstance(source, Mapping):
        config = dict(source)
    elif source is None:
        path = (
            Path(config_path)
            if config_path is not None
            else config_module.default_config_path(environ)
        )
        try:
            config = config_module.load_config(path, environ=environ)
            if path.exists():
                results.append(CheckResult("config", OK, f"loaded from {path}"))
        except config_module.ConfigError as exc:
            results.append(CheckResult("config", FAIL, str(exc)))
            config = {}

    # Runtime mode: prefer the live HA driver's own settings.
    ha = config_module.ha_settings(config, environ=environ)
    if runtime is not None:
        driver = runtime.manager.drivers.get("homeassistant")
        if driver is not None and getattr(driver, "configured", False):
            ha = {
                "url": driver.base_url,
                "token": driver.token,
                "token_description": "runtime driver (set)",
            }
    results.append(_check_home_assistant(ha, timeout))
    results.extend(_check_miio(_miio_entries(config, environ), timeout))
    tuya_entries, tuya_error = _tuya_entries(config, environ)
    results.append(_check_tuya(tuya_entries, tuya_error))
    results.append(_check_state_dir(runtime))
    results.append(_check_scenes(scenes_dir))
    return results


_LABELS = {OK: "[OK]  ", WARN: "[WARN]", FAIL: "[FAIL]"}


def format_report(results: list[CheckResult]) -> str:
    """Render check results as plain text, ready to print as-is."""
    lines = ["OmniButler health check", "=" * 23]
    for result in results:
        lines.append(f"{_LABELS[result.status]} {result.name}: {result.detail}")
    counts = {status: 0 for status in _STATUSES}
    for result in results:
        counts[result.status] += 1
    lines.append("")
    lines.append(
        f"{counts[OK]} ok, {counts[WARN]} warning(s), {counts[FAIL]} failure(s)"
    )
    if counts[FAIL]:
        lines.append("Fix the [FAIL] items first - they block real control.")
    elif counts[WARN]:
        lines.append("Nothing is broken; the [WARN] items are features you have not set up yet.")
    else:
        lines.append("Everything checks out.")
    return "\n".join(lines)

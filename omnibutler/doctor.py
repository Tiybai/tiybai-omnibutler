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
import json
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
from omnibutler.notify_webhook import resolve_webhook_url

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


def _check_config_version(
    config: Mapping[str, Any],
    config_file: Path | None,
    environ: Mapping[str, str],
    *,
    config_is_authoritative: bool = False,
) -> CheckResult:
    name = "config-version"
    current = config_module.CURRENT_CONFIG_VERSION
    if "version" in config:
        raw: Any = config["version"]
    elif config_is_authoritative:
        # The config was handed to us (or loaded from its file): an
        # absent version field is a version-1 config, full stop.
        return CheckResult(
            name, OK,
            "no \"version\" field - treated as version 1, which is "
            "the current format; it gets stamped in the next time "
            "a command writes the config",
        )
    else:
        path = config_file or config_module.default_config_path(environ)
        if not path.exists():
            if config:
                return CheckResult(
                    name, OK,
                    "no \"version\" field - treated as version 1, which "
                    "is the current format; it gets stamped in the next "
                    "time a command writes the config",
                )
            return CheckResult(
                name, WARN,
                f"no config file at {path} yet - nothing to version; "
                f"the first config write stamps version {current}",
            )
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return CheckResult(
                name, WARN,
                f"could not read a version from {path} - see the "
                "config result above for the actual problem",
            )
        if not isinstance(parsed, dict) or "version" not in parsed:
            return CheckResult(
                name, OK,
                "no \"version\" field - treated as version 1, which is "
                "the current format; it gets stamped in the next time "
                "a command writes the config",
            )
        raw = parsed["version"]
    try:
        version = config_module.config_version({"version": raw})
    except config_module.ConfigError as exc:
        # Same explanation the loader gives when it refuses the file.
        return CheckResult(name, FAIL, str(exc))
    migrated = "a migration path to the current format exists" \
        if version < current else "this is the current format"
    return CheckResult(
        name, OK, f"config version {version} - {migrated}")


def _probe_tcp(host: str, port: int, timeout: float) -> str | None:
    """Open one TCP connection: None when it connects, else the error.

    Reachability only - no protocol bytes are sent, so this is safe to
    point at any service (Matter controller, MQTT broker).
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as exc:
        return exc.strerror or str(exc)


def _check_tcp_endpoint(
    name: str,
    url: str,
    *,
    timeout: float,
    default_scheme: str,
    default_ports: Mapping[str, int],
    ok_detail: str,
    fail_hint: str,
) -> CheckResult:
    parsed = urlparse(url if "://" in url else f"{default_scheme}://{url}")
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        port = default_ports.get(parsed.scheme, next(iter(default_ports.values())))
    if not host:
        return CheckResult(
            name, FAIL,
            f"could not read a host out of the configured address "
            f"{url!r} - write it like {default_scheme}://192.168.1.10:{port}",
        )
    error = _probe_tcp(host, port, timeout)
    if error is not None:
        return CheckResult(
            name, FAIL,
            f"configured at {url} but nothing answers at {host}:{port} "
            f"({error}) - {fail_hint}",
        )
    return CheckResult(name, OK, ok_detail.format(host=host, port=port))


def _check_matter(
    config: Mapping[str, Any], environ: Mapping[str, str], timeout: float
) -> CheckResult:
    name = "matter"
    section = config_module.get_section(config, "matter")
    url = str(section.get("server_url") or section.get("url") or "").strip()
    if not url:
        url = environ.get("MATTER_SERVER_URL", "").strip()
    if not url:
        return CheckResult(
            name, WARN,
            "not configured - no Matter controller address. If you run "
            "a Matter controller service (matterjs-server or "
            "python-matter-server), set matter.server_url in the "
            "config file (or MATTER_SERVER_URL) and the Matter driver "
            "will talk to it",
        )
    return _check_tcp_endpoint(
        name, url, timeout=timeout,
        default_scheme="ws",
        default_ports={"ws": 80, "wss": 443},
        ok_detail="the Matter controller answers TCP at {host}:{port} "
                  "(reachability only - the doctor opens a plain TCP "
                  "connection; the real WebSocket handshake happens "
                  "when the driver connects)",
        fail_hint="is the Matter controller service running, and is "
                  "this machine on the same network?",
    )


def _check_zigbee2mqtt(
    config: Mapping[str, Any], environ: Mapping[str, str], timeout: float
) -> CheckResult:
    name = "zigbee2mqtt"
    section = config_module.get_section(config, "zigbee2mqtt")
    url = str(
        section.get("mqtt_url") or section.get("broker_url")
        or section.get("url") or ""
    ).strip()
    if not url:
        url = environ.get("Z2M_MQTT_URL", "").strip()
    if not url:
        return CheckResult(
            name, WARN,
            "not configured - no MQTT broker address for Zigbee2MQTT. "
            "If you run Zigbee2MQTT, set zigbee2mqtt.mqtt_url in the "
            "config file (or Z2M_MQTT_URL) so the bridge can reach "
            "its broker",
        )
    return _check_tcp_endpoint(
        name, url, timeout=timeout,
        default_scheme="mqtt",
        default_ports={"mqtt": 1883, "mqtts": 8883},
        ok_detail="an MQTT broker answers TCP at {host}:{port} "
                  "(reachability only - this does not check the MQTT "
                  "username/password, and a broker answering does not "
                  "by itself prove Zigbee2MQTT is running)",
        fail_hint="is the MQTT broker running, and is this machine "
                  "on the same network?",
    )


def _check_gateway_token(environ: Mapping[str, str]) -> CheckResult:
    name = "gateway-token"
    if environ.get("OMNIBUTLER_GATEWAY_TOKEN", "").strip():
        return CheckResult(
            name, OK,
            "the phone-gateway token is set (its value is never "
            "shown) - `tob gateway` will accept uploads that carry it",
        )
    return CheckResult(
        name, WARN,
        "the phone-gateway token is not set - only needed if you use "
        "the phone gateway (`tob gateway`), which refuses to start "
        "without one. Set OMNIBUTLER_GATEWAY_TOKEN to a long random "
        "string first",
    )


def _check_notify_webhook(config: Mapping[str, Any],
                          environ: Mapping[str, str]) -> CheckResult:
    """Approval webhook: is a notification URL configured?

    Presence only, like the gateway-token check - the URL is never
    shown (webhook URLs commonly embed a topic or key that acts as a
    credential). An optional extra, so "not configured" is a WARN with
    an explanation, not a failure.
    """
    name = "notify-webhook"
    if resolve_webhook_url(config, environ=environ) is not None:
        return CheckResult(
            name, OK,
            "an approval webhook is configured (its URL is never "
            "shown) - the daemon POSTs one notification there when a "
            "high-risk action joins the confirmation queue",
        )
    return CheckResult(
        name, WARN,
        "no approval webhook is configured - only needed if you want "
        "a phone push when a high-risk action waits for approval. "
        "Set OMNIBUTLER_NOTIFY_WEBHOOK_URL or notify.webhook_url in "
        "the config file (ntfy, Bark and similar services work)",
    )


def _check_cloud_fallbacks(config: Mapping[str, Any],
                           environ: Mapping[str, str]) -> CheckResult:
    """Vendor-cloud fallback drivers: are credentials present?

    Presence only, like the gateway-token check - values are never
    read out or shown. A cloud fallback is an explicit, optional
    extra (neither cloud driver joins the `all` set), so "nothing
    configured" is a WARN with an explanation, not a failure.
    """
    name = "cloud-fallbacks"
    configured: list[str] = []

    def section_has(section: str, *keys: str) -> bool:
        data = config.get(section)
        if not isinstance(data, Mapping):
            return False
        return all(str(data.get(key) or "").strip() for key in keys)

    def env_has(*keys: str) -> bool:
        return all(environ.get(key, "").strip() for key in keys)

    if (env_has("TUYA_CLOUD_ACCESS_ID", "TUYA_CLOUD_ACCESS_SECRET")
            or section_has("tuya_cloud", "access_id", "access_secret")):
        configured.append("tuya_cloud")
    if (env_has("XIAOMI_CLOUD_USERNAME", "XIAOMI_CLOUD_PASSWORD")
            or section_has("xiaomi_cloud", "username", "password")):
        configured.append("xiaomi_cloud")

    if configured:
        return CheckResult(
            name, OK,
            "vendor-cloud fallback credentials are set for: "
            + ", ".join(configured)
            + " (values are never shown). These drivers only run when "
            "selected explicitly, e.g. --driver tuya_cloud",
        )
    return CheckResult(
        name, WARN,
        "no vendor-cloud fallback is configured - only needed for a "
        "device with no local path. See `tob setup tuya_cloud` or "
        "`tob setup xiaomi_cloud`; local control never needs them",
    )


def _human_size(num_bytes: int) -> str:
    """A byte count the way a human reads it: 812 B, 3.2 MB, 1.4 GB."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover - loop always returns


def _dir_total_size(directory: Path) -> int:
    """Total size of every file under ``directory`` (best effort)."""
    total = 0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue  # a file vanishing mid-walk is not a failure
    return total


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
    size = _human_size(_dir_total_size(directory))
    return CheckResult(
        name, OK,
        f"{directory} is writable (audit log lives here; high-risk "
        f"actions wait in the confirmation queue before anything runs). "
        f"The directory currently holds {size} in total - the audit log "
        "and data streams rotate by size, so this stays bounded",
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
    config_file: Path | None = None
    config_is_authoritative = False
    if isinstance(source, Mapping):
        config = dict(source)
        config_is_authoritative = True
    elif source is None:
        path = (
            Path(config_path)
            if config_path is not None
            else config_module.default_config_path(environ)
        )
        config_file = path
        try:
            config = config_module.load_config(path, environ=environ)
            config_is_authoritative = path.exists()
            if path.exists():
                results.append(CheckResult("config", OK, f"loaded from {path}"))
        except config_module.ConfigError as exc:
            results.append(CheckResult("config", FAIL, str(exc)))
            config = {}
    results.append(_check_config_version(
        config, config_file, environ,
        config_is_authoritative=config_is_authoritative))

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
    results.append(_check_matter(config, environ, timeout))
    results.append(_check_zigbee2mqtt(config, environ, timeout))
    results.append(_check_gateway_token(environ))
    results.append(_check_notify_webhook(config, environ))
    results.append(_check_cloud_fallbacks(config, environ))
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

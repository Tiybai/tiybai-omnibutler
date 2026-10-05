"""Local configuration file for OmniButler.

One JSON file holds the operator's local setup - driver endpoints and
device lists - so nothing has to be squeezed into environment variables
once more than a device or two is involved:

    ~/.omnibutler/config.json          (override: $OMNIBUTLER_CONFIG)

Shape (all sections optional)::

    {
      "version": 1,
      "ha":   {"url": "http://192.168.1.10:8123",
               "token": "env:HA_TOKEN"},
      "miio": {"devices": [{"id": "living_ac", "host": "192.168.1.31",
                            "token": "env:MIIO_LIVING_AC_TOKEN",
                            "model": "zhimi.aircondition.v1"}]},
      "tuya": {"devices": [{"device_id": "bf...", "ip": "192.168.1.50",
                            "local_key": "env:TUYA_PLUG_LOCAL_KEY"}]}
    }

Secrets (HA token, miIO tokens, Tuya local_keys) should be *references*,
not literals: a value of the form ``"env:VARNAME"`` is read from that
environment variable at use time, so the config file itself stays safe to
back up or show around. A literal value also works (the file is written
with 0600 permissions by the setup helpers), and an HA token may instead
be named with a plain ``"token_env": "HA_TOKEN"`` field. Either way,
nothing in this package ever prints a resolved secret value: helpers
here describe secrets only as set / not set.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from omnibutler.core.errors import OmniButlerError

CONFIG_ENV_VAR = "OMNIBUTLER_CONFIG"
ENV_REF_PREFIX = "env:"

#: Config keys whose values are secrets and may be "env:" references.
SECRET_KEYS = ("token", "local_key")


class ConfigError(OmniButlerError):
    """The local config file exists but cannot be used as written."""


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Where the config file lives: $OMNIBUTLER_CONFIG or ~/.omnibutler/."""
    environ = os.environ if environ is None else environ
    override = environ.get(CONFIG_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".omnibutler" / "config.json"


def load_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Read the local config file and return it as a plain dict.

    A missing file is not an error - it simply means "nothing configured
    here" and returns ``{}`` (callers then fall back to the drivers'
    environment variables). A file that exists but is not valid JSON, or
    whose top level is not a JSON object, raises :class:`ConfigError`
    naming the file and the parse problem - never the file's contents.
    Secret values are returned unresolved; use :func:`resolve_secret`.
    """
    config_path = Path(path) if path is not None else default_config_path(environ)
    if not config_path.exists():
        return {}
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"cannot read config file {config_path}: {exc.strerror or exc}"
        ) from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"config file {config_path} is not valid JSON "
            f"({exc.msg} at line {exc.lineno}, column {exc.colno}); "
            "fix it or delete it and run the setup again"
        ) from exc
    if not isinstance(parsed, dict):
        raise ConfigError(
            f"config file {config_path} must contain a JSON object at the "
            f"top level, not {type(parsed).__name__}"
        )
    return migrate_config(parsed)


# ---------------------------------------------------------------------------
# Format versioning (design: docs/config-versioning.md)
# ---------------------------------------------------------------------------

#: The config format version this build writes and understands.
CURRENT_CONFIG_VERSION = 1

#: Migration registry: ``MIGRATIONS[n]`` turns a version-n config dict
#: into a version-(n+1) one. Migrations are pure dict -> dict functions
#: with no I/O; they run on the raw parsed JSON *before* any ``env:``
#: secret resolution, so they never see a real secret value. Empty
#: while the format is at version 1 - the first format change registers
#: its step here, e.g. ``MIGRATIONS[1] = _migrate_1_to_2`` alongside a
#: ``CURRENT_CONFIG_VERSION`` bump to 2.
MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def config_version(data: Mapping[str, Any]) -> int:
    """The format version of a parsed config dict.

    A missing ``version`` field means version 1 (every config file
    written before versioning existed is a version-1 file). A version
    that is not an integer, is below 1, or is newer than
    :data:`CURRENT_CONFIG_VERSION` raises :class:`ConfigError` - a
    config written by a newer OmniButler is refused with an explanation,
    never silently misread.
    """
    if "version" not in data:
        return 1
    value = data["version"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"config version must be an integer, got {value!r} - fix the "
            "\"version\" field in the config file or remove it (a missing "
            "version is treated as 1)"
        )
    if value < 1:
        raise ConfigError(
            f"config version {value} is not a real version - fix the "
            "\"version\" field in the config file or remove it (a missing "
            "version is treated as 1)"
        )
    if value > CURRENT_CONFIG_VERSION:
        raise ConfigError(
            f"this config was written by a newer OmniButler (config "
            f"version {value}); this build only understands up to "
            f"version {CURRENT_CONFIG_VERSION}. Please upgrade "
            "OmniButler instead of editing the version number down - "
            "the newer format may store settings this build would "
            "misread or drop."
        )
    return value


def migrate_config(data: dict[str, Any]) -> dict[str, Any]:
    """Validate a parsed config and walk it up to the current version.

    Applies the :data:`MIGRATIONS` chain in memory only - loading never
    rewrites the user's file (see :func:`save_config` for the write-back
    rules). Raises :class:`ConfigError` for an unusable version field
    or a missing migration step.
    """
    version = config_version(data)
    migrated = data
    while version < CURRENT_CONFIG_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            raise ConfigError(
                f"no migration is registered from config version "
                f"{version} to {version + 1}; this build cannot load "
                "the config safely - please upgrade OmniButler"
            )
        migrated = step(migrated)
        version += 1
    return migrated


def save_config(path: str | Path, data: Mapping[str, Any]) -> Path:
    """Write a config dict to ``path`` under the versioning rules.

    The file is written atomically with mode 0600 (same discipline as
    the setup helpers) and stamped with
    ``"version": CURRENT_CONFIG_VERSION`` - the caller's dict is copied,
    never mutated. Loading never rewrites the file, so this write-back
    is the one moment an older file is upgraded on disk: before the
    first write-back of a file that predates the current format (no
    ``version`` field, or a lower one), a one-time backup is kept next
    to it as ``<name>.v<old>.bak`` (e.g. ``config.json.v1.bak``); an
    existing backup is never overwritten, so the first backup stays the
    pristine pre-versioning copy. A file that exists but is not valid
    JSON, or not a JSON object, raises :class:`ConfigError` instead of
    being clobbered.
    """
    config_path = Path(path)
    payload = dict(data)
    payload["version"] = CURRENT_CONFIG_VERSION
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(
                f"config file {config_path} exists but cannot be parsed "
                f"({exc}); refusing to overwrite it - fix it by hand first"
            ) from exc
        if not isinstance(existing, dict):
            raise ConfigError(
                f"config file {config_path} must contain a JSON object "
                "at the top level; refusing to overwrite it"
            )
        raw_version = existing.get("version")
        predates = "version" not in existing or (
            isinstance(raw_version, int)
            and not isinstance(raw_version, bool)
            and raw_version < CURRENT_CONFIG_VERSION
        )
        if predates:
            old = (
                raw_version
                if isinstance(raw_version, int)
                and not isinstance(raw_version, bool)
                and raw_version >= 1
                else 1
            )
            backup = config_path.with_name(f"{config_path.name}.v{old}.bak")
            if not backup.exists():
                backup.write_bytes(config_path.read_bytes())
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(config_path.parent), prefix=".config-", suffix=".tmp"
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, config_path)
        os.chmod(config_path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return config_path


def get_section(config: Mapping[str, Any], name: str) -> dict[str, Any]:
    """Return one config section (``"ha"``, ``"miio"``, ...) as a dict."""
    section = config.get(name)
    return dict(section) if isinstance(section, dict) else {}


def is_env_ref(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(ENV_REF_PREFIX)


def resolve_secret(
    value: Any,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve a secret field to its value, or None when unavailable.

    ``"env:VARNAME"`` reads VARNAME from the environment (missing or
    empty counts as unavailable); any other non-empty string is taken as
    the literal value. The result is a secret: callers must not log it.
    """
    environ = os.environ if environ is None else environ
    if not isinstance(value, str) or not value.strip():
        return None
    if is_env_ref(value):
        resolved = environ.get(value[len(ENV_REF_PREFIX):].strip(), "")
        return resolved.strip() or None
    return value


def describe_secret(
    value: Any,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """A log-safe description of a secret field: how it is supplied and
    whether it resolves - never the value itself."""
    environ = os.environ if environ is None else environ
    if value is None or (isinstance(value, str) and not value.strip()):
        return "not set"
    if is_env_ref(value):
        var = value[len(ENV_REF_PREFIX):].strip()
        state = "set" if environ.get(var, "").strip() else "NOT set"
        return f"env:{var} ({state})"
    if isinstance(value, str):
        return "literal value (set)"
    return "not set"


def ha_settings(
    config: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Effective Home Assistant settings: config section over env vars.

    Returns ``{"url", "token", "token_description"}`` where ``token`` is
    the resolved secret (or None) and ``token_description`` is the
    log-safe form from :func:`describe_secret`. The ``ha`` section may
    carry ``url``, ``token`` (literal or ``env:`` ref) and ``token_env``
    (plain name of an environment variable holding the token). With no
    config section, HA_URL / HA_TOKEN apply, exactly like the driver.
    """
    environ = os.environ if environ is None else environ
    section = get_section(config, "ha")
    url = str(section.get("url") or environ.get("HA_URL", "")).strip().rstrip("/")
    token_field: Any = section.get("token")
    if token_field is None and section.get("token_env"):
        token_field = f"{ENV_REF_PREFIX}{str(section['token_env']).strip()}"
    if token_field is None:
        # No token in the config file: fall back to HA_TOKEN, like the
        # driver does when no config exists at all.
        env_token = environ.get("HA_TOKEN", "").strip()
        return {
            "url": url or None,
            "token": env_token or None,
            "token_description": "env:HA_TOKEN (set)" if env_token else "not set",
        }
    return {
        "url": url or None,
        "token": resolve_secret(token_field, environ=environ),
        "token_description": describe_secret(token_field, environ=environ),
    }


def device_entries(
    config: Mapping[str, Any],
    section_name: str,
    secret_key: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Device entries of one section with the secret field resolved.

    Each returned entry is a copy of the configured object with
    ``secret_key`` replaced by its resolved value (None when the field is
    missing or its ``env:`` reference is unset) plus a
    ``"<secret_key>_description"`` log-safe companion. Non-object entries
    are passed through untouched so callers can flag them.
    """
    environ = os.environ if environ is None else environ
    section = get_section(config, section_name)
    raw_entries = section.get("devices")
    if not isinstance(raw_entries, list):
        return []
    entries: list[dict[str, Any]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            entries.append({"_invalid_entry": raw})
            continue
        entry = dict(raw)
        field = entry.get(secret_key)
        entry[secret_key] = resolve_secret(field, environ=environ)
        entry[f"{secret_key}_description"] = describe_secret(field, environ=environ)
        entries.append(entry)
    return entries

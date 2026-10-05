"""Backup and restore of the butler's local config + state.

``tob backup`` packs the config file and the whole state directory
(the audit log and its rotations, the data streams, the persisted
confirmation queue, learned Broadlink codes) into one ``.tar.gz``;
``tob restore`` unpacks it again - after moving whatever is currently
in place aside to a ``.bak`` sibling, the same keep-the-old-copy
discipline the config writer uses (see ``omnibutler.config``).

Archive layout (the only two top-level names restore accepts):

* ``config.json``   - the config file, from wherever it lives
* ``state/<name>``  - every regular file of the state directory

Restore validates *every* member before touching the disk: absolute
paths, ``..`` components, link members and anything outside the two
prefixes above are rejected, and members are written out by hand
(never ``extractall``), so a hostile archive cannot land a file
outside the state directory. Two things are deliberately *not* in a
backup: the live daemon lock (``daemon.lock`` - meaningless, and
dangerous, in a backup) and transient ``*.tmp-*`` files.

A backup can contain secrets: the config file may hold device keys
inline (they are usually ``env:`` references, but nothing forces
that). The CLI says so out loud; encrypting the archive is out of
scope here.
"""

from __future__ import annotations

import contextlib
import tarfile
import time
from pathlib import Path

from omnibutler.core.errors import OmniButlerError
from omnibutler.instance_lock import LOCK_FILENAME

CONFIG_ARCNAME = "config.json"
STATE_PREFIX = "state"


class BackupError(OmniButlerError):
    """A backup or restore could not be done (nothing was clobbered)."""


def _is_transient(path: Path) -> bool:
    """Files that must never travel in a backup (lock, temp writes)."""
    return path.name == LOCK_FILENAME or ".tmp-" in path.name


def collect_members(
    config_path: Path,
    state_dir: Path,
) -> list[tuple[str, Path]]:
    """``(arcname, source)`` pairs for one backup, sorted by arcname."""
    members: dict[str, Path] = {}
    if config_path.is_file():
        members[CONFIG_ARCNAME] = config_path
    if state_dir.is_dir():
        for path in sorted(state_dir.rglob("*")):
            if not path.is_file() or _is_transient(path):
                continue
            if config_path.is_file() and path == config_path:
                continue  # already packed under its canonical name
            rel = path.relative_to(state_dir).as_posix()
            members[f"{STATE_PREFIX}/{rel}"] = path
    return sorted(members.items())


def create_backup(
    dest: Path,
    config_path: Path,
    state_dir: Path,
) -> list[tuple[str, int]]:
    """Write the ``.tar.gz``; return ``(arcname, size)`` manifest entries."""
    members = collect_members(config_path, state_dir)
    if not members:
        raise BackupError(
            f"nothing to back up: no config at {config_path} and no "
            f"state files in {state_dir}")
    manifest = [(arcname, src.stat().st_size) for arcname, src in members]
    with tarfile.open(dest, "w:gz") as tar:
        for arcname, src in members:
            tar.add(src, arcname=arcname, recursive=False)
    # The archive carries the config verbatim (device keys included),
    # so it gets the config file's own discipline: owner-only access.
    # Best effort - filesystems without POSIX modes simply keep their
    # default permissions.
    with contextlib.suppress(OSError):
        dest.chmod(0o600)
    return manifest


def _validate_member_name(name: str) -> list[str]:
    """Normalise an archive member name to safe parts, or raise.

    Only ``config.json`` and ``state/<...>`` are accepted; absolute
    paths, drive letters, ``..`` and empty names are rejected.
    """
    parts = [p for p in name.replace("\\", "/").split("/")
             if p not in ("", ".")]
    if not parts:
        raise BackupError(f"backup member has an empty name: {name!r}")
    if any(p == ".." for p in parts):
        raise BackupError(f"backup member escapes its target: {name!r}")
    if len(parts[0]) >= 2 and parts[0][1] == ":":
        raise BackupError(f"backup member is an absolute path: {name!r}")
    if parts == [CONFIG_ARCNAME] or (parts[0] == STATE_PREFIX and len(parts) > 1):
        return parts
    raise BackupError(
        f"unexpected backup member {name!r}: only {CONFIG_ARCNAME!r} "
        f"and '{STATE_PREFIX}/...' belong in an omnibutler backup")


def _free_bak_path(path: Path) -> Path:
    """``<path>.bak``, or with a timestamp/counter when that is taken."""
    candidate = path.with_name(path.name + ".bak")
    if not candidate.exists():
        return candidate
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    counter = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{counter}")
        counter += 1
    return candidate


def restore_backup(
    archive: Path,
    config_path: Path,
    state_dir: Path,
) -> dict:
    """Restore one archive; return a report dict for the CLI to print.

    Report keys: ``restored`` (arcnames written), ``preserved``
    (paths the previous config/state were moved to, possibly empty).

    Every member is validated before anything on disk moves, so a
    rejected archive leaves the current install exactly as it was.
    """
    if not archive.is_file():
        raise BackupError(f"backup file not found: {archive}")
    try:
        with tarfile.open(archive, "r:*") as tar:
            return _restore_members(tar, config_path, state_dir)
    except (tarfile.TarError, OSError) as exc:
        raise BackupError(
            f"backup {archive} could not be restored: {exc}") from exc


def _restore_members(
    tar: tarfile.TarFile,
    config_path: Path,
    state_dir: Path,
) -> dict:
    """Validate, preserve and extract (see restore_backup's contract)."""
    plan: list[tuple[list[str], tarfile.TarInfo]] = []
    for member in tar.getmembers():
        parts = _validate_member_name(member.name)
        if member.isdir():
            continue
        if not member.isfile():
            raise BackupError(
                f"backup member {member.name!r} is not a regular "
                f"file - refusing to restore links or devices")
        plan.append((parts, member))
    if not plan:
        raise BackupError("backup contains no files")

    preserved: list[Path] = []
    state_inside = state_dir in config_path.parents
    if state_dir.is_dir() and any(state_dir.iterdir()):
        bak = _free_bak_path(state_dir)
        state_dir.rename(bak)
        preserved.append(bak)
    if config_path.is_file() and not state_inside:
        bak = _free_bak_path(config_path)
        config_path.rename(bak)
        preserved.append(bak)

    restored: list[str] = []
    for parts, member in plan:
        target = (config_path if parts == [CONFIG_ARCNAME]
                  else state_dir.joinpath(*parts[1:]))
        target.parent.mkdir(parents=True, exist_ok=True)
        source = tar.extractfile(member)
        if source is None:  # validated isfile() above; defensive
            raise BackupError(f"cannot read backup member {member.name!r}")
        target.write_bytes(source.read())
        if parts == [CONFIG_ARCNAME]:
            # Config files are 0600 by house rule.
            with contextlib.suppress(OSError):
                target.chmod(0o600)
        restored.append(member.name)
    return {"restored": restored, "preserved": preserved}

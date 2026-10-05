# Config file versioning - design note

Status: **implemented in v0.5** - see the implementation status section
at the end of this document. The design below is the original plan and
is unchanged.

## Where we are today (v0.3.x)

- `omnibutler/config.py` loads `~/.omnibutler/config.json` (or
  `$OMNIBUTLER_CONFIG`) as a plain JSON object and reads well-known
  sections (`ha`, `miio`, `tuya`, ...) with per-section helpers.
- There is **no version field** - neither written by `tob setup-secret`
  nor read by the loader. Unknown keys are ignored, missing sections
  simply mean "not configured", and secret values may be literal strings
  or `"env:VARNAME"` references.
- Because nothing is versioned yet, any future format change (renamed
  keys, restructured device lists) would land with no way for the loader
  to tell an old file from a new one.

The saving grace: the format has never shipped a breaking change, and
every reader tolerates missing/extra keys. So "no version" is still
exactly equivalent to "version 1" - which is what the plan below builds
on.

## The v1 plan

1. **Top-level version key.** New and rewritten config files carry:

   ```json
   { "version": 1, "ha": { "...": "..." }, "miio": { "...": "..." } }
   ```

2. **Missing means 1.** When loading, an absent `version` is treated as
   `1` - all existing user files keep working untouched. A non-integer
   or a version newer than the running code understands is a hard
   `ConfigError` ("config written by a newer OmniButler, please
   upgrade"), never a silent misread.

3. **Migration registry.** Each future format bump registers one small,
   pure function in a table in `config.py`:

   ```python
   MIGRATIONS = {
       1: migrate_1_to_2,   # dict -> dict, no I/O, no secret resolution
       2: migrate_2_to_3,
   }
   CURRENT_CONFIG_VERSION = 3
   ```

   The loader walks the chain from the file's version up to
   `CURRENT_CONFIG_VERSION`, in memory. Migrations operate on the raw
   JSON dict *before* any `env:` secret resolution, so they never see -
   and can never leak - a real secret value.

4. **Write-back rules.** Loading never rewrites the user's file. The
   migrated shape is written back only when a command already writes
   config (`tob setup-secret`), and then atomically, keeping the 0600
   permissions, with the new `version` stamped in. Before the first
   write-back of an older file, keep a one-time backup next to it
   (`config.json.v1.bak`).

5. **Doctor support.** `tob doctor` gains a line reporting the config
   version found and whether a migration path exists, so "my config
   stopped working after upgrade" is diagnosable from one report.

## Rules for future format changes

- Only additive changes (new optional keys/sections) may ship *without*
  a version bump; readers already ignore unknown keys.
- Renames, moves, type changes, or semantic changes to existing keys
  require a bump + a migration + a fixture test (an old-format sample
  file in `tests/` that must load to the expected shape).
- `config.example.json` always shows the current version.

## Explicitly out of scope for v1

- Versioning the runtime *state* files (confirmation queue, audit log)
  in the same directory - they have their own formats and lifecycles;
  if they ever need it, they get separate version keys, not this one.
- Encrypting the config at rest. Secrets already live in environment
  variables by reference; the file itself is 0600.

## Implementation status (v0.5)

Landed, in `omnibutler/config.py` unless noted otherwise:

- `CURRENT_CONFIG_VERSION = 1` and `config_version(data)`: a missing
  `version` reads as 1; a non-integer, below-1, or newer-than-current
  version is a hard `ConfigError` ("written by a newer OmniButler,
  please upgrade"), raised by `load_config` before anything else
  touches the config.
- `MIGRATIONS` registry + `migrate_config(data)`: the loader walks
  the chain in memory on the raw parsed dict (before any `env:`
  resolution). The registry is empty at version 1; the first format
  bump registers its step there.
- `save_config(path, data)`: the versioned write path - stamps
  `version`, writes atomically with mode 0600, and keeps the one-time
  backup (`config.json.v1.bak`, never overwritten) before the first
  write-back of a file that predates the current format. Loading still
  never rewrites the file.
- Doctor: `tob doctor` has a `config-version` line reporting the
  version found, whether it is current, and - for a too-new config -
  the same refusal explanation the loader gives.
  (In `omnibutler/doctor.py`.)
- Covered by `tests/test_config_versioning.py` and the config-version
  cases in `tests/test_doctor_new_checks.py`.

One wiring step remains: the older direct writers -
`setup_guide.store_secret` and the config merge in the CLI
(`fetch-keys --store`) - still write the file with their own code.
They behave correctly today (version 1 needs no migration and readers
treat a missing version as 1), but they should switch to
`config.save_config` so the version stamp and backup rules apply to
every write; that switch touches files outside the v0.5 doctor/config
change and is tracked as the follow-up. `config.example.json` should
likewise gain a top-level `"version": 1` to match the rule above.

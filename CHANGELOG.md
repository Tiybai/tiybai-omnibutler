# Changelog

All notable changes to Tiybai OmniButler are recorded here, one entry per
release. The format follows [Keep a Changelog](https://keepachangelog.com/).
For the full release notes, see
[GitHub Releases](https://github.com/Tiybai/tiybai-omnibutler/releases).

Every driver below is tested against fake devices unless noted otherwise -
real-hardware verification is still the project's open gap.

## [0.10.0] - 2026-10-05

The audit round: four independent audits (cross-platform, security
and concurrency, data layer and performance, docs and UI alignment)
went over the whole codebase; every finding below was fixed, and the
few accepted trade-offs are recorded in the docs.

### Added
- CI test matrix now includes Windows and macOS runners
  (Python 3.12), so platform-specific breakages are caught before
  release instead of after.
- docs/platform-support.md: per-platform notes for Windows, macOS,
  Linux, Android (Termux) and iOS (Shortcuts + approvals page -
  iOS cannot run the daemon itself).
- The approvals page is bilingual: `?lang=zh|en` wins, otherwise
  the browser's Accept-Language decides; dark-mode and
  phone-friendly styling included.

### Fixed
- Confirmation queue hardening: all queue mutations are locked
  in-process and across processes, and a queued action is claimed
  atomically before execution - approving the same item from two
  places can no longer run it twice. The queue file is now 0600,
  corrupt files are preserved as .corrupt instead of silently
  dropped, and decided items older than 30 days are pruned.
- `save_config` crashed on Windows (`os.fchmod` is Unix-only) -
  storing a secret via `tob setup-secret` or `tob fetch-keys
  --store` now works there; config writes are also locked against
  concurrent processes, and version backups get 0600.
- The phone gateway only accepts geofence and presence events.
  It could previously relay forged state_change/schedule/session
  events and trigger scenes.
- `tob setup` now lists all eight guides; five topics (matter,
  zigbee2mqtt, gateway, tuya_cloud, xiaomi_cloud) were unreachable
  from the CLI.
- Webhook failure messages no longer include the webhook URL.
- Broadlink code store writes atomically and recovers from a
  corrupt file instead of taking the driver down.
- Scene files are size-capped (1 MiB) and duplicate YAML keys are
  an error instead of silently overriding.

### Changed
- Audit reads stream line by line and skip torn lines instead of
  failing; `tob audit --last 20` on a full 60 MB log went from
  1.8 s / 362 MB to 0.13 s / ~45 MB.
- Data streams load lazily (everyday commands no longer pay for
  history they do not read) and appending a point no longer
  re-sorts the whole series (about 90x faster at 50k points).
- Polling runs drivers in parallel (bounded pool, serial within a
  driver; OMNIBUTLER_POLL_SERIAL=1 restores the old behaviour).
  One device failing no longer aborts the rest of its driver, and
  the Matter driver caches its node list for 60 seconds.
- Approval webhook notifications are sent from a background queue
  instead of blocking the daemon loop.
- MCP stdio runs over explicit UTF-8 streams and survives
  malformed messages with proper JSON-RPC errors.
- HTTP surfaces share hardening: malformed/oversized bodies close
  the connection, handlers have a 30 s socket timeout, non-loopback
  binds validate the Host header, cloud response bodies are read
  with a 4 MiB cap, and device ids are URL-quoted.
- Backup archives are created 0600 (they contain your config).

## [0.9.0] - 2026-10-05

### Added
- Delayed scene actions (`- delay: <seconds>` between actions; a
  re-trigger restarts the timer, and a delayed high-risk action still
  queues for human confirmation when it comes due).
- Home Assistant event-stream subscription (WebSocket): state changes
  reach the scene engine near-instantly; the 30-second poll stays as
  fallback and the two paths de-duplicate against one snapshot.
- Third-party drivers as pip packages (entry point group
  `omnibutler.drivers`) - no fork needed. Built-ins win name
  conflicts; a broken package is skipped with a clear error.
- `tob audit` reads the audit log back across rotated files;
  `tob backup` / `tob restore` pack config and state into one tarball
  (restore keeps a .bak and rejects unsafe member paths).
- device-data: generic Matter light and Zigbee smart-plug profiles
  (20 in total); the mock demo home gains a robot vacuum.

### Changed
- The daemon takes a single-instance lock per state directory and
  refuses to start twice (the stale lock of a dead process is taken
  over with a note).
- CI: coverage floor of 80% (currently 87%), test matrix adds
  Python 3.13; `pip install tiybai-omnibutler[all]` installs every
  driver extra; the package ships `py.typed`.

## [0.8.0] - 2026-10-05

### Added
- Xiaomi robot vacuums in the local miio driver (start / stop / return to
  dock, battery level) via the standard MIOT vacuum service; the Xiaomi
  cloud fallback inherits the family. device-data grows to 18 profiles.
- Webhook notification when a high-risk action is parked in the
  confirmation queue (`OMNIBUTLER_NOTIFY_WEBHOOK_URL` or
  `notify.webhook_url`) - a headless bridge no longer queues in silence.

### Changed
- Scene schedules accept `days`, so "weekdays at 07:30" finally works.
- CI gains lint (ruff) and type-check (mypy) gates; both clean.

## [0.7.0] - 2026-10-05

### Added
- Xiaomi light, fan and humidifier families (standard MIOT services);
  the Xiaomi cloud fallback picks them up automatically.
- Tuya curtain motors (open/close + position), matching their
  device-data profile.
- Scene state conditions accept `for_seconds` - a value must hold for a
  duration, not just spike, before a scene fires.

### Changed
- Audit log and data streams rotate by size (default 10 MiB x 5,
  tunable), so a long-running butler cannot fill the disk.
- `tob doctor` reports the state directory's total size.

## [0.6.1] - 2026-10-05

### Added
- CI gains gitleaks secret scanning (working tree and history) and a
  dependency licence gate.
- `tob doctor` reports whether cloud-fallback credentials are present -
  never their values.

### Fixed
- Removed stale "gateway not implemented" notes from the Dockerfile,
  compose example and install docs; the gateway shipped in v0.4/v0.5.

## [0.6.0] - 2026-10-05

### Added
- Xiaomi cloud fallback driver (`--driver xiaomi_cloud`): when a Xiaomi
  device cannot be reached locally, it can be controlled through the
  vendor cloud using the same login machinery as `tob fetch-keys` and
  the same MIOT property mapping as the local driver. Like `tuya_cloud`
  it is deliberately not part of `all`.
- device-data grows to 14 profiles (Zigbee door and motion sensors, a
  generic Matter plug, a Tuya curtain motor).

## [0.5.1] - 2026-10-05

### Changed
- Every config write path (setup key storage, fetch-keys merge) now goes
  through the single versioned `save_config`, so the version stamp and
  one-time backup apply everywhere; `config.example.json` carries the
  top-level `"version": 1`.

## [0.5.0] - 2026-10-05

### Added
- MCP surface covers data streams and terminal sessions (12 tools).
- The phone gateway can run inside the daemon (`tob run --gateway`) -
  one process, scenes fire exactly once.
- Config files are versioned: a missing version reads as v1, a
  newer-than-supported version is a hard error, and the first rewrite
  keeps a one-time backup.
- Tuya cloud fallback driver (`--driver tuya_cloud`) for devices with no
  local path; going cloud is always an explicit choice.
- `tob streams` command; `tob setup` guides for matter, zigbee2mqtt,
  gateway and tuya_cloud.

### Changed
- `tob doctor` also checks Matter controller and Zigbee2MQTT broker
  reachability and config-version health.

## [0.4.0] - 2026-10-05

### Added
- Matter driver (controller-client mode over WebSocket against a
  matterjs-server-class service; pairing codes are only forwarded).
- Zigbee2MQTT driver.
- Phone gateway and data streams (`tob gateway`, port 8767, its own
  token); geofence events can trigger scenes.
- Terminal sessions (demo glasses display/speak, session open/close
  scene triggers).
- `tob onboard` scan reporting what each device still needs, and
  `tob fetch-keys` cloud key retrieval for Xiaomi/Tuya (stops plainly
  at captcha / 2FA instead of guessing).
- Docker, launchd and systemd packaging; bundled scenes ship inside
  the wheel.

## [0.3.1] - 2026-10-05

### Added
- Approvals web page (`tob approvals`) and macOS popup notifications
  for the human confirmation queue.

## [0.3.0] - 2026-10-05

### Added
- Midea driver (msmart-ng) and Broadlink driver (learn / send IR codes).
- `tob run` resident daemon; `tob mcp --http` with a mandatory token.
- `tob doctor` real health checks and `tob setup` plain-language key
  guides.

### Changed
- Human-only confirmations enforced in code: there is no MCP approve
  tool, the queue persists on disk across processes, and approval
  happens only via `tob pending` / `tob confirm` / `tob reject` on
  the host.

## [0.2.0] - 2026-10-05

### Added
- First real local drivers: the miio (Xiaomi) driver, an original
  clean-room implementation written from the protocol spec in
  `docs/specs/`, and a Tuya local driver via tinytuya.
- device-data grows to 10 profiles; Home Assistant driver and scene
  engine improvements.

## [0.1.0] - 2026-10-05

### Added
- Core capability model, device registry, event bus and append-only
  audit log.
- Mock driver with a 7-device virtual home; Home Assistant REST driver.
- Deterministic local scene engine; high-risk actions are diverted to
  a human confirmation queue instead of executing.
- Zero-dependency MCP server (stdio, 8 tools) and the `tob` CLI.
- device-data format, plus clean-room, licence and security docs;
  bilingual README and CI.

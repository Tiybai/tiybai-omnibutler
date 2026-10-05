# Packaging templates

Ready-made "run it all the time" setups for Tiybai OmniButler. Pick your
platform. For containers instead, use the `Dockerfile` and
`docker-compose.example.yml` in the repository root. For the big picture
see `docs/install.md`.

Both setups assume the same install location; adjust if yours differs:

```bash
python3 -m venv ~/.venv-omnibutler
~/.venv-omnibutler/bin/pip install /path/to/tiybai-omnibutler   # or a clone: pip install .
```

## macOS — `com.tiybai.omnibutler.plist` (LaunchAgent)

Keeps `tob run --notify` alive in your login session. The `--notify` flag
turns on the native macOS dialog popups when a high-risk action waits for
your approval; it is macOS-only.

1. Edit the plist: replace every `YOURNAME` placeholder (the `tob` path,
   the log paths). Change `TOB_DRIVER` if you do not want `all`.
2. Optional approvals web page: add two more strings to
   `ProgramArguments` — `--approvals-port` and `8766` — and add an
   `OMNIBUTLER_APPROVALS_TOKEN` key (long random string) under
   `EnvironmentVariables`. The daemon refuses to serve the page without
   that token.
3. Install and load:

   ```bash
   mkdir -p ~/Library/Logs/omnibutler
   cp com.tiybai.omnibutler.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.tiybai.omnibutler.plist
   ```

4. Verify: `launchctl list | grep omnibutler`, logs at
   `~/Library/Logs/omnibutler/stdout.log`, and run `tob doctor` any time.

Uninstall: `launchctl unload ~/Library/LaunchAgents/com.tiybai.omnibutler.plist`
and delete the file.

## Linux — `omnibutler.service` (systemd user unit)

Keeps plain `tob run` alive in your user session (no root needed). It
deliberately does not use `--notify` — dialog popups are a macOS feature;
on Linux approve from the terminal (`tob pending`, `tob confirm <id>`) or
enable the approvals page as described in the unit file's comments.

1. Copy: `cp omnibutler.service ~/.config/systemd/user/`
2. `systemctl --user daemon-reload && systemctl --user enable --now omnibutler.service`
3. Logs: `journalctl --user -u omnibutler -f`. To keep it running while
   logged out: `loginctl enable-linger $USER`.

Uninstall: `systemctl --user disable --now omnibutler.service` and delete
the file.

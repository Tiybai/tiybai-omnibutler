# Installing Tiybai OmniButler

Three ways to install, from "just try it" to "runs in my home 24/7".
All of them end at the same place: the `tob` command. You need
Python 3.11+ for the first and third ways; Docker only for the second.

Whichever way you install, configuration lives in one place:
`~/.omnibutler/config.json` - copy `config.example.json` from the repo
and fill in your devices, or let `tob setup <brand>` walk you through
getting each device key. Afterwards, `tob doctor` tells you what is
working and what is not.

## Way 1: pip install from source (try it out)

```bash
git clone https://github.com/Tiybai/tiybai-omnibutler.git
cd tiybai-omnibutler
python3 -m venv .venv && source .venv/bin/activate
pip install .
```

That gives you the core: the mock "fake home", the Home Assistant
driver, scenes, and the MCP server. Add only the brands you own -
each extra pulls that vendor's library:

```bash
pip install ".[miio]"       # Xiaomi devices (local miIO protocol)
pip install ".[tuya]"      # Tuya devices (LAN control)
pip install ".[broadlink]" # Broadlink IR/RF hubs
pip install ".[midea]"     # Midea appliances
pip install ".[matter]"    # Matter controller client
pip install ".[zigbee]"    # Zigbee2MQTT
pip install ".[homeassistant]" # Home Assistant event stream (WebSocket)
pip install ".[all]"       # everything
```

First steps:

```bash
tob devices            # the built-in fake home, no hardware needed
tob simulate           # watch an "arrive home" scene run
tob doctor             # health check of your real setup
tob mcp                # serve MCP on stdio for your AI agent
```

(PyPI publication is prepared but not live yet - see
`docs/publishing.md`. For now, install from the clone as above.)

## Way 2: Docker (a box that is always on)

Best for a home server or NAS. The repo root has a `Dockerfile` and a
`docker-compose.example.yml` with every knob explained in comments.

```bash
cp docker-compose.example.yml docker-compose.yml
mkdir omnibutler-home     # your config.json goes in here
# edit docker-compose.yml: set which extras to build, replace the
# CHANGE-ME tokens, and put your config.json in omnibutler-home/
docker compose up -d --build
```

What you get: the daemon (`tob run`) running full-time, the approvals
web page on port 8766, and - if you enable the `mcp` profile - the MCP
HTTP endpoint on port 8765. The phone gateway listens on port 8767 -
run it inside the daemon with `tob run --gateway`, or as the separate
`gateway` service sketched in the compose file.

One warning that matters: the LAN drivers (Xiaomi, Tuya, Broadlink)
need to talk to devices on your home network. On Linux, enable
`network_mode: host` in the compose file (it is marked there), or the
containers can only reach Home Assistant and the internet, not your
bulbs and plugs.

## Way 3: always-on service on your own computer

Best for a Mac mini or a Linux box that is already on all day - this
is how the project is developed and dogfooded. Ready-made templates
live in `packaging/`, each with step-by-step comments:

- **macOS**: `packaging/com.tiybai.omnibutler.plist` - a LaunchAgent
  that starts `tob run --notify` at login and restarts it if it ever
  exits. `--notify` gives you a native dialog popup whenever a risky
  action (garage door, lock) is waiting for your approval.
- **Linux**: `packaging/omnibutler.service` - a systemd *user* unit
  (no root needed) that keeps `tob run` alive, restarts on failure,
  and logs to the journal.

The short version for both: install into a venv
(`python3 -m venv ~/.venv-omnibutler`, then `pip install .` from the
clone), copy the template into the folder your OS expects
(`~/Library/LaunchAgents/` or `~/.config/systemd/user/`), fix the
paths inside it, and load it. Full instructions are in
`packaging/README.md`.

## After installing

- Run `tob doctor` - it separates "not configured yet" (fine) from
  "configured but broken" (worth fixing).
- Approving risky actions: terminal (`tob pending` / `tob confirm`),
  the approvals web page (`tob approvals`, its own token), or the
  macOS dialog (Way 3). Approving is deliberately something only a
  human on the host can do - the AI connected over MCP cannot.

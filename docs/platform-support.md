# Platform support

The butler itself is plain Python (the only hard dependency is PyYAML),
so it runs anywhere Python 3.11+ runs. What differs per platform is
*how it stays running* and *how your phone takes part*. There is no
official mobile app, and none of the recipes below needs one.

## Windows, macOS, Linux - run it natively

Install as in [docs/install.md](install.md) (pip from source, or
Docker on Linux/macOS). For the always-on daemon (`tob run`), each OS
has its own way to keep it alive:

| OS | Keep-alive | Notes |
|---|---|---|
| macOS | launchd LaunchAgent - ready-made template `packaging/com.tiybai.omnibutler.plist` | Starts at login, restarts on exit |
| Linux | systemd user unit - ready-made template `packaging/omnibutler.service` | No root needed, logs to the journal |
| Windows | Task Scheduler: a logon-triggered task running `tob run` | No ready-made template; create the task in Task Scheduler (Start a program: your venv's `tob.exe`) |

Two Windows details worth knowing:

- **Stopping is not graceful there.** A service wrapper or
  `taskkill /F` ends the process without a signal Python can catch,
  so `daemon.lock` is left behind. That is expected: the next start
  sees the recorded pid is gone and takes the stale lock over (it
  says so on stderr). Only if that pid has meanwhile been recycled
  by an unrelated program would the start be refused - the error
  names the pid and the lock path, so you can check and remove the
  file yourself.
- **Secret files are protected by your user profile, not by POSIX
  permissions.** On macOS/Linux the config file (which holds device
  keys) is tightened to owner-only (`0600`). Windows has no `0600`;
  the equivalent protection is the ACL on your user directory. Keep
  the state directory (default `~/.omnibutler`) inside your own
  profile and not in a shared folder, and the keys stay yours.

## Android - Termux

Android cannot host the "real" install, but Termux gets you a
working butler on the phone itself - handy as a travelling gateway
or a spare node, not as the house's main hub (the phone sleeps,
roams and loses Wi-Fi; the hub should be a machine that is always
on).

```bash
pkg install python
pip install "tiybai-omnibutler[all]"   # or only the extras you need
tob run --gateway                      # or any other command
```

- Run `termux-wake-lock` (from the Termux:API / Termux:Boot add-ons)
  and exempt Termux from battery optimisation, or Android will kill
  the daemon in its sleep.
- Most optional dependencies are pure Python and install cleanly. If
  one of them ever needs compiling on your device, install only the
  extras for the drivers you actually use instead of `[all]`.

## iOS - client only

iOS does not allow an always-on background program like this, so the
butler itself cannot live on an iPhone. The phone still takes part -
as a *client* of a butler running on a machine at home. Two recipes,
both built from parts the project already ships:

### Recipe 1: Shortcuts geofence ("open the gate app, tap nothing")

1. On the host, start the gateway reachable from the LAN:
   `OMNIBUTLER_GATEWAY_TOKEN=<a long random string> tob run --gateway`
   (or a standalone `tob gateway --host 0.0.0.0`, port **8767**).
   Never expose this port to the public internet - home LAN or your
   own VPN only.
2. In Shortcuts, create a personal automation: *When I arrive* (or
   *leave*) at your home address -> action **Get Contents of URL**:
   - URL: `http://<host-LAN-IP>:8767/event`
   - Method: POST
   - Headers: `Authorization` = `Bearer <the gateway token>`
   - Request Body: JSON -
     `{"type": "geofence", "zone": "home", "transition": "enter"}`
     (use `"exit"` in the leaving automation)
3. Scenes with a geofence trigger (e.g. `arrive-home`) now fire from
   your phone's location, with no app in between.

### Recipe 2: the approvals page in Safari

1. On the host: `OMNIBUTLER_APPROVALS_TOKEN=<a long random string> tob approvals`
   (port **8766**; the token is separate from the gateway token).
2. On the phone, open `http://<host-LAN-IP>:8766/?token=<the token>`
   in Safari and add it to the Home Screen. Queued high-risk actions
   show up there for one-tap approve / reject.
3. The token rides in the URL, so it lands in the browser history -
   fine on your own phone and home network, not something to share
   or to use over a network you do not trust.

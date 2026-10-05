# Writing a third-party driver

OmniButler ships with drivers for the brands we could test ourselves.
Everything else is up to the community - and you do **not** need to fork
this repo to add a brand. Install your package next to OmniButler and
the bridge picks it up as a normal driver:

```bash
pip install tiybai-omnibutler omnibutler-mybrand
tob devices --driver mybrand
```

## The smallest possible package

```
omnibutler-mybrand/
├── pyproject.toml
└── omnibutler_mybrand/
    ├── __init__.py
    └── driver.py
```

The whole registration is one block in your `pyproject.toml`:

```toml
[project.entry-points."omnibutler.drivers"]
mybrand = "omnibutler_mybrand.driver:MyBrandDriver"
```

The entry-point name (`mybrand`) is the `--driver` value. That is all:
no registration call, no config edit. Your driver appears in
`--driver` choices, joins `tob onboard` scans, and shows up in
`tob doctor` with its load status.

## The driver protocol

Subclass `omnibutler.drivers.base.Driver` if you can; if not, matching
its shape is enough (duck typing - you are checked for the methods,
not the inheritance). You need:

- a `name` attribute equal to your entry-point name;
- `discover() -> list[Device]` - devices reachable right now;
- `list_devices() -> list[Device]` - devices you currently manage;
- `get_state(device_id) -> dict` - current property values;
- `set_property(device_id, property_name, value) -> dict`;
- `call_action(device_id, action, params) -> dict`.

Report device state in the shared capability vocabulary (`onoff`,
`target_temperature`, `brightness`, ...) - see how the built-in
drivers fill in `omnibutler.core.models.Device`, and the facts-only
files in `device-data/` for what a device description looks like.

Two construction rules:

- **Your constructor takes no required arguments.** The runtime builds
  the driver with `MyBrandDriver()`. Read your own settings inside
  `__init__` - environment variables, or a section of the shared
  `~/.omnibutler/config.json` via `omnibutler.config.load_config()`.
- **Import-time code stays cheap and side-effect free.** Your module
  gets imported when the bridge starts; connect to hardware lazily, on
  first use.

High-risk devices (locks, garage doors, gas valves) are not your call
to make: set the `risk` field honestly on the devices you report and
the confirmation queue handles the rest.

## How loading treats you

- A built-in name always wins. If your entry point is called `miio`,
  it is ignored with a warning on stderr.
- A package that fails to import, or whose class is missing methods,
  is skipped with the reason printed - it never takes the bridge down.
  `tob doctor` lists every installed entry point as loaded or not, and
  why not.
- Deliberately selected with `--driver mybrand`, a driver that fails
  to start says so and stops that command - no silent fallback.

## Testing your driver

Copy the pattern of the built-in driver tests (`tests/` in this repo):
build your driver against a fake transport - a stub object that answers
like the device or hub would - and assert on the requests you send and
the state you report. No real hardware in unit tests; that keeps them
fast and lets other people run them.

Then try it live, in this order:

```bash
tob doctor                        # your driver: loaded
tob devices --driver mybrand      # your devices, with state
tob onboard                       # what each found device still needs
```

If a model needs facts in `device-data/` format, contributions there
are welcome too - facts only, with sources, like the existing files.

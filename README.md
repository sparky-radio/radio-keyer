# radio-keyer

Turn a **PCsensor MK623Pro** 6-key USB/Bluetooth keypad into a CAT control pad for a ham radio on Linux.

Each key fires an action — send a CW message, change band/frequency/mode, or send a raw CAT string. Profiles (layers) let the 6 keys cover many more functions. A small local web UI shows radio status and edits key mappings.

## Features (planned)

- **Exclusive keypad capture** via evdev — key presses never leak into other apps.
- **Any radio** via Hamlib, plus **raw CAT passthrough** for radio-specific commands.
- **Two rig connection modes**
  - `rigctld` (TCP) — share the radio with WSJT-X, fldigi, loggers
  - Direct serial — app owns the port (`/dev/ttyUSB*` / `/dev/ttyACM*`)
- **CW memories**, per-macro method: radio keyer via CAT, radio's stored memories, or WinKeyer/DTR keying.
- **Band / frequency / mode** actions.
- **Profiles** — switch layers (e.g. CW contest / band / macros).
- **Local web UI** — live radio status, key grid with click-to-fire, profile switch, config editor (done; form-based editor planned).

## Hardware

| Item | Detail |
|---|---|
| Keypad | PCsensor MK623Pro, USB ID `3553:c111`, no knob |
| Device path | `/dev/input/by-id/usb-PCsensor_MK623Pro-event-kbd` |
| Default keycodes | Left→right, top→bottom: `F E D C B A` |
| Radio | Any Hamlib-supported rig over USB virtual serial |

## Requirements

- Linux, Python 3.12+
- Hamlib (`rigctl`, `rigctld`)
- Access to the keypad's input device (see Permissions)
- User in the `dialout` group for direct serial access

## Installation

### 1. System packages

```bash
sudo apt install python3-venv python3-dev build-essential libhamlib-utils
```

`python3-dev` and `build-essential` are needed only if pip has to build `evdev` from source.

### 2. Python virtual environment

From the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .            # installs the `radio-keyer` command
```

Verify:

```bash
python -c "import evdev, serial, fastapi, uvicorn, yaml; print('ok')"
```

Later sessions only need `source .venv/bin/activate`. Run `deactivate` to leave the venv.

### 3. Python dependencies

Listed in [requirements.txt](requirements.txt):

| Package | Purpose |
|---|---|
| `evdev` | Read and exclusively grab the keypad |
| `pyserial` | Direct serial CAT, WinKeyer, DTR keying |
| `fastapi`, `uvicorn` | Local web UI |
| `pyyaml` | Profiles and key-mapping config |

Hamlib is used through `rigctld`/`rigctl` (system package), not pip.

### 4. Development / tests (optional)

With the venv active:

```bash
pip install -r requirements-dev.txt   # adds pytest
python -m pytest                      # use the venv's pytest
```

Check it's the venv's copy with `python -m pytest --version`. A system-wide `pytest` may not see the project's packages. The suite includes an integration test that starts a real `rigctld -m 1` (dummy rig) on a free localhost port; it's skipped if Hamlib isn't installed.

### 5. Permissions

The keypad's event devices are `root:input 0660`. Grant access to just this device with a udev rule:

```
# /etc/udev/rules.d/99-mk623pro.rules
SUBSYSTEM=="input", ATTRS{idVendor}=="3553", ATTRS{idProduct}=="c111", MODE="0660", GROUP="plugdev", TAG+="uaccess"
```

```bash
sudo udevadm control --reload && sudo udevadm trigger
# then unplug/replug the keypad
```

## Usage

Activate the venv first: `source .venv/bin/activate`

### Key test (no radio needed)

```bash
radio-keyer keytest
```

Grabs the keypad and prints each press. Ctrl+C on the laptop keyboard to quit.

```
key 1  short  KEY_F    142 ms
key 3  long   KEY_D    600 ms
```

| Option | Default | Meaning |
|---|---|---|
| `--long-ms N` | 600 | Long-press threshold (fires while still held) |
| `--device PATH` | MK623Pro by-id path | Other evdev device |
| `--no-grab` | off | Don't grab exclusively (keys also type normally) |
| `-v` | off | Debug logging (shows unmapped keys) |

Unplugging the keypad is handled: it reconnects automatically on replug.

### Check the config

```bash
radio-keyer check            # or: -c path/to/other.yaml
```

Validates `config.yaml` and lists every profile and key. Errors name the exact location, e.g. `profiles.cw.keys.4.short: unknown action 'cww'`.

### Dry run (keypad + config, simulated rig)

```bash
radio-keyer dryrun
```

Press keys to see each action and the rig commands it would send. The simulated rig starts at 14.030 MHz CW and tracks frequency/mode, so `band: up/down` works. Profile switches take effect. The web UI runs too (see below), so you can try everything from the browser without a radio.

### Rig status

```bash
radio-keyer status
```

Connects to the rig per `config.yaml` and prints frequency and mode.

### Run

```bash
radio-keyer run
```

Keypad → actions → rig. If rigctld isn't up yet, it logs a warning and retries on each key press. Failed actions are logged; the rest of that press's series is skipped.

### Web UI

`run` and `dryrun` also serve a local web UI at **http://localhost:8073/** (`--no-web` to skip it).

- **Rig**: live frequency/mode (polled every second while a browser is open), connection indicators for rig and keypad.
- **Keys**: the active profile's 6 keys (short + long), with keys inherited from `global` tagged. Click a key to fire it, exactly like the keypad.
- **Profiles**: click to switch.
- **Activity**: the same log as the terminal.
- **Config**: edit `config.yaml` in place. *Check* validates; *Save & apply* (Ctrl+S) validates, writes the file (previous version → `config.yaml.bak`) and applies it immediately: mappings, profiles, callsign and rig connection. Changes to `keypad:` or `web:` need a restart (a banner says so). Saving from the UI keeps your comments, because it saves the text exactly as typed.

Keep it on localhost: the UI can rewrite the config and send raw CAT. It refuses requests whose Host isn't localhost (DNS rebinding) and cross-site POSTs/WebSockets (CSRF). Set `web.host` only if you need LAN access; there's no login.

### Rig connection: rigctld (shared)

Start rigctld for your radio (find the model with `rigctl -l`), e.g. IC-7300:

```bash
rigctld -m 3073 -r /dev/ttyUSB0 -s 19200 -T 127.0.0.1 -t 4532
```

No radio? Use Hamlib's dummy rig:

```bash
rigctld -m 1 -T 127.0.0.1 -t 4532
```

WSJT-X, fldigi and loggers can connect to the same rigctld (Hamlib NET rigctl, `127.0.0.1:4532`).

### Rig connection: serial (app owns the port)

With `rig.backend: serial` the app starts its own private `rigctld` on a free localhost port (it needs the Hamlib `rigctld` binary on `PATH`) and stops it on exit:

```yaml
rig:
  backend: serial
  serial:
    model: 3073          # `rigctl -l`
    port: /dev/ttyUSB0   # better: /dev/serial/by-id/...
    baud: 19200          # omit for Hamlib's default
```

rigctld is started on the first rig command and restarted if it dies. Other programs can't share the radio in this mode; use the `rigctld` backend for that. The app checks that the port exists and that you can open it before starting rigctld. With a real port but the wrong baud or radio, Hamlib 4.5 doesn't exit; commands just time out. rigctld's `rig_open: error` line shows up in the log (run with `-v` for its full output).

### CW on Yaesu radios (FT-891): `cw_keyer: yaesu`

Hamlib 4.5.5's `send_morse` doesn't work on the FT-891: it sends `KYT;`, the radio answers `?;`, and you get `RPRT -14` ("communication bus collision"). Use the radio's own memory keyer instead:

```yaml
rig:
  cw_keyer: yaesu
  cw_scratch_memory: 5     # overwritten by every `cw:` text
```

- `cw: "CQ TEST {call}"` writes the text to text memory 5 (`KM5...};`), reads it back, then plays it (`KY5;`). Text is uppercased, and is limited to 49 characters plus the closing `}`. `#` sends the radio's contest number. If the read-back doesn't match, nothing is sent.
- `cw_memory: 1`–`5` plays text memories 1–5 (`KY1;`–`KY5;`). `cw_memory: 6`–`10` plays paddle-recorded message memories 1–5 (`KY6;`–`KYA;`).
- `cw_stop` isn't possible: the CAT protocol has no command to stop memory playback. Touch the paddle to stop it.
- On the radio: mode CW and the keyer on. Turn BK-IN on to transmit; with it off you hear sidetone only.
- The FT-891 factory text in memory 5 (`R 5NN K}`) gets overwritten.

### Known limitations (Hamlib 4.5 / rigctld)

- **Raw ASCII CAT can't contain spaces** (rigctld splits arguments on whitespace). Use `cw:` for CW text instead of e.g. Kenwood `KY` commands.
- **Raw ASCII CAT is fire-and-forget**: no reply is read.
- **`cw_memory`** works only with `rig.cw_keyer: yaesu` (Hamlib has no generic command). For other radios, use `cat`/`cat_hex` with the radio's memory-keyer command.
- **`cw ... via: winkeyer | dtr`** isn't implemented yet.
- `stop_morse` depends on the radio's Hamlib backend.
- A `delay_ms` step blocks other key presses until it finishes (keypad and web UI presses share one queue).

## Configuration

Everything lives in `config.yaml` (not in git; your personal copy). Start from [config.example.yaml](config.example.yaml), whose header comment is the full reference:

```bash
cp config.example.yaml config.yaml
```

- **Profiles**: named layers of key mappings. The first one is active at startup.
- **global**: fallback used when the active profile doesn't define a key/press.
- **Each key** has `short` and/or `long`. A bare action means `short`.
- **Each press** runs one action, or a list of actions in order.
- **web**: `enabled` (default true), `host` (default `127.0.0.1`), `port` (default `8073`).

| Action | Example |
|---|---|
| CW text | `cw: "CQ TEST {call}"` (+ `via: cat \| winkeyer \| dtr`) |
| Radio CW memory | `cw_memory: 1` (Yaesu: 1–5 text, 6–10 paddle; needs `rig.cw_keyer: yaesu`) |
| Stop CW | `cw_stop: true` |
| Band | `band: 20m` / `up` / `down` |
| Frequency (Hz) | `freq: 14074000` (+ `mode: PKTUSB`) |
| Mode | `mode: CW` |
| Raw CAT (ASCII) | `cat: "FA;"` |
| Raw CAT (binary, Icom) | `cat_hex: "FE FE 94 E0 03 FD"` |
| Switch profile | `profile: next` / `prev` / `<name>` |
| Pause | `delay_ms: 300` |

Series example:

```yaml
6:
  long:
    - {freq: 14030000, mode: CW}
    - delay_ms: 300
    - cw: "CQ CQ {call} {call} K"
```

## Layout

```
config.example.yaml   example configuration (copy to config.yaml, which git ignores)
radio_keyer/
  __main__.py         CLI (keytest, check, dryrun, status, run; run/dryrun serve the web UI)
  keypad.py           evdev reader, exclusive grab, short/long press, reconnect
  config.py           YAML loader + validation, profile lookup / global fallback
  actions.py          action types
  bands.py            band table, band up/down
  rig.py              Rig interface, rigctld client, simulated rig
  runner.py           executes bindings on a Rig, tracks active profile
  web.py              FastAPI web UI: state/press/profile/config API + WebSocket
  static/             web UI page (index.html, app.js, style.css; no build step)
tests/                pytest suite (incl. fake rigctld + real dummy-rig test)
```

## Project status

See [PROGRESS.md](PROGRESS.md).

## License

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 Paul E. Rogers (K5AQ).

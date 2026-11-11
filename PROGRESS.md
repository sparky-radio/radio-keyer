# Progress

## 2026-11-11

### Done
- Requirements gathered (see README → Features).
- Keypad identified: MK623Pro `3553:c111`, kbd at `/dev/input/by-id/usb-PCsensor_MK623Pro-event-kbd`.
- Keycodes captured with `evtest`: positions 1–6 (L→R, T→B) = `F E D C B A`. No knob.
- Python venv created at `.venv/` (Python 3.12.3).
- README.md and PROGRESS.md created.
- requirements.txt created (evdev, pyserial, fastapi, uvicorn, pyyaml); README Installation section added.
- pip install done in `.venv`: evdev 2.0.0, pyserial 3.5, fastapi 0.162.4, uvicorn 0.54.0, PyYAML 6.0.3.
- udev rule active: keypad is `root:plugdev` with ACL; readable from Python without sudo.
- **Keypad module** (`radio_keyer/keypad.py`): exclusive grab, short/long press (timer-based, 600 ms default, long fires while held), auto-reconnect, async iterator API.
- `radio-keyer keytest` CLI. Verified: opens + grabs device. `PressDetector` logic tested (short, long, dup-down, stray-up, reset).
- **Config** (`config.py`, `actions.py`, `bands.py`, `config.yaml`):
  - Profiles (first = startup) + `global` fallback per key/press kind.
  - Each press = one action or a list (series).
  - Actions: cw (+via cat/winkeyer/dtr, `{call}` placeholder), cw_memory, cw_stop, band (name/up/down), freq (+mode), mode, cat, cat_hex, profile (next/prev/name), delay_ms.
  - Validation errors report exact path.
- CLI: `check` (validate + list) and `dryrun` (keypad + config, prints actions, follows profile switches).
- Tests in `tests/` (31 cases) — pass via stand-in harness; pytest in `requirements-dev.txt`, not yet installed.

### Findings
- Keys send plain letters → exclusive evdev grab is mandatory.
- Key repeat on (delay 250 ms) → long-press detection is feasible.
- User is in `dialout` but not `input`; keypad not yet readable without udev rule.
- Hamlib (`rigctl`, `rigctld`) already installed.

### Next
- [x] User ran `keytest` on real hardware: all 6 key positions correct.
- [ ] Confirm long-press feel and unplug/replug recovery on hardware.
- [x] Callsign set in config.yaml; `dryrun` verified on hardware.
- [x] pytest installed into `.venv`; 48 tests pass (2026-11-11).
- [ ] User: start `rigctld -m 1`, try `status` and `run` with the keypad.
- [ ] Web UI (status, mapping editor, rig setup).
- [ ] `serial` backend: app spawns its own rigctld on a private localhost port (reuses RigctldRig).
- [ ] cw_memory per radio family; WinKeyer / DTR CW.
- [ ] Verify raw CAT terminators (`0`, `icom`) on a real radio.

## 2026-11-11 (session 2): rig backend

### Done
- `rig.py`: `Rig` ABC; `RigctldRig` (asyncio TCP, extended `+` protocol, per-command timeout, drop+lazy reconnect, input validation against command injection); `SimulatedRig` for dry runs.
- `runner.py`: executes bindings, series abort on first failure, profile tracking, band up/down from the rig's current freq.
- CLI: `status`, `run`; `dryrun` now uses Runner + SimulatedRig.
- Tests: fake rigctld server, runner tests, real `rigctld -m 1` integration test. 48/48 pass (stand-in harness).
- `status` verified against a live dummy rigctld.

### Findings (Hamlib 4.5.5, probed against `rigctld -m 1`)
- Unknown command → rigctld sends **no reply** → timeouts are required; drop connection after timeout.
- Args are whitespace-split: `send_raw` takes one token; extra tokens are run as **new commands** (desync). Binary format: `send_raw icom 0xFE;0xFE;...`.
- `send_morse` takes the rest of the line (spaces OK).
- `set_mode <mode> -1` keeps passband. Dummy rig accepts invalid modes (we validate in config).
- `stop_morse` / `get_ptt` return -11 on the dummy rig.
- No generic CW-memory command in Hamlib.
- pytest was not found in `.venv` (user's earlier run likely used another pytest).
- [ ] Connect radio and confirm serial port + Hamlib model.

### Decisions
- Global fallback is per key **and** press kind (profile defines 1-short only → 1-long comes from global).
- Radio CW memory playback is its own action (`cw_memory: N`), not `cw ... via: memory`.
- Band jumps use a fixed per-band default freq (CW segment) for now; band-stack recall may come later.

### Open questions
- Which radio(s) to test against first?

## 2026-11-11 (session 3): naming

### Done
- Project/CLI name is now `radio-keyer` (matches free-sat convention). `pyproject.toml` gains `[project]` + `[project.scripts] radio-keyer`; package dir stays `radio_keyer/` (Python import name).
- README, config.yaml: commands now `radio-keyer <cmd>`; install step `pip install -e .` added.

### Next
- [x] `pip install -e .` done; `radio-keyer` command works (MIT license metadata picked up).

## 2026-11-11 (session 4): web UI

### Done
- `web.py` (FastAPI + uvicorn in the same asyncio loop as the keypad) and `static/` (index.html, app.js, style.css; vanilla JS, no build step).
  - Live rig status over WebSocket (polls 1 s only while a browser is connected); rig/keypad/UI indicators.
  - Key grid for the active profile (short + long, `global` fallback tagged); click fires the press through the same Runner as the keypad.
  - Profile switcher; activity log (mirrors `radio_keyer` INFO logs).
  - Config editor: check / save & apply. Writes atomically, keeps `config.yaml.bak`, hot-reloads mappings/profiles/callsign/rig; `keypad:`/`web:` changes flag "restart required". An unsupported rig backend is refused before anything is written.
  - Security: binds 127.0.0.1 by default; Host must be localhost (DNS rebinding); cross-origin POST/PUT/WebSocket refused (CSRF).
- Config: new `web:` section (enabled/host/port, default 127.0.0.1:8073). `check` shows the URL.
- Runner: presses serialized with a lock (keypad + web); `reload()`, `set_profile()`, `replace_rig()`.
- CLI: `run` and `dryrun` serve the UI; `--no-web` to skip. Port in use → clean error. `dryrun` rig lines now go through logging (so they show in the UI).
- Tests: `tests/test_web.py` (18 cases) call the ASGI app in-process (no httpx needed). 66/66 pass.
- Smoke test: dryrun starts the UI, port clash gives a clean error, Ctrl+C exits cleanly.

### Next
- [ ] User: `radio-keyer dryrun`, open http://localhost:8073/, try keys, profiles, config save.
- [ ] Form-based mapping editor (per-key action builder) on top of the YAML editor.
- [ ] `serial` backend: app spawns its own rigctld on a private localhost port.
- [ ] Still pending: `rigctld -m 1` + `run` with the keypad; long-press feel; replug recovery.

## 2026-11-11 (session 5): example config

### Done
- User's tuned config copied to `config.example.yaml` (committed). `config.yaml` is now git-ignored (personal copy); README says `cp config.example.yaml config.yaml`.
- Shipped-config test validates `config.example.yaml`. Missing default `config.yaml` → CLI hints the `cp` command.
- Web UI verified by user over SSH tunnel (`ssh -L 8073:localhost:8073 ...`).

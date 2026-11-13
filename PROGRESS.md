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
- Initial commit `6778dbf` (MIT license added); pushed by user.

### Open question at pause
- Next: form-based per-key editor in the web UI, or `serial` backend (app spawns private rigctld)?

## 2026-11-12 (session 6): serial backend

### Done
- `rigctld_proc.py`: `SpawnedRigctldRig` (subclass of `RigctldRig`) runs a private `rigctld -m <model> -r <port> [-s baud] -T 127.0.0.1 -t <free port>`. Starts lazily on the first command and restarts transparently if the child died. `close()` sends SIGTERM, then SIGKILL after 3 s. rigctld stderr: lines containing "error" are logged as WARNING, the rest as DEBUG; the last error line goes into startup failure messages.
- `make_rig` builds it for `backend: serial`; the web UI's hot-swap of the rig works too (the old child is stopped).
- Hamlib 4.5 finding: with a wrong or missing serial port rigctld does not exit. It listens, but commands hang (we hit the 3 s timeout) and it logs `rig_open: error`.
- Tests: `tests/test_rigctld_proc.py` (argv, missing binary, child exits at startup, real `rigctld -m 1` lifecycle + restart after SIGKILL). 70/70 pass.
- Smoke: `status` and `run` (Ctrl+C) with `backend: serial, model: 1`; no rigctld left behind.
- README: new "Rig connection: serial" section.
- Port check before spawning rigctld: "serial port … not found" / "no permission … dialout" (skipped for the dummy rig and host:port devices). Prompted by an FT-891 test where `port:` still held the `<serial>` placeholder and rigctld just hung. 72/72 tests pass.

### Next
- [x] User verified `backend: serial` with the dummy rig (model 1). [ ] Real radio still pending (use a `/dev/serial/by-id/...` path).
- [ ] Form-based per-key editor in the web UI.

### FT-891 bring-up (2026-11-12)
- `backend: serial` works: model 1036, CP2105 `if00-port0` (Enhanced), 38400 baud. `status`, `band`/`mode` keys OK.
- **`cw:` fails: Hamlib 4.5.5 `send_morse` is broken for the FT-891.** Its newcat backend sends `KYT;` (for "TEST"), the radio answers `?;`, and Hamlib retries 4× → RPRT -14 "bus collision". Reproduced with plain `rigctl ... b TEST`, so it's not our code.
- Options: raw-CAT keyer memory (KM write + KY play; needs the FT-891 CAT manual checked), DTR/RTS keying on the Standard port (`if01-port0`), or a newer Hamlib.

## 2026-11-12 (session 6, cont.): Yaesu memory keyer

### Done
- Read `docs/FT-891_CAT_OM_ENG_1909-C.pdf` and `docs/FT-891_Advance_Manual_ENG_1806-F.pdf` (user-supplied). `KY P1;` plays memory 1-5 (text) / 6-9,A (paddle message). `KM P1 text};` writes a text memory (≤50 chars incl. `}`, spaces OK, `#` = contest number), and `KM P1;` reads it back. All five memories default to TEXT (menus 04-07..11).
- Tested rigctld on the dummy rig: `send_raw` cuts the text at the first space, but `send_cmd` keeps the rest of the line. → `Rig.send_cmd()` added (rigctld `send_cmd`; reply parsing accepts both a labelled and a bare answer; the dummy rig never answers send_cmd, so the real format is still unverified).
- `radio_keyer/yaesu_keyer.py`; config `rig.cw_keyer: hamlib|yaesu` and `rig.cw_scratch_memory` (default 5, user's choice).
  - `cw:` → `send_cmd KM5<TEXT>};KM5;` (write + read-back), then `send_raw KY5;`. A read-back mismatch → error, nothing transmitted.
  - `cw_memory: 1-10` → `KY1;`..`KY5;`, `KY6;`..`KYA;` (6-10 = paddle memories, user's choice).
  - `cw_stop` → unsupported (no CAT stop command).
- `check` prints the CW keyer. README section + example config. 92/92 tests pass.

### Next
- [ ] User, on the FT-891 (dummy load): `cw:` key, `cw_memory: 1` key, a paddle memory (6-10). Confirm send_cmd's reply format (run with `-v`) and that read-back works.

## 2026-11-12 (session 7): FT-891 keyer test

### Findings
- `check` OK on ham-laptop (model 1036, if00-port0, 38400, Yaesu keyer, memory 5).
- `cw:` text fails: `send_cmd KM5…};KM5;` hits the app's 3 s timeout. With `rigctl -vvvvv`, the radio stores the text and answers `KM5TEST};`, but Hamlib 4.5.5 `send_cmd` waits for a `\n\r` terminator (`send_cmd_term=0x0a 0x0d`), times out and drops the answer. `rigctl` has `-t/--send-cmd-term`; `rigctld` doesn't (its `-t` is the port).
- `cw_memory: 1` / `6` (`KY1;`/`KY6;` via `send_raw`) returned no error. Whether they sounded is not confirmed yet.
- FT-891 caps: timeout 2000 ms, retry 3, post_write_delay 50 ms.
- Side issue (not the app): SSH drops on ham-laptop came from a flapping switch port; moving the cable fixed it.

## 2026-11-13 (session 8): hex send_raw keyer write

### Findings (rigctl interactive on the FT-891)
- `rigctl ... '\send_raw' ...` on the command line doesn't work in 4.5.5 (`Command '' not found`), so use the interactive prompt.
- Hex write `\send_raw 0 0x4B;0x4D;0x35;…;0x7D;0x3B` stores the text, and `\send_raw ; KM5;` reads back `KM5CQ TEST};`.
- `KY5;`/`KY1;` answered `?;` even in CW with `KR1;`. The cause was KEYER TYPE `EX04010;` (OFF). The user set `EX04013;` (ELEKEY-B).

### Done
- `Rig.send_raw(..., terminator=None)` overrides the reply terminator. `send_cmd` was removed.
- `yaesu_keyer.send_text`: the `KM` write goes hex via `send_raw 0`, the read-back via `send_raw ; KM P1;` (must match exactly, else nothing is sent), then `KY`.
- `yaesu_keyer.keyer_warning` reads EX0401. `status` prints a warning when KEYER TYPE is OFF.
- `SimulatedRig` answers a `;` read with the latest matching write, so dry runs still play `cw:` text.
- README Yaesu section updated. 97 tests pass.

- Later the same day: `KY1`/`KY5`/`KY6` still answer `?;` with KEYER TYPE ELEKEY-B (`EX04013;`), REC SETTING enabled (`EX05111;`), CW MEMORY 5 = TEXT (`EX04110;`), BK-IN on (`BI1;`) and mode CW (`MD03;`). So CAT `KY` is unusable on this radio, maybe because of its firmware (not confirmed). Panel playback (REC SETTING → CH5) not tested yet.

### DTR keying (`cw_keyer: dtr`)
- New `radio_keyer/dtr_keyer.py`: Morse table and PARIS timing. A worker thread keys DTR or RTS against absolute deadlines, spinning for the last 2 ms of each wait. Text is queued, so a press returns at once. `stop()` uses a generation counter, so text queued before a stop never plays.
- Config: `rig.dtr: {port, line: dtr|rts, wpm: 5-50}`. `port` is required for `cw_keyer: dtr`. `cw: ... via: dtr` works with any `cw_keyer`.
- Runner: the keyer is created lazily, rebuilt when `rig.dtr` changes on reload, and closed at exit. `cw_stop` aborts DTR text. Dry runs use `NullKeyLine`.
- README section, example config and `check` output updated. 107 tests pass.

### Next
- [x] FT-891 DTR keying works on the air (dummy load): PC KEYING = DTR (`EX07123;`), BK-IN on, `rig.dtr.port` = `if01-port0`. `cw:` and `cw_stop` keys OK.
- [x] Config check rejects `rig.dtr.port` equal to `rig.serial.port` (the user first set `if00`).
- [ ] Optionally: panel playback of CH5, and a firmware check for `KY`.
- [ ] Form-based per-key editor in the web UI.

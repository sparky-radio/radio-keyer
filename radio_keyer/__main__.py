"""Entry point: radio-keyer <command> (or python -m radio_keyer <command>)"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from radio_keyer import yaesu_keyer
from radio_keyer.actions import Binding
from radio_keyer.config import (
    Config,
    ConfigError,
    CwKeyer,
    KeypadConfig,
    Profile,
    RigBackend,
    load_config,
)
from radio_keyer.keypad import (
    DeviceInfo,
    Keypad,
    KeyLearner,
    find_device_by_name,
    key_name,
    learn_keys,
    list_devices,
    wait_for_any_key,
)
from radio_keyer.rig import Rig, RigConnectionError, RigctldRig, RigError, SimulatedRig
from radio_keyer.rigctld_proc import SpawnedRigctldRig
from radio_keyer.dtr_keyer import DtrKeyer, NullKeyLine
from radio_keyer.runner import KeyerFactory, Runner, serial_dtr_keyer
from radio_keyer.web import HubLogHandler, WebHub, bind_socket, serve, url_for

DEFAULT_CONFIG = Path("config.yaml")

log = logging.getLogger("radio_keyer")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radio-keyer", description="USB keypad → ham radio CAT")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    kt = sub.add_parser("keytest", help="print keypad presses (no radio)")
    kt.add_argument("--list", action="store_true", help="list input devices and exit")
    kt.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="read the keypad: section from this file, if it exists (default %(default)s)",
    )
    _device_args(kt)
    kt.add_argument("--long-ms", type=int, help="long-press threshold in ms (default: config)")
    kt.add_argument("--no-grab", action="store_true", help="don't grab exclusively")

    lr = sub.add_parser("learn", help="press each key in turn; prints a keypad: config block")
    _device_args(lr)
    lr.add_argument("--no-grab", action="store_true", help="don't grab exclusively")

    for name, help_text in (
        ("check", "validate the config and print profiles"),
        ("dryrun", "keypad + config + simulated rig (no radio)"),
        ("status", "connect to the rig and print frequency/mode"),
        ("run", "keypad + config + rig: the real thing"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG, help="config file")
        if name in ("dryrun", "run"):
            p.add_argument("--no-web", action="store_true", help="don't start the web UI")
    return parser


def _device_args(p: argparse.ArgumentParser) -> None:
    group = p.add_mutually_exclusive_group()
    group.add_argument("--device", metavar="PATH", help="evdev device path")
    group.add_argument("--name", help="evdev device name (see keytest --list)")


# --- keytest --------------------------------------------------------------


def keytest_config(config_path: Path, device: str | None, name: str | None) -> KeypadConfig:
    """The keypad: section of `config_path` (defaults if absent), with CLI overrides."""
    kc = load_config(config_path).keypad if config_path.exists() else KeypadConfig()
    if device is not None:
        kc = replace(kc, device=device, device_name=None)
    elif name is not None:
        kc = replace(kc, device_name=name)
    return kc


async def keytest(kc: KeypadConfig, grab: bool) -> None:
    keypad = Keypad(
        kc.device,
        kc.keymap,
        device_name=kc.device_name,
        long_press_s=kc.long_press_ms / 1000,
        grab=grab,
        on_unmapped=lambda code: print(f"unmapped  {key_name(code)}  (not in keypad.keys)", flush=True),
    )
    print(f"Keypad {keypad.describe_device()}, {kc.num_keys} keys. Ctrl+C to quit.", flush=True)
    async with keypad:
        async for press in keypad:
            print(
                f"key {press.position}  {press.kind.value:<5}  "
                f"{key_name(press.keycode):<6}  {press.held_s * 1000:4.0f} ms",
                flush=True,
            )


def print_devices() -> None:
    devices, unreadable = list_devices()
    if not devices:
        print("No readable input devices.")
    for d in devices:
        keys = "" if d.has_keys else "  (no keys)"
        print(f"{d.path:<20} {d.name!r}{keys}")
        for link in d.by_id:
            print(f"{'':<20}   {link}")
    if unreadable:
        print(
            f"\n{len(unreadable)} more input devices are not readable "
            "(no permission: your keyboard, or a keypad without a udev rule)."
        )


# --- learn ----------------------------------------------------------------


async def learn(device: str | None, name: str | None, grab: bool) -> int:
    learner = KeyLearner()
    if name is not None:
        path = find_device_by_name(name)
    elif device is not None:
        path = device
    else:
        print("Press any key on the keypad to pick it (the first press may type a letter)...")
        candidates = [d.path for d in list_devices()[0] if d.has_keys]
        path, first = await wait_for_any_key(candidates)
        learner.key_down(first)
    info = _device_info(path)
    print(f"Keypad: {path} ({info.name if info else '?'})")
    if learner.codes:
        print(f"key 1 = {learner.names[0]}")
    print(
        "Press each key once, in order (left to right, top to bottom).\n"
        "Press the first key again to finish.",
        flush=True,
    )

    def report(result: str, code: int) -> None:
        if result == "added":
            print(f"key {len(learner.codes)} = {key_name(code)}", flush=True)
        elif result == "duplicate":
            print(f"  {key_name(code)} is already key {learner.codes.index(code) + 1}; skipped")

    await learn_keys(path, learner, report, grab=grab)
    print()
    print(keypad_block(learner.names, path, info, name))
    return 0


def _device_info(path: str) -> DeviceInfo | None:
    real = os.path.realpath(path)
    return next((d for d in list_devices()[0] if d.path == real), None)


def keypad_block(
    names: list[str], path: str, info: DeviceInfo | None, name: str | None
) -> str:
    """A ready-to-paste keypad: section."""
    links = list(info.by_id) if info else []
    by_id = [p for p in links if p.endswith("-kbd")] or links
    if name is None and path.startswith("/dev/input/by-id/"):
        device = path
    elif name is None and by_id:
        device = by_id[0]
    else:
        device = None
    dev_name = name or (info.name if info else None)
    lines = ["# Paste into config.yaml (replace the existing keypad: section):", "keypad:"]
    if device is not None:
        lines.append(f"  device: {device}")
        if dev_name:
            lines.append(f"  # device: {{name: {json.dumps(dev_name)}}}   # by name (Bluetooth/2.4 GHz)")
    elif dev_name:
        lines.append(f"  device: {{name: {json.dumps(dev_name)}}}")
    else:
        lines.append(f"  device: {path}")
    lines.append(f"  keys: [{', '.join(names)}]")
    lines.append(f"  columns: {min(len(names), 3)}               # key grid width in the web UI")
    return "\n".join(lines)


# --- check ----------------------------------------------------------------


def describe_binding(binding: Binding) -> str:
    return " → ".join(a.describe() for a in binding)


def print_profile(profile: Profile, title: str) -> None:
    print(f"\n[{title}]")
    for pos in sorted(profile.keys):
        for kind, binding in profile.keys[pos].items():
            print(f"  {pos} {kind.value:<5}  {describe_binding(binding)}")


def describe_rig(config: Config) -> str:
    rig = config.rig
    if rig.backend is RigBackend.RIGCTLD:
        return f"rigctld → {rig.rigctld.host}:{rig.rigctld.port}"
    s = rig.serial
    return f"serial → private rigctld, model {s.model} on {s.port} @ {s.baud or 'default'} baud"


def check(config: Config) -> None:
    print(f"Station:  {config.station.callsign or '(no callsign)'}")
    print(f"Rig:      {describe_rig(config)}")
    rc = config.rig
    if rc.cw_keyer is CwKeyer.YAESU:
        print(f"CW:       Yaesu memory keyer (cw: text uses memory {rc.cw_scratch_memory})")
    elif rc.cw_keyer is CwKeyer.DTR:
        d = rc.dtr
        print(f"CW:       {d.line.value.upper()} keying on {d.port} at {d.wpm} WPM")
    kc = config.keypad
    print(
        f"Keypad:   {kc.describe_device()}, {kc.num_keys} keys "
        f"(long press {kc.long_press_ms} ms)"
    )
    w = config.web
    print(f"Web UI:   {url_for(w.host, w.port) if w.enabled else 'disabled'}")
    if config.presets:
        print("\n[presets]")
        for preset in config.presets.values():
            print(f"  {preset.describe()}")
    print_profile(config.global_profile, "global (fallback)")
    for i, p in enumerate(config.profiles):
        print_profile(p, f"{p.name}: {p.label}" + ("  ← startup" if i == 0 else ""))
    print("\nConfig OK")


# --- rig / run ------------------------------------------------------------


class UsageError(Exception):
    pass


def make_rig(config: Config) -> Rig:
    rig = config.rig
    if rig.backend is RigBackend.RIGCTLD:
        return RigctldRig(rig.rigctld.host, rig.rigctld.port)
    s = rig.serial
    if s.model is None:  # config parser already enforces this
        raise UsageError("rig.serial.model is required for the 'serial' backend")
    return SpawnedRigctldRig(model=s.model, device=s.port, baud=s.baud)


async def status(config: Config) -> int:
    rig = make_rig(config)
    warning: str | None = None
    try:
        st = await rig.status()
        if config.rig.cw_keyer is CwKeyer.YAESU:
            warning = await yaesu_keyer.keyer_warning(rig)
    except (RigConnectionError, RigError) as exc:
        print(f"Rig error: {exc}", file=sys.stderr)
        return 1
    finally:
        await rig.close()
    print(f"{describe_rig(config)}")
    print(f"Frequency: {st.freq_hz / 1e6:.6f} MHz")
    print(f"Mode:      {st.mode} ({st.passband_hz} Hz)")
    if warning:
        print(f"Warning:   {warning}", file=sys.stderr)
    return 0


def web_rig_factory(config: Config) -> Rig:
    """make_rig for config saved from the web UI: unsupported → ConfigError (nothing saved)."""
    try:
        return make_rig(config)
    except UsageError as exc:
        raise ConfigError(str(exc)) from None


async def _keypad_loop(keypad: Keypad, runner: Runner) -> None:
    async for press in keypad:
        await runner.handle_press(press)


async def run_app(
    config: Config,
    config_path: Path,
    rig: Rig,
    *,
    web: bool,
    rig_factory: Callable[[Config], Rig] | None,
    keyer_factory: KeyerFactory = serial_dtr_keyer,
) -> None:
    """Keypad (+ web UI) until Ctrl+C."""
    runner = Runner(config, rig, keyer_factory=keyer_factory)
    kc = config.keypad
    keypad = Keypad(
        kc.device,
        kc.keymap,
        device_name=kc.device_name,
        long_press_s=kc.long_press_ms / 1000,
        grab=kc.grab,
    )
    log.info("Profile: %s (%s)", runner.active_profile, config.profile(runner.active_profile).label)

    hub: WebHub | None = None
    handler: HubLogHandler | None = None
    sock: socket.socket | None = None
    wc = config.web
    if web and wc.enabled:
        try:
            sock = bind_socket(wc.host, wc.port)
        except OSError as exc:
            await rig.close()
            raise UsageError(
                f"web UI cannot listen on {wc.host}:{wc.port} ({exc.strerror or exc}); "
                "change web.port or use --no-web"
            ) from None
        hub = WebHub(
            runner,
            config_path,
            describe_rig=describe_rig,
            rig_factory=rig_factory,
            keypad_connected=lambda: keypad.connected,
        )
        handler = HubLogHandler(hub)
        log.addHandler(handler)
        log.info("Web UI: %s", url_for(wc.host, wc.port))

    tasks: list[asyncio.Task[None]] = []
    try:
        async with keypad:
            tasks.append(asyncio.create_task(_keypad_loop(keypad, runner), name="keypad-loop"))
            if hub is not None and sock is not None:
                tasks.append(asyncio.create_task(serve(hub, sock, wc.host), name="web"))
                tasks.append(asyncio.create_task(hub.poll_loop(), name="rig-poll"))
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # surface a crash
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if handler is not None:
            log.removeHandler(handler)
        if sock is not None:
            sock.close()
        runner.close_keyer()
        await runner.rig.close()


async def run(config: Config, config_path: Path, *, web: bool) -> None:
    rig = make_rig(config)
    try:
        st = await rig.status()
        log.info("Rig: %s, %.6f MHz %s", describe_rig(config), st.freq_hz / 1e6, st.mode)
    except (RigConnectionError, RigError) as exc:
        log.warning("Rig not reachable yet (%s); will retry on each key press", exc)
    await run_app(config, config_path, rig, web=web, rig_factory=web_rig_factory)


async def dryrun(config: Config, config_path: Path, *, web: bool) -> None:
    rig = SimulatedRig(lambda msg: log.info("rig ← %s", msg))
    log.info("Dry run: simulated rig at %.6f MHz %s. Ctrl+C to quit.", rig.freq_hz / 1e6, rig.mode)
    await run_app(
        config,
        config_path,
        rig,
        web=web,
        rig_factory=None,
        keyer_factory=lambda dc: DtrKeyer(NullKeyLine, dc.wpm),
    )


# --- main -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        if args.command == "keytest":
            if args.list:
                print_devices()
                return 0
            if args.long_ms is not None and args.long_ms <= 0:
                print("--long-ms must be > 0", file=sys.stderr)
                return 2
            try:
                kc = keytest_config(args.config, args.device, args.name)
            except ConfigError as exc:
                print(f"Config error: {exc}", file=sys.stderr)
                return 1
            if args.long_ms is not None:
                kc = replace(kc, long_press_ms=args.long_ms)
            asyncio.run(keytest(kc, not args.no_grab))
            return 0
        if args.command == "learn":
            try:
                return asyncio.run(learn(args.device, args.name, not args.no_grab))
            except OSError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

        try:
            config = load_config(args.config)
        except ConfigError as exc:
            print(f"Config error: {exc}", file=sys.stderr)
            if not args.config.exists() and args.config == DEFAULT_CONFIG:
                print("Create it with: cp config.example.yaml config.yaml", file=sys.stderr)
            return 1

        match args.command:
            case "check":
                check(config)
            case "dryrun":
                asyncio.run(dryrun(config, args.config, web=not args.no_web))
            case "status":
                return asyncio.run(status(config))
            case "run":
                asyncio.run(run(config, args.config, web=not args.no_web))
    except UsageError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

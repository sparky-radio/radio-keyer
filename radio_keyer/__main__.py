"""Entry point: radio-keyer <command> (or python -m radio_keyer <command>)"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
from collections.abc import Callable
from pathlib import Path

from radio_keyer.actions import Binding
from radio_keyer.config import Config, ConfigError, CwKeyer, Profile, RigBackend, load_config
from radio_keyer.keypad import DEFAULT_DEVICE_PATH, DEFAULT_LONG_PRESS_S, Keypad, key_name
from radio_keyer.rig import Rig, RigConnectionError, RigctldRig, RigError, SimulatedRig
from radio_keyer.rigctld_proc import SpawnedRigctldRig
from radio_keyer.runner import Runner
from radio_keyer.web import HubLogHandler, WebHub, bind_socket, serve, url_for

DEFAULT_CONFIG = Path("config.yaml")

log = logging.getLogger("radio_keyer")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radio-keyer", description="MK623Pro → ham radio CAT")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    kt = sub.add_parser("keytest", help="print keypad presses (no config, no radio)")
    kt.add_argument("--device", default=DEFAULT_DEVICE_PATH, help="evdev device path")
    kt.add_argument(
        "--long-ms",
        type=int,
        default=int(DEFAULT_LONG_PRESS_S * 1000),
        help="long-press threshold in ms (default %(default)s)",
    )
    kt.add_argument("--no-grab", action="store_true", help="don't grab exclusively")

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


# --- keytest --------------------------------------------------------------


async def keytest(device: str, long_ms: int, grab: bool) -> None:
    async with Keypad(device, long_press_s=long_ms / 1000, grab=grab) as keypad:
        print("Press keypad keys (Ctrl+C to quit)...", flush=True)
        async for press in keypad:
            print(
                f"key {press.position}  {press.kind.value:<5}  "
                f"{key_name(press.keycode):<6}  {press.held_s * 1000:4.0f} ms",
                flush=True,
            )


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
    print(f"Keypad:   {config.keypad.device} (long press {config.keypad.long_press_ms} ms)")
    w = config.web
    print(f"Web UI:   {url_for(w.host, w.port) if w.enabled else 'disabled'}")
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
    try:
        st = await rig.status()
    except (RigConnectionError, RigError) as exc:
        print(f"Rig error: {exc}", file=sys.stderr)
        return 1
    finally:
        await rig.close()
    print(f"{describe_rig(config)}")
    print(f"Frequency: {st.freq_hz / 1e6:.6f} MHz")
    print(f"Mode:      {st.mode} ({st.passband_hz} Hz)")
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
) -> None:
    """Keypad (+ web UI) until Ctrl+C."""
    runner = Runner(config, rig)
    kc = config.keypad
    keypad = Keypad(kc.device, long_press_s=kc.long_press_ms / 1000, grab=kc.grab)
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
    await run_app(config, config_path, rig, web=web, rig_factory=None)


# --- main -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        if args.command == "keytest":
            if args.long_ms <= 0:
                print("--long-ms must be > 0", file=sys.stderr)
                return 2
            asyncio.run(keytest(args.device, args.long_ms, not args.no_grab))
            return 0

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

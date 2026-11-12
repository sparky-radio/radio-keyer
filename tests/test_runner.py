import asyncio

from radio_keyer.config import parse_config_text
from radio_keyer.keypad import KeyPress, PressKind
from radio_keyer.rig import RigError, SimulatedRig
from radio_keyer.runner import Runner

CONFIG = """
station: {callsign: K5AQ}
global:
  keys:
    1: {long: {profile: next}}
profiles:
  main:
    keys:
      1: {cw: "CQ {call}"}
      2: {band: up}
      3: [{freq: 7074000, mode: PKTUSB}, {delay_ms: 1}, {cw: "TEST"}]
      4: {cw_memory: 1}
      5: [{cw_stop: true}, {cw: "NEVER"}]
      6: {cat_hex: "FE FE 94 E0 03 FD"}
  other:
    keys:
      1: {band: 40m}
"""


def press(pos: int, kind: PressKind = PressKind.SHORT) -> KeyPress:
    return KeyPress(position=pos, kind=kind, keycode=0, held_s=0.1)


class FailingStopRig(SimulatedRig):
    async def stop_morse(self) -> None:
        raise RigError("stop_morse", 11)


def run(presses: list[KeyPress], rig: SimulatedRig | None = None) -> tuple[list[str], Runner, list[bool]]:
    out: list[str] = []
    rig = rig or SimulatedRig(out.append, freq_hz=14_030_000)
    if rig is not None:
        rig._out = out.append
    runner = Runner(parse_config_text(CONFIG), rig)

    async def main() -> list[bool]:
        return [await runner.handle_press(p) for p in presses]

    results = asyncio.run(main())
    return out, runner, results


def test_cw_with_callsign() -> None:
    out, _, ok = run([press(1)])
    assert out == ['send_morse "CQ K5AQ"'] and ok == [True]


def test_band_up_from_current_freq() -> None:
    out, _, _ = run([press(2)])
    assert out == ["set_freq 18.080000 MHz"]


def test_series_in_order() -> None:
    out, _, _ = run([press(3)])
    assert out == ["set_freq 7.074000 MHz", "set_mode PKTUSB", 'send_morse "TEST"']


def test_unsupported_and_unassigned_return_false() -> None:
    _, _, ok = run([press(4), press(2, PressKind.LONG)])
    assert ok == [False, False]


def test_failure_skips_rest_of_series() -> None:
    out, _, ok = run([press(5)], FailingStopRig(lambda _m: None))
    assert out == [] and ok == [False]


def test_profile_switch_via_global_fallback() -> None:
    out, runner, _ = run([press(1, PressKind.LONG), press(1)])
    assert runner.active_profile == "other"
    assert out == ["set_freq 7.030000 MHz"]


def test_cat_hex() -> None:
    out, _, _ = run([press(6)])
    assert out == ["send_raw icom 0xFE;0xFE;0x94;0xE0;0x03;0xFD"]


YAESU_CONFIG = CONFIG.replace(
    "station: {callsign: K5AQ}\n", "station: {callsign: K5AQ}\nrig: {cw_keyer: yaesu}\n"
)


def run_yaesu(presses: list[KeyPress]) -> tuple[list[str], list[bool]]:
    out: list[str] = []
    runner = Runner(parse_config_text(YAESU_CONFIG), SimulatedRig(out.append))

    async def main() -> list[bool]:
        return [await runner.handle_press(p) for p in presses]

    return out, asyncio.run(main())


def test_yaesu_cw_text_uses_scratch_memory_5() -> None:
    out, ok = run_yaesu([press(1)])
    assert out == ["send_cmd KM5CQ K5AQ};KM5;", "send_raw 0 KY5;"] and ok == [True]


def test_yaesu_cw_memory_plays_ky() -> None:
    out, ok = run_yaesu([press(4)])
    assert out == ["send_raw 0 KY1;"] and ok == [True]


def test_yaesu_cw_stop_is_unsupported() -> None:
    out, ok = run_yaesu([press(5)])
    assert out == [] and ok == [False]

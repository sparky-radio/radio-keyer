"""FT-891 memory channel / scan actions against a small CAT model of the radio."""

from __future__ import annotations

import asyncio
import logging

import pytest

from radio_keyer import ft891
from radio_keyer.actions import MemoryAction, ScanAction
from radio_keyer.config import ConfigError, parse_config_text
from radio_keyer.keypad import KeyPress, PressKind
from radio_keyer.rig import SimulatedRig, raw_args
from radio_keyer.runner import Runner


class Ft891Cat(SimulatedRig):
    """Answers MC/CH/MA/SC/IF like the radio (per the CAT manual)."""

    def __init__(self, channels: dict[int, int], *, area: str = "1") -> None:
        self.sent: list[str] = []
        super().__init__(self.sent.append)
        self.channels = channels  # channel → Hz (programmed channels only)
        self.channel = min(channels)
        self.area = area  # IF P7: 0 VFO, 1 memory
        self.scan_code = "0"

    async def send_raw(self, data: bytes, *, binary: bool, terminator: str | None = None) -> str:
        raw_args(data, binary=binary, terminator=terminator)  # same validation as the real thing
        cmd = data.decode("ascii")
        self.sent.append(cmd)
        if cmd.startswith("MC") and cmd != "MC;":
            n = int(cmd[2:5])
            if n in self.channels:  # an empty channel is refused
                self.channel = n
        elif cmd in ("CH0;", "CH1;"):
            order = sorted(self.channels)
            i = order.index(self.channel) + (1 if cmd == "CH0;" else -1)
            self.channel = order[i % len(order)]
        elif cmd.startswith("SC") and cmd != "SC;":
            self.scan_code = cmd[2]
        if terminator != ";":
            return ""
        if cmd == "MC;":
            return f"MC{self.channel:03d};"
        if cmd == "SC;":
            return f"SC{self.scan_code};"
        if cmd == "IF;":
            hz = self.channels[self.channel]
            return f"IF{self.channel:03d}{hz:09d}+000000{3}{self.area}000;"
        return ""


def test_parse_info() -> None:
    info = ft891.parse_info("IF012014074000+000000C10000;")
    assert info == ft891.RadioInfo(channel="012", freq_hz=14_074_000, area="memory")
    assert not info.in_vfo
    assert ft891.parse_info("IF001007030000+00000030000;").in_vfo  # type: ignore[union-attr]
    assert ft891.parse_info("?;") is None and ft891.parse_info("") is None


def test_recall_memory() -> None:
    rig = Ft891Cat({1: 7_030_000, 12: 14_074_000})
    info = asyncio.run(ft891.recall_memory(rig, 12))
    assert rig.sent[:3] == ["MC012;", "MC;", "IF;"]
    assert info is not None and info.freq_hz == 14_074_000 and info.channel == "012"


def test_recall_empty_channel_fails() -> None:
    rig = Ft891Cat({1: 7_030_000})
    with pytest.raises(ft891.MemoryChannelError, match=r"memory 5 not selected .*MC001;"):
        asyncio.run(ft891.recall_memory(rig, 5))
    with pytest.raises(ValueError):
        asyncio.run(ft891.recall_memory(rig, 100))


def test_step_and_to_vfo() -> None:
    rig = Ft891Cat({1: 7_030_000, 2: 10_120_000, 3: 14_030_000})
    info = asyncio.run(ft891.step_memory(rig, up=True))
    assert info is not None and info.channel == "002"
    info = asyncio.run(ft891.step_memory(rig, up=False))
    assert info is not None and info.channel == "001"
    asyncio.run(ft891.memory_to_vfo(rig))
    assert rig.sent == ["CH0;", "IF;", "CH1;", "IF;", "MA;"]


def test_scan() -> None:
    rig = Ft891Cat({1: 7_030_000})
    asyncio.run(ft891.scan(rig, "down"))
    assert rig.scan_code == "2" and rig.sent == ["SC2;", "SC;"]
    asyncio.run(ft891.scan(rig, "stop"))
    assert rig.scan_code == "0"


def test_scan_not_taken() -> None:
    class Deaf(Ft891Cat):
        async def send_raw(self, data: bytes, *, binary: bool, terminator: str | None = None) -> str:
            return "?;" if terminator == ";" else ""

    with pytest.raises(ft891.MemoryChannelError, match=r"scan up: radio answered \?;"):
        asyncio.run(ft891.scan(Deaf({1: 1}), "up"))


# --- config ---------------------------------------------------------------


def test_parse_memory_and_scan_actions() -> None:
    cfg = parse_config_text(
        """
profiles:
  mem:
    keys:
      1: {memory: 12}
      2: {memory: "7"}
      3: {memory: UP}
      4: {memory: to_vfo}
      5: {scan: down}
      6: [{scan: off}, {scan: stop}]
"""
    )
    b = lambda pos: cfg.binding("mem", pos, PressKind.SHORT)  # noqa: E731
    assert b(1) == (MemoryAction(12),) and b(2) == (MemoryAction(7),)
    assert b(3) == (MemoryAction("up"),) and b(4) == (MemoryAction("to_vfo"),)
    assert b(5) == (ScanAction("down"),)
    assert b(6) == (ScanAction("stop"), ScanAction("stop"))
    assert MemoryAction(12).describe() == "memory 12"
    assert MemoryAction("to_vfo").describe() == "memory → VFO-A"


@pytest.mark.parametrize(
    ("action", "message"),
    [
        ("{memory: 0}", "memory channel must be 1-99"),
        ("{memory: 100}", "memory channel must be 1-99"),
        ("{memory: sideways}", "expected a channel 1-99 or one of: up, down, to_vfo"),
        ("{memory: true}", "memory"),
        ("{scan: left}", "'left' is not one of: up, down, stop"),
    ],
)
def test_bad_memory_actions(action: str, message: str) -> None:
    with pytest.raises(ConfigError) as exc_info:
        parse_config_text(f"profiles: {{a: {{keys: {{1: {action}}}}}}}")
    assert message in str(exc_info.value)


# --- runner ---------------------------------------------------------------


def _press(runner: Runner, pos: int) -> bool:
    return asyncio.run(runner.handle_press(KeyPress(pos, PressKind.SHORT, 0, 0.1)))


CONFIG = """
profiles:
  mem:
    keys:
      1: {memory: 2}
      2: [{memory: 9}, {cw_stop: true}]
      3: {scan: up}
"""


def test_runner_memory(caplog: pytest.LogCaptureFixture) -> None:
    rig = Ft891Cat({1: 7_030_000, 2: 14_074_000}, area="0")
    runner = Runner(parse_config_text(CONFIG), rig)
    with caplog.at_level(logging.INFO):
        assert _press(runner, 1)
    assert "channel 002, 14.074000 MHz, VFO mode" in caplog.text
    assert "press V/M" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert not _press(runner, 2)  # empty channel 9: series stops
    assert "memory 9 not selected" in caplog.text and "skipping 1 remaining" in caplog.text
    assert _press(runner, 3) and rig.scan_code == "1"

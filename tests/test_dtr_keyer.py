import threading
import time

import pytest

from radio_keyer.config import ConfigError, CwKeyer, parse_config_text
from radio_keyer.dtr_keyer import DtrKeyer, KeyerError, KeyLineName, elements, normalize

WPM = 50
UNIT = 1.2 / WPM


def units(text: str) -> list[tuple[bool, int]]:
    return [(down, round(s / UNIT)) for down, s in elements(text, WPM)]


def test_elements_paris_timing() -> None:
    # E (.) gap T (-) word gap
    assert units("E T") == [(True, 1), (False, 7), (True, 3), (False, 7)]
    assert units("en") == [(True, 1), (False, 3), (True, 3), (False, 1), (True, 1), (False, 7)]
    # PARIS + word gap = 50 units
    assert sum(n for _, n in units("PARIS")) == 50


def test_normalize_and_limits() -> None:
    assert normalize("  cq  de k5aq/p ") == "CQ DE K5AQ/P"
    for bad in ["", "CQ;", "CQ#", "é"]:
        with pytest.raises(ValueError):
            normalize(bad)
    for wpm in (4, 51):
        with pytest.raises(ValueError):
            elements("E", wpm)


class RecordingLine:
    def __init__(self) -> None:
        self.states: list[bool] = []
        self.closed = False
        self.first_down = threading.Event()

    def set(self, down: bool) -> None:
        self.states.append(down)
        if down:
            self.first_down.set()

    def close(self) -> None:
        self.closed = True


def make(wpm: int = WPM) -> tuple[DtrKeyer, RecordingLine, list[int]]:
    line = RecordingLine()
    opened: list[int] = []

    def open_line() -> RecordingLine:
        opened.append(1)
        return line

    return DtrKeyer(open_line, wpm), line, opened


def test_send_keys_line_and_releases() -> None:
    keyer, line, opened = make()
    keyer.send("ee")
    assert keyer.wait_idle(2)
    downs = [s for s in line.states if s]
    assert len(downs) == 2 and line.states[-1] is False
    keyer.send("E")
    assert keyer.wait_idle(2)
    assert opened == [1]  # opened once, kept open
    keyer.close()
    assert line.closed and not keyer.busy


def test_timing_is_close_to_schedule() -> None:
    keyer, _, _ = make()
    start = time.perf_counter()
    keyer.send("PARIS")  # 50 units = 1.2 s at 50 WPM
    assert keyer.wait_idle(3)
    assert abs(time.perf_counter() - start - 1.2) < 0.05
    keyer.close()


def test_stop_aborts_current_and_queued() -> None:
    keyer, line, _ = make(wpm=5)
    keyer.send("PARIS")
    keyer.send("PARIS")
    assert line.first_down.wait(1)
    keyer.stop()
    assert keyer.wait_idle(1)
    assert line.states[-1] is False
    assert sum(line.states) <= 1  # stopped during the first dot
    keyer.send("E")  # usable again after stop
    assert keyer.wait_idle(2)
    assert sum(line.states) == 2
    keyer.close()


def test_open_failure_raises_and_bad_text_does_not_open() -> None:
    def fail() -> RecordingLine:
        raise KeyerError("no port")

    keyer = DtrKeyer(fail, WPM)
    with pytest.raises(KeyerError):
        keyer.send("E")
    keyer2, _, opened = make()
    with pytest.raises(ValueError):
        keyer2.send("CQ;")
    assert opened == []


BASE = """
profiles:
  main:
    keys:
      1: {cw: "CQ"}
"""


def test_config_dtr() -> None:
    cfg = parse_config_text(
        "rig: {cw_keyer: dtr, dtr: {port: /dev/ttyUSB1, line: rts, wpm: 30}}\n" + BASE
    )
    assert cfg.rig.cw_keyer is CwKeyer.DTR
    assert (cfg.rig.dtr.port, cfg.rig.dtr.line, cfg.rig.dtr.wpm) == (
        "/dev/ttyUSB1",
        KeyLineName.RTS,
        30,
    )
    assert parse_config_text(BASE).rig.dtr.line is KeyLineName.DTR
    for bad in [
        "rig: {cw_keyer: dtr}\n",
        "rig: {dtr: {port: x, line: cts}}\n",
        "rig: {dtr: {port: x, wpm: 99}}\n",
        "rig: {dtr: {port: x, speed: 20}}\n",
    ]:
        with pytest.raises(ConfigError):
            parse_config_text(bad + BASE)

import asyncio

import pytest

from radio_keyer.rig import SimulatedRig
from radio_keyer.yaesu_keyer import (
    MAX_TEXT,
    keyer_warning,
    km_read,
    km_write,
    ky_command,
    normalize_text,
    play_memory,
    send_text,
)


@pytest.mark.parametrize(
    ("slot", "cmd"), [(1, b"KY1;"), (5, b"KY5;"), (6, b"KY6;"), (9, b"KY9;"), (10, b"KYA;")]
)
def test_ky_mapping(slot: int, cmd: bytes) -> None:
    assert ky_command(slot) == cmd


@pytest.mark.parametrize("slot", [0, 11])
def test_ky_out_of_range(slot: int) -> None:
    with pytest.raises(ValueError):
        ky_command(slot)


def test_normalize() -> None:
    assert normalize_text("  cq  test k5aq ") == "CQ TEST K5AQ"
    assert normalize_text("599 #") == "599 #"
    assert len(normalize_text("E" * MAX_TEXT)) == MAX_TEXT
    for bad in ["", "CQ;", "CQ}", "CQé", "E" * (MAX_TEXT + 1)]:
        with pytest.raises(ValueError):
            normalize_text(bad)


def test_km_commands() -> None:
    assert km_write(5, "cq test") == b"KM5CQ TEST};"
    assert km_read(5) == b"KM5;"
    with pytest.raises(ValueError):
        km_write(6, "CQ")


class AnsweringRig(SimulatedRig):
    """Answers every ';'-terminated read with `answer`."""

    def __init__(self, answer: str) -> None:
        self.sent: list[str] = []
        super().__init__(self.sent.append)
        self.answer = answer

    async def send_raw(self, data: bytes, *, binary: bool, terminator: str | None = None) -> str:
        await super().send_raw(data, binary=binary, terminator=terminator)
        return self.answer if terminator == ";" else ""


KM5_TEST_HEX = "0x4B;0x4D;0x35;0x54;0x45;0x53;0x54;0x7D;0x3B"


def test_send_text_writes_hex_verifies_then_plays() -> None:
    rig = AnsweringRig("KM5TEST};")
    asyncio.run(send_text(rig, "test", scratch=5))
    assert rig.sent == [f"send_raw 0 {KM5_TEST_HEX}", "send_raw ; KM5;", "send_raw 0 KY5;"]


@pytest.mark.parametrize("answer", ["", "?;", "KM5R 5NN K};"])
def test_bad_readback_does_not_transmit(answer: str) -> None:
    rig = AnsweringRig(answer)
    with pytest.raises(ValueError, match="not sent"):
        asyncio.run(send_text(rig, "TEST", scratch=5))
    assert rig.sent == [f"send_raw 0 {KM5_TEST_HEX}", "send_raw ; KM5;"]


def test_simulated_rig_echoes_write_on_read() -> None:
    out: list[str] = []
    asyncio.run(send_text(SimulatedRig(out.append), "TEST", scratch=5))
    assert out[-1] == "send_raw 0 KY5;"


def test_play_memory() -> None:
    rig = AnsweringRig("")
    asyncio.run(play_memory(rig, 7))
    assert rig.sent == ["send_raw 0 KY7;"]


@pytest.mark.parametrize(
    ("answer", "warns"), [("EX04013;", False), ("EX04010;", True), ("?;", True), ("", True)]
)
def test_keyer_warning(answer: str, warns: bool) -> None:
    rig = AnsweringRig(answer)
    assert (asyncio.run(keyer_warning(rig)) is not None) is warns
    assert rig.sent == ["send_raw ; EX0401;"]

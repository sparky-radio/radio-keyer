import asyncio

import pytest

from radio_keyer.rig import SimulatedRig
from radio_keyer.yaesu_keyer import (
    MAX_TEXT,
    km_write_and_read,
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


def test_km_command() -> None:
    assert km_write_and_read(5, "cq test") == "KM5CQ TEST};KM5;"
    with pytest.raises(ValueError):
        km_write_and_read(6, "CQ")


class AnsweringRig(SimulatedRig):
    def __init__(self, answer: str) -> None:
        self.sent: list[str] = []
        super().__init__(self.sent.append)
        self.answer = answer

    async def send_cmd(self, text: str) -> str:
        await super().send_cmd(text)
        return self.answer


def test_send_text_writes_then_plays() -> None:
    rig = AnsweringRig("KM5CQ K5AQ};")
    asyncio.run(send_text(rig, "cq k5aq", scratch=5))
    assert rig.sent == ["send_cmd KM5CQ K5AQ};KM5;", "send_raw 0 KY5;"]


def test_send_text_without_readback_still_plays() -> None:
    rig = AnsweringRig("")
    asyncio.run(send_text(rig, "TEST", scratch=3))
    assert rig.sent == ["send_cmd KM3TEST};KM3;", "send_raw 0 KY3;"]


def test_readback_mismatch_does_not_transmit() -> None:
    rig = AnsweringRig("KM5R 5NN K};")
    with pytest.raises(ValueError, match="not sent"):
        asyncio.run(send_text(rig, "TEST", scratch=5))
    assert rig.sent == ["send_cmd KM5TEST};KM5;"]


def test_play_memory() -> None:
    rig = AnsweringRig("")
    asyncio.run(play_memory(rig, 7))
    assert rig.sent == ["send_raw 0 KY7;"]

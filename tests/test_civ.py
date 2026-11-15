import asyncio

import pytest

from radio_keyer import civ
from radio_keyer.rig import SimulatedRig


def test_frame() -> None:
    assert civ.frame(0x94, b"\x03") == bytes.fromhex("FEFE94E003FD")
    with pytest.raises(ValueError):
        civ.frame(0x94, b"")
    with pytest.raises(ValueError):
        civ.frame(0x94, b"\x17\xfd")
    with pytest.raises(ValueError):
        civ.frame(0x01, b"\x03")


@pytest.mark.parametrize(
    "text",
    ["0xfe 0xfe 0xe0 0x94 0xfb 0xfd", "FE FE E0 94 FB FD", "0xFE;0xFE;0xE0;0x94;0xFB;0xFD", " fe,fe,e0,94,fb,fd "],
)
def test_answer_bytes_formats(text: str) -> None:
    assert civ.answer_bytes(text) == bytes.fromhex("FEFEE094FBFD")


def test_answer_bytes_rejects_text() -> None:
    with pytest.raises(civ.CivError, match="format"):
        civ.answer_bytes("OK")
    assert civ.answer_bytes("") == b""


def test_split_frames() -> None:
    data = bytes.fromhex("00 FEFEFE E094FBFD 12 FEFE0094 0300 FD FEFE")
    frames = civ.split_frames(data)
    assert frames == [civ.Frame(0xE0, 0x94, b"\xfb"), civ.Frame(0x00, 0x94, b"\x03\x00")]
    assert frames[0].is_ok and not frames[1].is_ok


def test_pick_reply_skips_echo_and_broadcast() -> None:
    echo = civ.Frame(0x94, 0xE0, b"\x03")
    bcast = civ.Frame(0x00, 0x94, b"\x00\x12")
    reply = civ.Frame(0xE0, 0x94, b"\x03\x12")
    assert civ.pick_reply([echo, bcast, reply], 0x94) is reply


@pytest.mark.parametrize(
    ("frames", "message"),
    [
        ([civ.Frame(0x94, 0xE0, b"\x03")], "Echo Back"),
        ([civ.Frame(0x00, 0x94, b"\x00")], "Transceive"),
        ([civ.Frame(0xE0, 0x98, b"\xfb")], "civ_address"),
        ([], "no CI-V reply"),
    ],
)
def test_pick_reply_errors(frames: list[civ.Frame], message: str) -> None:
    with pytest.raises(civ.CivError, match=message):
        civ.pick_reply(frames, 0x94)


def test_bcd() -> None:
    assert civ.bcd(255, 4) == b"\x02\x55"
    assert civ.bcd(8, 2) == b"\x08"
    assert civ.from_bcd(b"\x02\x55") == 255
    with pytest.raises(ValueError):
        civ.bcd(100, 2)
    with pytest.raises(civ.CivError):
        civ.from_bcd(b"\x0a")


class ReplyRig(SimulatedRig):
    def __init__(self, answer: str) -> None:
        self.sent: list[str] = []
        super().__init__(self.sent.append)
        self.answer = answer

    async def send_raw(self, data: bytes, *, binary: bool, terminator: str | None = None) -> str:
        await super().send_raw(data, binary=binary, terminator=terminator)
        return self.answer


def test_write_ok_and_ng() -> None:
    rig = ReplyRig("FE FE E0 94 FB FD")
    asyncio.run(civ.write(rig, 0x94, b"\x16\x47\x01"))
    assert rig.sent == ["send_raw icom 0xFE;0xFE;0x94;0xE0;0x16;0x47;0x01;0xFD"]
    rig.answer = "FE FE E0 94 FA FD"
    with pytest.raises(civ.CivError, match="NG"):
        asyncio.run(civ.write(rig, 0x94, b"\x16\x47\x01"))


def test_read_returns_data() -> None:
    rig = ReplyRig("FE FE E0 94 16 47 02 FD")
    assert asyncio.run(civ.read(rig, 0x94, b"\x16\x47")) == b"\x02"
    rig.answer = "FE FE E0 94 FB FD"
    with pytest.raises(civ.CivError, match="unexpected answer"):
        asyncio.run(civ.read(rig, 0x94, b"\x16\x47"))


def test_simulated_rig_answers_civ() -> None:
    rig = SimulatedRig(lambda _: None)

    async def main() -> bytes:
        await civ.write(rig, 0x94, b"\x1a\x02\x01CQ TEST")
        await civ.write(rig, 0x94, b"\x17E E E")
        await civ.write(rig, 0x94, b"\x17E E")  # a 17 that prefixes an earlier one is still a write
        return await civ.read(rig, 0x94, b"\x1a\x02\x01")

    assert asyncio.run(main()) == b"CQ TEST"

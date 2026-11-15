import asyncio

import pytest

from radio_keyer import civ, icom_keyer
from radio_keyer.rig import SimulatedRig


def test_normalize() -> None:
    assert icom_keyer.normalize_text("  cq  test k5aq ") == "CQ TEST K5AQ"
    assert icom_keyer.normalize_text("^AR 5nn?") == "^AR 5NN?"
    for bad in ["", "   ", "CQ #", "CQ;", "CQé", "CQ *"]:
        with pytest.raises(ValueError):
            icom_keyer.normalize_text(bad)


def test_chunks_short_text_is_one_piece() -> None:
    assert icom_keyer.chunks("CQ TEST K5AQ") == ["CQ TEST K5AQ"]


def test_chunks_split_at_word_gaps_and_keep_the_gap() -> None:
    text = "CQ CQ CQ DE K5AQ K5AQ K5AQ TEST"  # 31 characters; 30 ends inside TEST
    pieces = icom_keyer.chunks(text)
    assert pieces == ["CQ CQ CQ DE K5AQ K5AQ K5AQ ", "TEST"]
    assert "".join(pieces) == text
    assert all(len(p) <= icom_keyer.MAX_CHUNK for p in pieces)


def test_chunks_full_piece_moves_gap_to_next() -> None:
    text = "A" * 10 + " " + "B" * 19 + " C"  # first piece exactly 30
    pieces = icom_keyer.chunks(text)
    assert pieces == ["A" * 10 + " " + "B" * 19, " C"]
    assert "".join(pieces) == text


def test_chunks_word_too_long() -> None:
    with pytest.raises(ValueError, match="longer"):
        icom_keyer.chunks("E" * 30)


def test_send_commands() -> None:
    assert icom_keyer.send_commands("cq k5aq") == [b"\x17CQ K5AQ"]


def test_memory_read_command() -> None:
    assert icom_keyer.memory_read_command(1) == b"\x1a\x02\x01"
    assert icom_keyer.memory_read_command(8) == b"\x1a\x02\x08"
    for bad in [0, 9]:
        with pytest.raises(ValueError):
            icom_keyer.memory_read_command(bad)


class Rig(SimulatedRig):
    def __init__(self) -> None:
        self.sent: list[str] = []
        super().__init__(self.sent.append)


def test_send_text_and_stop() -> None:
    rig = Rig()
    asyncio.run(icom_keyer.send_text(rig, 0x94, "CQ"))
    asyncio.run(icom_keyer.stop(rig, 0x94))
    assert rig.sent == [
        "send_raw icom 0xFE;0xFE;0x94;0xE0;0x17;0x43;0x51;0xFD",
        "send_raw icom 0xFE;0xFE;0x94;0xE0;0x17;0xFF;0xFD",
    ]


def store_memory(rig: SimulatedRig, slot: int, text: str) -> None:
    asyncio.run(civ.write(rig, 0x94, icom_keyer.memory_read_command(slot) + text.encode()))


def test_play_memory_reads_then_sends() -> None:
    rig = Rig()
    store_memory(rig, 3, "CFM TU ")
    rig.sent.clear()
    asyncio.run(icom_keyer.play_memory(rig, 0x94, 3))
    assert rig.sent == [
        "send_raw icom 0xFE;0xFE;0x94;0xE0;0x1A;0x02;0x03;0xFD",
        "send_raw icom 0xFE;0xFE;0x94;0xE0;0x17;0x43;0x46;0x4D;0x20;0x54;0x55;0xFD",
    ]


def test_play_memory_refuses_contest_number() -> None:
    rig = Rig()
    store_memory(rig, 2, "UR 5NN *")
    with pytest.raises(ValueError, match="contest number"):
        asyncio.run(icom_keyer.play_memory(rig, 0x94, 2))


def test_play_memory_empty() -> None:
    rig = Rig()
    store_memory(rig, 4, "   ")
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(icom_keyer.play_memory(rig, 0x94, 4))


@pytest.mark.parametrize(
    ("state", "warns"), [("00", "break-in is OFF"), ("01", None), ("02", None)]
)
def test_keyer_warning(state: str, warns: str | None) -> None:
    rig = Rig()
    asyncio.run(civ.write(rig, 0x94, bytes.fromhex("1647" + state)))
    warning = asyncio.run(icom_keyer.keyer_warning(rig, 0x94))
    assert warning == warns or (warns is not None and warning is not None and warns in warning)


def test_keyer_warning_unreadable() -> None:
    warning = asyncio.run(icom_keyer.keyer_warning(Rig(), 0x94))  # simulated rig answers OK
    assert warning is not None and "could not read" in warning

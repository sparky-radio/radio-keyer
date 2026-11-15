"""Icom CW over CI-V (IC-7300 manual, section 19).

- `17 <ASCII>` sends up to 30 characters as CW; `17 FF` stops. The radio only
  transmits it with TX or break-in on (footnote *2 of the command table).
- `1A 02 <ch>` reads keyer memory M1-M8 (ch 01-08) → `1A 02 <ch> <text>`.
  CI-V has no command to *play* a memory, so `cw_memory N` reads M`N` and
  sends its text with `17`. A `*` in a memory inserts the radio's contest
  number, which the app can't reproduce, so such a memory is refused.
- `16 47` reads break-in: 00 = OFF, 01 = semi, 02 = full.

Unverified on a radio: whether a second `17` sent while the first is still
keying is queued or cuts in (texts over 30 characters go as several `17`s).
"""

from __future__ import annotations

import logging
from typing import Final

from radio_keyer import civ
from radio_keyer.rig import Rig

log = logging.getLogger(__name__)

MAX_CHUNK: Final[int] = 30
MEMORIES: Final[int] = 8
CMD_SEND_CW: Final[bytes] = b"\x17"
STOP: Final[bytes] = b"\xff"
CMD_BREAK_IN: Final[bytes] = b"\x16\x47"
CONTEST_NUMBER: Final[str] = "*"
# Characters `17` accepts (table "Codes for CW message contents"); '^' joins
# characters with no gap (prosigns). Text is uppercased first.
_ALLOWED: Final[frozenset[str]] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 /?.-,:'()=+\"@^"
)


def normalize_text(text: str) -> str:
    """Uppercase, single spaces; raise ValueError if `17` can't send it."""
    norm = " ".join(text.upper().split())
    if not norm:
        raise ValueError("CW text is empty")
    bad = sorted({c for c in norm if c not in _ALLOWED})
    if bad:
        raise ValueError(f"CW text has characters CI-V can't send: {''.join(bad)!r}")
    return norm


def chunks(text: str) -> list[str]:
    """Split normalized text into ≤30-character pieces at word gaps.

    Every piece but the last keeps its trailing space, so the word gap is sent.
    """
    out: list[str] = []
    current = ""
    for word in text.split(" "):
        if len(word) >= MAX_CHUNK:  # room for a gap is needed when it starts a piece
            raise ValueError(f"CW word longer than {MAX_CHUNK - 1} characters: {word!r}")
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= MAX_CHUNK:
            current = candidate
        elif len(current) < MAX_CHUNK:
            out.append(current + " ")
            current = word
        else:  # a full piece: the gap leads the next one
            out.append(current)
            current = " " + word
    out.append(current)
    return out


def send_commands(text: str) -> list[bytes]:
    """`17` command bodies for `text`."""
    return [CMD_SEND_CW + c.encode("ascii") for c in chunks(normalize_text(text))]


def memory_read_command(slot: int) -> bytes:
    if not 1 <= slot <= MEMORIES:
        raise ValueError(f"Icom keyer memory must be 1-{MEMORIES}: {slot}")
    return b"\x1a\x02" + civ.bcd(slot, 2)


async def send_text(rig: Rig, addr: int, text: str) -> None:
    for body in send_commands(text):
        await civ.write(rig, addr, body)


async def stop(rig: Rig, addr: int) -> None:
    await civ.write(rig, addr, CMD_SEND_CW + STOP)


async def read_memory(rig: Rig, addr: int, slot: int) -> str:
    data = await civ.read(rig, addr, memory_read_command(slot))
    try:
        return data.decode("ascii").strip()
    except UnicodeDecodeError:
        raise civ.CivError(f"keyer memory M{slot} is not text: {data.hex(' ').upper()}") from None


async def play_memory(rig: Rig, addr: int, slot: int) -> None:
    """Read keyer memory M`slot` and send its text with `17`."""
    text = await read_memory(rig, addr, slot)
    if not text:
        raise ValueError(f"keyer memory M{slot} is empty")
    if CONTEST_NUMBER in text:
        raise ValueError(
            f"keyer memory M{slot} uses the contest number '*', which can't be sent over CI-V"
        )
    log.info("keyer memory M%d: %s", slot, text)
    await send_text(rig, addr, text)


async def keyer_warning(rig: Rig, addr: int) -> str | None:
    """A warning if break-in is OFF (then `17` CW isn't transmitted), else None."""
    try:
        data = await civ.read(rig, addr, CMD_BREAK_IN)
    except civ.CivError as exc:
        return f"could not read break-in (16 47): {exc}"
    if data == b"\x00":
        return "break-in is OFF: CW sent over CI-V is only transmitted with break-in or TX on"
    return None

"""Yaesu memory keyer over CAT (verified against the FT-891 CAT manual, 1909-C).

Hamlib 4.5.5's `send_morse` is broken for the FT-891 (sends `KY<char>;`, the
radio answers `?;`). The radio's own keyer is used instead:

- `KY P1;` plays a memory: 1-5 = keyer (text) memory 1-5,
  6-9/A = message (paddle-recorded) memory 1-5. Set-only, no answer.
- `KM P1 <text>;` writes text memory P1 (1-5); the text ends with '}' and is
  at most 50 characters including it. '#' inserts the contest number.
- `KM P1;` reads it back → `KM P1 <text>;`.

- `EX0401;` reads KEYER TYPE → `EX0401P2;`. With 0 (OFF) the radio answers
  every `KY` with `?;` (verified on an FT-891, 2026-11-13).

Free CW text = write it to a scratch text memory, then play that memory.
`KM` text may contain spaces, which a rigctld `send_raw` token can't, so the
write goes hex-encoded (terminator '0': no answer) and the read-back as ASCII
with terminator ';'. Verified on an FT-891 with Hamlib 4.5.5.
"""

from __future__ import annotations

import logging
from typing import Final

from radio_keyer.rig import Rig

log = logging.getLogger(__name__)

TEXT_MEMORIES: Final[int] = 5
MEMORY_SLOTS: Final[int] = 10
"""cw_memory 1-5 = text memories, 6-10 = message (paddle) memories 1-5."""
MAX_TEXT: Final[int] = 49
"""50 characters including the closing '}'."""
END: Final[str] = "}"
_KY_CODES: Final[str] = "123456789A"
# Text-entry characters: letters, digits, space and common CW punctuation.
# ';' ends a CAT command and '}' ends the memory, so both are excluded.
_ALLOWED: Final[frozenset[str]] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ./?,-=+()'\":@&!#"
)


def ky_command(slot: int) -> bytes:
    """`KY` playback for cw_memory `slot` (1-10)."""
    if not 1 <= slot <= MEMORY_SLOTS:
        raise ValueError(f"Yaesu CW memory must be 1-{MEMORY_SLOTS}: {slot}")
    return f"KY{_KY_CODES[slot - 1]};".encode("ascii")


def normalize_text(text: str) -> str:
    """Uppercase, single spaces; raise ValueError if the radio can't store it."""
    norm = " ".join(text.upper().split())
    if not norm:
        raise ValueError("CW text is empty")
    bad = sorted({c for c in norm if c not in _ALLOWED})
    if bad:
        raise ValueError(f"CW text has characters the keyer can't send: {''.join(bad)!r}")
    if len(norm) > MAX_TEXT:
        raise ValueError(f"CW text is {len(norm)} characters; the keyer memory holds {MAX_TEXT}")
    return norm


def _check_text_memory(slot: int) -> int:
    if not 1 <= slot <= TEXT_MEMORIES:
        raise ValueError(f"Yaesu scratch text memory must be 1-{TEXT_MEMORIES}: {slot}")
    return slot


def km_write(slot: int, text: str) -> bytes:
    """`KM` write of `text` to text memory `slot`."""
    _check_text_memory(slot)
    return f"KM{slot}{normalize_text(text)}{END};".encode("ascii")


def km_read(slot: int) -> bytes:
    return f"KM{_check_text_memory(slot)};".encode("ascii")


async def play_memory(rig: Rig, slot: int) -> None:
    await rig.send_raw(ky_command(slot), binary=False)


async def send_text(rig: Rig, text: str, *, scratch: int) -> None:
    """Store `text` in text memory `scratch`, verify it, and play it."""
    write = km_write(scratch, text)
    await rig.send_raw(write, binary=True, terminator="0")
    answer = (await rig.send_raw(km_read(scratch), binary=False, terminator=";")).strip()
    expected = write.decode("ascii")
    if answer != expected:  # don't transmit whatever else is in the memory
        raise ValueError(
            f"keyer memory {scratch} read back {answer!r}, expected {expected!r}; not sent"
        )
    await play_memory(rig, scratch)


async def keyer_warning(rig: Rig) -> str | None:
    """A warning if the radio's KEYER TYPE would make `KY` fail, else None."""
    answer = (await rig.send_raw(b"EX0401;", binary=False, terminator=";")).strip()
    if answer == "EX04010;":
        return "KEYER TYPE (menu 04-01) is OFF: the radio rejects memory playback (KY)"
    if not answer.startswith("EX0401"):
        return f"could not read KEYER TYPE (menu 04-01): answer {answer!r}"
    return None

"""Yaesu memory keyer over CAT (verified against the FT-891 CAT manual, 1909-C).

Hamlib 4.5.5's `send_morse` is broken for the FT-891 (sends `KY<char>;`, the
radio answers `?;`). The radio's own keyer is used instead:

- `KY P1;` plays a memory: 1-5 = keyer (text) memory 1-5,
  6-9/A = message (paddle-recorded) memory 1-5. Set-only, no answer.
- `KM P1 <text>;` writes text memory P1 (1-5); the text ends with '}' and is
  at most 50 characters including it. '#' inserts the contest number.
- `KM P1;` reads it back → `KM P1 <text>;`.

Free CW text = write it to a scratch text memory, then play that memory.
`KM` text may contain spaces, which rigctld's `send_raw` can't carry, so it
goes via `send_cmd` with a `KM P1;` read appended (send_cmd waits for an answer).
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


def km_write_and_read(slot: int, text: str) -> str:
    """`KM` write of `text` to text memory `slot`, followed by a `KM` read."""
    _check_text_memory(slot)
    return f"KM{slot}{normalize_text(text)}{END};KM{slot};"


async def play_memory(rig: Rig, slot: int) -> None:
    await rig.send_raw(ky_command(slot), binary=False)


async def send_text(rig: Rig, text: str, *, scratch: int) -> None:
    """Store `text` in text memory `scratch` and play it."""
    command = km_write_and_read(scratch, text)
    expected = command.split(";", 1)[0]
    answer = await rig.send_cmd(command)
    if answer and expected not in answer:  # don't transmit whatever else is in the memory
        raise ValueError(
            f"keyer memory {scratch} read back {answer!r}, expected {expected + ';'!r}; not sent"
        )
    if not answer:
        log.debug("Keyer memory %d: no read-back answer", scratch)
    await play_memory(rig, scratch)

"""Icom CI-V framing over Hamlib `send_raw` (IC-7300 manual, section 19).

Frame: `FE FE <to> <from> <cmd> [sub] [data] FD`. The controller is E0h.
The radio answers a write with OK (`FB`) or NG (`FA`), and a read with the
command bytes followed by the data.

Through rigctld, `send_raw icom ...` reads up to the first `FD`. Hamlib 4.5.5's
text format for that answer is unverified on a real radio, so `answer_bytes`
accepts hex bytes with or without `0x`, separated by spaces, ';' or ','.

Frames the controller didn't ask for are skipped: an echo of our own command
(CI-V USB Echo Back ON) and Transceive broadcasts (to = 00h). If only such a
frame came back, the real reply is still unread and the stream is out of step,
so that is an error naming the radio setting to change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from radio_keyer.rig import Rig

CONTROLLER: Final[int] = 0xE0
BROADCAST: Final[int] = 0x00
DEFAULT_ADDRESS: Final[int] = 0x94
"""IC-7300 factory CI-V address."""
ADDRESS_RANGE: Final[tuple[int, int]] = (0x02, 0xDF)
PREAMBLE: Final[bytes] = b"\xfe\xfe"
END: Final[int] = 0xFD
OK: Final[int] = 0xFB
NG: Final[int] = 0xFA

_HEX_TOKEN: Final[re.Pattern[str]] = re.compile(r"^(?:0[xX])?([0-9A-Fa-f]{1,2})$")


class CivError(Exception):
    """A CI-V command failed or got an unusable answer."""


@dataclass(frozen=True, slots=True)
class Frame:
    to: int
    sender: int
    body: bytes
    """Command, sub command and data (between the addresses and FD)."""

    @property
    def is_ok(self) -> bool:
        return self.body == bytes([OK])

    @property
    def is_ng(self) -> bool:
        return self.body == bytes([NG])


def check_address(addr: int) -> int:
    lo, hi = ADDRESS_RANGE
    if not lo <= addr <= hi:
        raise ValueError(f"CI-V address must be {lo:02X}h-{hi:02X}h: {addr:02X}h")
    return addr


def frame(addr: int, body: bytes) -> bytes:
    """A controller → radio frame."""
    if not body:
        raise ValueError("CI-V command is empty")
    if any(b in (0xFE, END) for b in body):
        raise ValueError(f"CI-V body contains a reserved byte (FE/FD): {body.hex(' ')}")
    return PREAMBLE + bytes([check_address(addr), CONTROLLER]) + body + bytes([END])


def answer_bytes(text: str) -> bytes:
    """Bytes of a `send_raw` answer printed as hex ('0xfe 0xfe ...' or 'FE FE ...')."""
    out = bytearray()
    for token in re.split(r"[\s;,]+", text.strip()):
        if not token:
            continue
        m = _HEX_TOKEN.match(token)
        if m is None:
            raise CivError(f"unexpected CI-V answer format: {text!r}")
        out.append(int(m.group(1), 16))
    return bytes(out)


def split_frames(data: bytes) -> list[Frame]:
    """Complete frames in `data`; stray bytes between frames are ignored."""
    frames: list[Frame] = []
    i = 0
    while (start := data.find(PREAMBLE, i)) >= 0:
        j = start
        while j < len(data) and data[j] == 0xFE:  # extra FE preamble bytes are allowed
            j += 1
        end = data.find(bytes([END]), j)
        if end < 0:
            break
        if end - j >= 3:  # to, from, at least one body byte
            frames.append(Frame(to=data[j], sender=data[j + 1], body=data[j + 2 : end]))
        i = end + 1
    return frames


def pick_reply(frames: list[Frame], addr: int) -> Frame:
    """The radio's reply to the controller among `frames`."""
    for f in frames:
        if f.to == CONTROLLER and f.sender == addr:
            return f
    if any(f.to == addr and f.sender == CONTROLLER for f in frames):
        raise CivError(
            "got only the echo of our command: set the radio's CI-V USB Echo Back to OFF"
        )
    if any(f.to == BROADCAST for f in frames):
        raise CivError(
            "got only a Transceive broadcast: set the radio's CI-V Transceive to OFF"
        )
    if frames:
        shown = ", ".join(f"{f.sender:02X}h→{f.to:02X}h" for f in frames)
        raise CivError(f"no reply from radio {addr:02X}h (frames: {shown}); check rig.civ_address")
    raise CivError(f"no CI-V reply from radio {addr:02X}h")


async def transact(rig: Rig, addr: int, body: bytes) -> Frame:
    """Send one command and return the radio's reply frame. NG raises CivError."""
    answer = await rig.send_raw(frame(addr, body), binary=True, terminator="icom")
    reply = pick_reply(split_frames(answer_bytes(answer)), addr)
    if reply.is_ng:
        raise CivError(f"radio rejected CI-V command {body.hex(' ').upper()} (NG)")
    return reply


async def write(rig: Rig, addr: int, body: bytes) -> None:
    """Send a set command; the radio must answer OK."""
    reply = await transact(rig, addr, body)
    if not reply.is_ok:
        raise CivError(
            f"CI-V command {body.hex(' ').upper()}: expected OK, got {reply.body.hex(' ').upper()}"
        )


async def read(rig: Rig, addr: int, command: bytes) -> bytes:
    """Send a read command; return the data after the echoed command bytes."""
    reply = await transact(rig, addr, command)
    if not reply.body.startswith(command):
        raise CivError(
            f"CI-V read {command.hex(' ').upper()}: unexpected answer {reply.body.hex(' ').upper()}"
        )
    return reply.body[len(command) :]


def bcd(value: int, digits: int) -> bytes:
    """Big-endian packed BCD, e.g. bcd(255, 4) == b'\\x02\\x55'."""
    if digits % 2 or value < 0 or value >= 10**digits:
        raise ValueError(f"cannot encode {value} as {digits}-digit BCD")
    text = f"{value:0{digits}d}"
    return bytes(int(text[i : i + 2], 16) for i in range(0, digits, 2))


def from_bcd(data: bytes) -> int:
    """Inverse of `bcd`; raises CivError on a non-BCD nibble."""
    text = data.hex()
    if not data or not text.isdigit():
        raise CivError(f"not BCD data: {data.hex(' ').upper()}")
    return int(text)

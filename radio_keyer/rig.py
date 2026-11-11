"""Rig control.

RigctldRig talks to Hamlib's rigctld over TCP using the extended response
protocol (commands prefixed with '+'), where every reply ends in 'RPRT n'.

Hamlib 4.5 notes (verified against `rigctld -m 1`):
- An unknown command gets NO reply → every command has a timeout, and the
  connection is dropped on timeout because the stream may be out of sync.
- Arguments are whitespace-separated: `send_raw` takes a single token, so raw
  CAT cannot contain spaces. `send_morse` takes the rest of the line.
- `set_mode <mode> -1` keeps the current passband.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

log = logging.getLogger(__name__)

PASSBAND_NOCHANGE: Final[int] = -1
DEFAULT_TIMEOUT_S: Final[float] = 3.0

_RPRT_MESSAGES: Final[dict[int, str]] = {
    1: "invalid parameter",
    2: "invalid configuration",
    3: "memory shortage",
    4: "feature not implemented",
    5: "communication timed out",
    6: "IO error",
    7: "internal Hamlib error",
    8: "protocol error",
    9: "command rejected by the rig",
    10: "argument truncated",
    11: "feature not available",
    12: "target VFO unaccessible",
    13: "communication bus error",
    14: "communication bus collision",
    15: "NULL rig handle",
    16: "invalid VFO",
    17: "argument out of domain",
    18: "function deprecated",
    19: "security error",
    20: "rig is not powered on",
}


class RigError(Exception):
    """The rig/Hamlib rejected a command (RPRT -n)."""

    def __init__(self, command: str, code: int) -> None:
        self.command = command
        self.code = code
        msg = _RPRT_MESSAGES.get(code, "error")
        super().__init__(f"{command}: {msg} (RPRT -{code})")


class RigConnectionError(Exception):
    """Cannot reach rigctld, or the connection broke / timed out."""


@dataclass(frozen=True, slots=True)
class RigStatus:
    freq_hz: int
    mode: str
    passband_hz: int


class Rig(ABC):
    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def get_freq(self) -> int: ...

    @abstractmethod
    async def set_freq(self, hz: int) -> None: ...

    @abstractmethod
    async def get_mode(self) -> tuple[str, int]:
        """(mode, passband_hz)"""

    @abstractmethod
    async def set_mode(self, mode: str, passband_hz: int = PASSBAND_NOCHANGE) -> None: ...

    @abstractmethod
    async def send_morse(self, text: str) -> None: ...

    @abstractmethod
    async def stop_morse(self) -> None: ...

    @abstractmethod
    async def send_raw(self, data: bytes, *, binary: bool) -> str:
        """Send raw CAT. Returns the rig's answer as text (may be empty)."""

    async def status(self) -> RigStatus:
        freq = await self.get_freq()
        mode, passband = await self.get_mode()
        return RigStatus(freq, mode, passband)


# --- argument checks (shared) --------------------------------------------


def _check_token(value: str, what: str) -> str:
    if not value or any(c.isspace() for c in value) or not value.isascii():
        raise ValueError(f"{what} must be non-empty ASCII without spaces: {value!r}")
    return value


def _check_line(value: str, what: str) -> str:
    text = value.strip()
    if not text or "\n" in text or "\r" in text or not text.isascii():
        raise ValueError(f"{what} must be a single non-empty ASCII line: {value!r}")
    return text


def _check_freq(hz: int) -> int:
    if isinstance(hz, bool) or not isinstance(hz, int) or hz <= 0:
        raise ValueError(f"frequency must be a positive integer Hz: {hz!r}")
    return hz


def raw_args(data: bytes, *, binary: bool) -> tuple[str, str]:
    """(terminator, command) for rigctld `send_raw`.

    ASCII: sent as-is, terminator '0' = read no reply (fire and forget).
    Binary (Icom CI-V): '0xFE;0xFE;...' with terminator 'icom' (reads to 0xFD).
    """
    if not data:
        raise ValueError("raw CAT is empty")
    if binary:
        return "icom", ";".join(f"0x{b:02X}" for b in data)
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("raw CAT must be ASCII (use cat_hex for binary)") from None
    return "0", _check_token(text, "raw CAT over rigctld")


# --- rigctld --------------------------------------------------------------


class RigctldRig(Rig):
    def __init__(self, host: str, port: int, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._host = host
        self._port = port
        self._timeout_s = timeout_s
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    @property
    def address(self) -> str:
        return f"{self._host}:{self._port}"

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def close(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is not None and self._writer is not None and not self._writer.is_closing():
            return self._reader, self._writer
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), self._timeout_s
            )
        except (OSError, TimeoutError) as exc:
            raise RigConnectionError(f"cannot connect to rigctld at {self.address}: {exc}") from exc
        self._reader, self._writer = reader, writer
        log.info("Connected to rigctld at %s", self.address)
        return reader, writer

    async def _command(self, command: str) -> list[tuple[str, str]]:
        """Send one command; return the 'Key: value' lines of the reply."""
        name = command.split(" ", 1)[0]
        line = f"+\\{_check_line(command, 'command')}\n".encode("ascii")
        async with self._lock:
            reader, writer = await self._connect()
            try:
                writer.write(line)
                await asyncio.wait_for(writer.drain(), self._timeout_s)
                return await asyncio.wait_for(self._read_reply(reader, name), self._timeout_s)
            except (OSError, TimeoutError, ConnectionError, asyncio.IncompleteReadError) as exc:
                await self.close()
                reason = "timed out" if isinstance(exc, TimeoutError) else str(exc) or type(exc).__name__
                raise RigConnectionError(f"rigctld {name}: {reason}") from exc

    @staticmethod
    async def _read_reply(reader: asyncio.StreamReader, name: str) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        header_seen = False
        while True:
            raw = await reader.readline()
            if not raw:
                raise ConnectionError("rigctld closed the connection")
            text = raw.decode("ascii", errors="replace").rstrip("\r\n")
            if text.startswith("RPRT "):
                try:
                    code = int(text[5:])
                except ValueError:
                    raise ConnectionError(f"bad reply {text!r}") from None
                if code != 0:
                    raise RigError(name, abs(code))
                return pairs
            if not header_seen:  # echo line, e.g. "set_freq: 14074000"
                header_seen = True
                continue
            key, _, value = text.partition(":")
            pairs.append((key.strip(), value.strip()))

    @staticmethod
    def _value(pairs: list[tuple[str, str]], key: str, name: str) -> str:
        for k, v in pairs:
            if k == key:
                return v
        raise RigConnectionError(f"rigctld {name}: reply missing '{key}'")

    async def get_freq(self) -> int:
        value = self._value(await self._command("get_freq"), "Frequency", "get_freq")
        try:
            return round(float(value))
        except ValueError:
            raise RigConnectionError(f"rigctld get_freq: bad frequency {value!r}") from None

    async def set_freq(self, hz: int) -> None:
        await self._command(f"set_freq {_check_freq(hz)}")

    async def get_mode(self) -> tuple[str, int]:
        pairs = await self._command("get_mode")
        mode = self._value(pairs, "Mode", "get_mode")
        passband = self._value(pairs, "Passband", "get_mode")
        try:
            return mode, int(passband)
        except ValueError:
            raise RigConnectionError(f"rigctld get_mode: bad passband {passband!r}") from None

    async def set_mode(self, mode: str, passband_hz: int = PASSBAND_NOCHANGE) -> None:
        await self._command(f"set_mode {_check_token(mode, 'mode')} {int(passband_hz)}")

    async def send_morse(self, text: str) -> None:
        await self._command(f"send_morse {_check_line(text, 'CW text')}")

    async def stop_morse(self) -> None:
        await self._command("stop_morse")

    async def send_raw(self, data: bytes, *, binary: bool) -> str:
        term, cmd = raw_args(data, binary=binary)
        pairs = await self._command(f"send_raw {term} {cmd}")
        return next((v for k, v in pairs if k == "Send raw answer"), "")


# --- simulated (dry run) --------------------------------------------------


class SimulatedRig(Rig):
    """In-memory rig for dry runs: reports every command via `out`."""

    def __init__(
        self,
        out: Callable[[str], None],
        *,
        freq_hz: int = 14_030_000,
        mode: str = "CW",
        passband_hz: int = 500,
    ) -> None:
        self._out = out
        self.freq_hz = freq_hz
        self.mode = mode
        self.passband_hz = passband_hz

    async def close(self) -> None:
        return None

    async def get_freq(self) -> int:
        return self.freq_hz

    async def set_freq(self, hz: int) -> None:
        self.freq_hz = _check_freq(hz)
        self._out(f"set_freq {hz / 1e6:.6f} MHz")

    async def get_mode(self) -> tuple[str, int]:
        return self.mode, self.passband_hz

    async def set_mode(self, mode: str, passband_hz: int = PASSBAND_NOCHANGE) -> None:
        self.mode = _check_token(mode, "mode")
        if passband_hz != PASSBAND_NOCHANGE:
            self.passband_hz = passband_hz
        self._out(f"set_mode {mode}")

    async def send_morse(self, text: str) -> None:
        self._out(f'send_morse "{_check_line(text, "CW text")}"')

    async def stop_morse(self) -> None:
        self._out("stop_morse")

    async def send_raw(self, data: bytes, *, binary: bool) -> str:
        term, cmd = raw_args(data, binary=binary)
        self._out(f"send_raw {term} {cmd}")
        return ""

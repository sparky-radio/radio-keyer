"""CW by keying a serial control line (DTR or RTS), timed by the app.

For radios that key CW from a USB serial port line, e.g. the FT-891's Standard
port (CP2105 `if01`) with menu 07-12 PC KEYING = DTR (or RTS) and BK-IN on.

Timing (PARIS): dot = 1.2 / WPM s, dash = 3 dots, 1 dot between elements,
3 between characters, 7 between words. A worker thread sends against absolute
deadlines (no drift) and spins for the last ~2 ms of each wait.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Final, Protocol

log = logging.getLogger(__name__)

MIN_WPM: Final[int] = 5
MAX_WPM: Final[int] = 50
_SPIN_S: Final[float] = 0.002

MORSE: Final[dict[str, str]] = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "/": "-..-.", "=": "-...-",
    "+": ".-.-.", "-": "-....-", "(": "-.--.", ")": "-.--.-", "'": ".----.",
    '"': ".-..-.", ":": "---...", "@": ".--.-.", "&": ".-...", "!": "-.-.--",
}  # fmt: skip


class KeyLineName(StrEnum):
    DTR = "dtr"
    RTS = "rts"


class KeyerError(Exception):
    """The keying port can't be opened or written."""


type Element = tuple[bool, float]
"""(key down, seconds)."""


def normalize(text: str) -> str:
    """Uppercase, single spaces; raise ValueError for characters without Morse."""
    norm = " ".join(text.upper().split())
    if not norm:
        raise ValueError("CW text is empty")
    bad = sorted({c for c in norm if c != " " and c not in MORSE})
    if bad:
        raise ValueError(f"CW text has characters without Morse: {''.join(bad)!r}")
    return norm


def elements(text: str, wpm: int) -> list[Element]:
    """Key-down/key-up schedule for `text`, ending with a word gap."""
    if not MIN_WPM <= wpm <= MAX_WPM:
        raise ValueError(f"WPM must be {MIN_WPM}-{MAX_WPM}: {wpm}")
    unit = 1.2 / wpm
    out: list[Element] = []
    for w, word in enumerate(normalize(text).split(" ")):
        if w:
            out[-1] = (False, 7 * unit)
        for c, char in enumerate(word):
            if c:
                out[-1] = (False, 3 * unit)
            for symbol in MORSE[char]:
                out.append((True, (1 if symbol == "." else 3) * unit))
                out.append((False, unit))
    out[-1] = (False, 7 * unit)
    return out


class KeyLine(Protocol):
    def set(self, down: bool) -> None: ...

    def close(self) -> None: ...


class SerialKeyLine:
    """DTR or RTS on a serial port; held up (unkeyed) while open."""

    def __init__(self, port: str, line: KeyLineName) -> None:
        import serial  # pyserial; imported here so dry runs and tests don't need a port

        self._line = line
        ser = serial.Serial()
        ser.port = port
        ser.dtr = False  # applied on open; Linux may still pulse DTR/RTS for a moment
        ser.rts = False
        ser.exclusive = True
        try:
            ser.open()
        except (serial.SerialException, OSError) as exc:
            raise KeyerError(f"cannot open CW keying port {port}: {exc}") from exc
        self._ser = ser
        self.set(False)

    def set(self, down: bool) -> None:
        try:
            if self._line is KeyLineName.DTR:
                self._ser.dtr = down
            else:
                self._ser.rts = down
        except OSError as exc:
            raise KeyerError(f"CW keying port: {exc}") from exc

    def close(self) -> None:
        try:
            self.set(False)
        finally:
            self._ser.close()


class NullKeyLine:
    """No hardware (dry run)."""

    def set(self, down: bool) -> None:
        return None

    def close(self) -> None:
        return None


class DtrKeyer:
    """Queues CW text and keys it on a background thread."""

    def __init__(self, open_line: Callable[[], KeyLine], wpm: int) -> None:
        if not MIN_WPM <= wpm <= MAX_WPM:
            raise ValueError(f"WPM must be {MIN_WPM}-{MAX_WPM}: {wpm}")
        self._open_line = open_line
        self._wpm = wpm
        self._line: KeyLine | None = None
        self._queue: queue.SimpleQueue[tuple[int, list[Element]] | None] = queue.SimpleQueue()
        self._generation = 0
        """Bumped by stop(): queued text from an older generation is dropped."""
        self._abort = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._pending = 0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return not self._idle.is_set()

    def send(self, text: str) -> None:
        """Queue `text`. Raises ValueError (bad text) or KeyerError (port)."""
        schedule = elements(text, self._wpm)
        with self._lock:
            if self._line is None:
                self._line = self._open_line()
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="dtr-keyer", daemon=True)
                self._thread.start()
            self._pending += 1
            self._idle.clear()
            item = (self._generation, schedule)
        self._queue.put(item)

    def stop(self) -> None:
        """Abort the current text and drop everything queued."""
        with self._lock:
            self._generation += 1
            self._abort.set()

    def wait_idle(self, timeout_s: float | None = None) -> bool:
        return self._idle.wait(timeout_s)

    def close(self) -> None:
        self.stop()
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is not None:
            self._queue.put(None)
            thread.join(timeout=2)
        with self._lock:
            line, self._line = self._line, None
        if line is not None:
            line.close()

    def _run(self) -> None:
        while (item := self._queue.get()) is not None:
            generation, schedule = item
            try:
                with self._lock:
                    current = generation == self._generation
                    if current:
                        self._abort.clear()  # only a later stop() sets it again
                if current:
                    self._key(schedule)
            except KeyerError as exc:
                log.error("CW keying failed: %s", exc)
            finally:
                with self._lock:
                    self._pending -= 1
                    if self._pending == 0:
                        self._idle.set()

    def _key(self, schedule: list[Element]) -> None:
        line = self._line
        if line is None:
            return
        deadline = time.perf_counter()
        try:
            for down, seconds in schedule:
                if self._abort.is_set():
                    return
                line.set(down)
                deadline += seconds
                if self._wait_until(deadline):
                    return
        finally:
            line.set(False)

    def _wait_until(self, deadline: float) -> bool:
        """Sleep until `deadline`; True if aborted."""
        coarse = deadline - time.perf_counter() - _SPIN_S
        if coarse > 0 and self._abort.wait(coarse):
            return True
        while time.perf_counter() < deadline:
            pass
        return self._abort.is_set()

"""MK623Pro keypad reader.

- Opens the keypad by its stable /dev/input/by-id path.
- Grabs it exclusively so key presses never reach other applications.
- Detects short vs long presses (long fires while still held, at the threshold).
- Reconnects automatically after unplug/replug.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Final, Self

import evdev
from evdev import ecodes

log = logging.getLogger(__name__)

DEFAULT_DEVICE_PATH: Final[str] = "/dev/input/by-id/usb-PCsensor_MK623Pro-event-kbd"
DEFAULT_LONG_PRESS_S: Final[float] = 0.6
DEFAULT_RECONNECT_DELAY_S: Final[float] = 2.0

# Factory keycodes → physical position (1 = top-left, left→right, top→bottom).
DEFAULT_KEYMAP: Final[Mapping[int, int]] = {
    ecodes.KEY_F: 1,
    ecodes.KEY_E: 2,
    ecodes.KEY_D: 3,
    ecodes.KEY_C: 4,
    ecodes.KEY_B: 5,
    ecodes.KEY_A: 6,
}

_KEY_UP: Final[int] = 0
_KEY_DOWN: Final[int] = 1
_KEY_REPEAT: Final[int] = 2


class PressKind(StrEnum):
    SHORT = "short"
    LONG = "long"


@dataclass(frozen=True, slots=True)
class KeyPress:
    position: int
    kind: PressKind
    keycode: int
    held_s: float
    """Short: how long the key was held. Long: the threshold at which it fired."""


def key_name(keycode: int) -> str:
    """Human-readable evdev key name, e.g. 'KEY_F'."""
    name: str | list[str] | None = ecodes.KEY.get(keycode)
    if name is None:
        return f"KEY_{keycode}"
    return name if isinstance(name, str) else name[0]


@dataclass(slots=True)
class _Held:
    down_at: float
    timer: asyncio.TimerHandle
    long_fired: bool = False


class PressDetector:
    """Turns key down/up events into SHORT / LONG presses. Independent of evdev."""

    def __init__(self, long_press_s: float, emit: Callable[[KeyPress], None]) -> None:
        if long_press_s <= 0:
            raise ValueError("long_press_s must be > 0")
        self._long_press_s = long_press_s
        self._emit = emit
        self._held: dict[int, _Held] = {}

    def key_down(self, keycode: int, position: int) -> None:
        if keycode in self._held:
            return
        loop = asyncio.get_running_loop()
        timer = loop.call_later(self._long_press_s, self._fire_long, keycode, position)
        self._held[keycode] = _Held(down_at=time.monotonic(), timer=timer)

    def key_up(self, keycode: int, position: int) -> None:
        held = self._held.pop(keycode, None)
        if held is None:
            return
        held.timer.cancel()
        if not held.long_fired:
            duration = time.monotonic() - held.down_at
            self._emit(KeyPress(position, PressKind.SHORT, keycode, duration))

    def reset(self) -> None:
        """Forget all held keys without emitting (e.g. on disconnect)."""
        for held in self._held.values():
            held.timer.cancel()
        self._held.clear()

    def _fire_long(self, keycode: int, position: int) -> None:
        held = self._held.get(keycode)
        if held is None or held.long_fired:
            return
        held.long_fired = True
        self._emit(KeyPress(position, PressKind.LONG, keycode, self._long_press_s))


class Keypad:
    """Async iterator of KeyPress events from the MK623Pro.

    Usage:
        async with Keypad() as keypad:
            async for press in keypad:
                ...
    """

    def __init__(
        self,
        device_path: str = DEFAULT_DEVICE_PATH,
        keymap: Mapping[int, int] = DEFAULT_KEYMAP,
        *,
        long_press_s: float = DEFAULT_LONG_PRESS_S,
        grab: bool = True,
        reconnect_delay_s: float = DEFAULT_RECONNECT_DELAY_S,
    ) -> None:
        self._device_path = device_path
        self._keymap: dict[int, int] = dict(keymap)
        self._grab = grab
        self._reconnect_delay_s = reconnect_delay_s
        self._queue: asyncio.Queue[KeyPress] = asyncio.Queue()
        self._detector = PressDetector(long_press_s, self._queue.put_nowait)
        self._task: asyncio.Task[None] | None = None
        self.connected: bool = False

    # --- lifecycle -------------------------------------------------------

    async def __aenter__(self) -> Self:
        self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="keypad")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._detector.reset()

    # --- iteration -------------------------------------------------------

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> KeyPress:
        if not self._queue.empty():
            return self._queue.get_nowait()
        task = self._task
        if task is None:
            raise StopAsyncIteration
        getter: asyncio.Task[KeyPress] = asyncio.create_task(self._queue.get())
        try:
            await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not getter.done():
                getter.cancel()
        if getter.done() and not getter.cancelled():
            return getter.result()
        task.result()  # re-raise a reader crash
        raise StopAsyncIteration

    # --- reader ----------------------------------------------------------

    async def _run(self) -> None:
        waiting_logged = False
        while True:
            try:
                device = self._open()
            except FileNotFoundError:
                if not waiting_logged:
                    log.info("Waiting for keypad at %s", self._device_path)
                    waiting_logged = True
                await asyncio.sleep(self._reconnect_delay_s)
                continue
            except PermissionError:
                log.error(
                    "Permission denied opening %s - install the udev rule (README -> Permissions)",
                    self._device_path,
                )
                await asyncio.sleep(self._reconnect_delay_s)
                continue
            except OSError as exc:
                log.warning("Cannot open/grab keypad (%s); is another program holding it?", exc)
                await asyncio.sleep(self._reconnect_delay_s)
                continue

            waiting_logged = False
            try:
                await self._read(device)
            except OSError as exc:
                log.warning("Keypad disconnected: %s", exc)
            finally:
                self.connected = False
                self._detector.reset()
                self._close(device)
            await asyncio.sleep(self._reconnect_delay_s)

    def _open(self) -> evdev.InputDevice:
        device = evdev.InputDevice(self._device_path)
        if self._grab:
            try:
                device.grab()
            except OSError:
                device.close()
                raise
        self.connected = True
        log.info(
            "Keypad connected: %s (%s)%s",
            device.name,
            device.path,
            " [grabbed]" if self._grab else "",
        )
        return device

    def _close(self, device: evdev.InputDevice) -> None:
        if self._grab:
            with contextlib.suppress(OSError):
                device.ungrab()
        with contextlib.suppress(OSError):
            device.close()

    async def _read(self, device: evdev.InputDevice) -> None:
        async for event in device.async_read_loop():
            if event.type != ecodes.EV_KEY:
                continue
            position = self._keymap.get(event.code)
            if position is None:
                if event.value == _KEY_DOWN:
                    log.debug("Unmapped key %s", key_name(event.code))
                continue
            if event.value == _KEY_DOWN:
                self._detector.key_down(event.code, position)
            elif event.value == _KEY_UP:
                self._detector.key_up(event.code, position)
            # _KEY_REPEAT ignored: long press is timer-based.

"""Keypad reader (any evdev keyboard-like device; MK623Pro by default).

- Opens the keypad by a device path (e.g. a stable /dev/input/by-id link) or
  by its evdev device name (covers Bluetooth / 2.4 GHz, which have no by-id path).
- Grabs it exclusively so key presses never reach other applications.
- Detects short vs long presses (long fires while still held, at the threshold).
- Reconnects automatically after unplug/replug.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
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

BY_ID_DIR: Final[str] = "/dev/input/by-id"

# MK623Pro factory keycodes in position order (1 = top-left, left→right, top→bottom).
DEFAULT_KEY_NAMES: Final[tuple[str, ...]] = ("KEY_F", "KEY_E", "KEY_D", "KEY_C", "KEY_B", "KEY_A")

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
    name: str | list[str] | tuple[str, ...] | None = ecodes.KEY.get(keycode)
    if name is None:
        name = ecodes.BTN.get(keycode)
    if name is None:
        return f"KEY_{keycode}"
    return name if isinstance(name, str) else name[0]


def key_code(name: str) -> int:
    """evdev key code for a name like 'KEY_F', 'f' or 'BTN_0'. ValueError if unknown."""
    upper = name.strip().upper()
    candidates = [upper] if upper.startswith(("KEY_", "BTN_")) else [f"KEY_{upper}"]
    for candidate in candidates:
        code = ecodes.ecodes.get(candidate)
        if code is not None and (code in ecodes.KEY or code in ecodes.BTN):
            return int(code)
    raise ValueError(f"unknown key name '{name}'")


def keymap_from_names(names: Sequence[str]) -> dict[int, int]:
    """Key names in position order → {keycode: position}."""
    return {key_code(name): pos for pos, name in enumerate(names, start=1)}


DEFAULT_KEYMAP: Final[Mapping[int, int]] = keymap_from_names(DEFAULT_KEY_NAMES)


# --- device discovery -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    path: str
    name: str
    by_id: tuple[str, ...]
    """/dev/input/by-id links pointing at this device."""
    has_keys: bool
    """Reports EV_KEY events (keyboards, keypads, mice buttons)."""


def _by_id_links() -> dict[str, list[str]]:
    links: dict[str, list[str]] = {}
    try:
        entries = sorted(os.listdir(BY_ID_DIR))
    except OSError:
        return links
    for entry in entries:
        link = os.path.join(BY_ID_DIR, entry)
        links.setdefault(os.path.realpath(link), []).append(link)
    return links


def _event_paths() -> list[str]:
    """All /dev/input/event* nodes (evdev.list_devices() hides unreadable ones)."""
    return sorted(glob.glob("/dev/input/event*"), key=_event_number)


def list_devices() -> tuple[list[DeviceInfo], list[str]]:
    """Readable input devices, and the paths that could not be opened (permissions)."""
    links = _by_id_links()
    found: list[DeviceInfo] = []
    unreadable: list[str] = []
    for path in _event_paths():
        try:
            device = evdev.InputDevice(path)
        except OSError:
            unreadable.append(path)
            continue
        try:
            has_keys = ecodes.EV_KEY in device.capabilities()
            found.append(DeviceInfo(path, device.name, tuple(links.get(path, ())), has_keys))
        finally:
            device.close()
    return found, unreadable


def _event_number(path: str) -> int:
    digits = path.rsplit("event", 1)[-1]
    return int(digits) if digits.isdigit() else 0


def find_device_by_name(name: str, keycodes: Iterable[int] = ()) -> str:
    """Path of the input device called `name` (exact match).

    Devices that send no keys are skipped. If several share the name, the one
    that can send all `keycodes` wins.
    FileNotFoundError if none matches.
    """
    wanted = set(keycodes)
    fallback: str | None = None
    unreadable = 0
    keyless = 0
    for path in _event_paths():
        try:
            device = evdev.InputDevice(path)
        except OSError:
            unreadable += 1
            continue
        try:
            if device.name != name:
                continue
            keys = set(device.capabilities().get(ecodes.EV_KEY, ()))
        finally:
            device.close()
        if keys and wanted <= keys:
            return path
        if not keys:
            keyless += 1
        elif fallback is None:
            fallback = path
    if fallback is not None:
        return fallback
    if keyless:
        raise FileNotFoundError(f"input device '{name}' sends no keys (try `keytest --list`)")
    hint = f" ({unreadable} input devices not readable: udev rule?)" if unreadable else ""
    raise FileNotFoundError(f"no input device named '{name}'{hint}")


# --- learn ----------------------------------------------------------------


class KeyLearner:
    """Records key codes in press order; pressing the first key again finishes."""

    def __init__(self) -> None:
        self.codes: list[int] = []
        self.done = False

    def key_down(self, keycode: int) -> str:
        """Feed one key-down. Returns 'added', 'done' or 'duplicate'."""
        if self.done:
            return "done"
        if self.codes and keycode == self.codes[0]:
            self.done = True
            return "done"
        if keycode in self.codes:
            return "duplicate"
        self.codes.append(keycode)
        return "added"

    @property
    def names(self) -> list[str]:
        return [key_name(code) for code in self.codes]


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
    """Async iterator of KeyPress events from a keypad.

    The device is `device_path`, or, when `device_name` is given, the input
    device with that name (looked up again on every reconnect).

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
        device_name: str | None = None,
        long_press_s: float = DEFAULT_LONG_PRESS_S,
        grab: bool = True,
        reconnect_delay_s: float = DEFAULT_RECONNECT_DELAY_S,
        on_unmapped: Callable[[int], None] | None = None,
    ) -> None:
        self._device_path = device_path
        self._device_name = device_name
        self._on_unmapped = on_unmapped
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
            except FileNotFoundError as exc:
                if not waiting_logged:
                    detail = f": {exc}" if self._device_name is not None else ""
                    log.info("Waiting for keypad %s%s", self.describe_device(), detail)
                    waiting_logged = True
                await asyncio.sleep(self._reconnect_delay_s)
                continue
            except PermissionError:
                log.error(
                    "Permission denied opening keypad %s - install the udev rule "
                    "(README -> Permissions)",
                    self.describe_device(),
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

    def describe_device(self) -> str:
        if self._device_name is not None:
            return f"named '{self._device_name}'"
        return f"at {self._device_path}"

    def _open(self) -> evdev.InputDevice:
        path = self._device_path
        if self._device_name is not None:
            path = find_device_by_name(self._device_name, self._keymap)
        device = evdev.InputDevice(path)
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
                    if self._on_unmapped is not None:
                        self._on_unmapped(event.code)
                continue
            if event.value == _KEY_DOWN:
                self._detector.key_down(event.code, position)
            elif event.value == _KEY_UP:
                self._detector.key_up(event.code, position)
            # _KEY_REPEAT ignored: long press is timer-based.


# --- learn: device I/O ----------------------------------------------------


async def wait_for_any_key(paths: Sequence[str]) -> tuple[str, int]:
    """Watch `paths` (not grabbed) until a key goes down and up again.

    Returns (device path, keycode). Waiting for the release before anyone grabs
    the device keeps the desktop from seeing a key that never comes up.
    """
    devices: list[evdev.InputDevice] = []
    for path in paths:
        with contextlib.suppress(OSError):
            devices.append(evdev.InputDevice(path))
    if not devices:
        raise FileNotFoundError("no readable input devices (udev rule? try `keytest --list`)")

    async def watch(device: evdev.InputDevice) -> tuple[str, int]:
        pressed: int | None = None
        async for event in device.async_read_loop():
            if event.type != ecodes.EV_KEY:
                continue
            if event.value == _KEY_DOWN and pressed is None:
                pressed = event.code
            elif event.value == _KEY_UP and event.code == pressed:
                return device.path, event.code
        raise OSError(f"{device.path} closed")

    tasks = [asyncio.create_task(watch(d)) for d in devices]
    try:
        while tasks:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception() is None:
                    return task.result()
            tasks = list(pending)  # a device went away; keep watching the rest
        raise FileNotFoundError("all input devices went away")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for device in devices:
            with contextlib.suppress(OSError):
                device.close()


async def learn_keys(
    path: str,
    learner: KeyLearner,
    report: Callable[[str, int], None],
    *,
    grab: bool = True,
) -> None:
    """Feed key-downs from `path` to `learner` until it is done.

    `report(result, keycode)` is called for each key-down ('added', 'duplicate', 'done').
    """
    device = evdev.InputDevice(path)
    try:
        if grab:
            device.grab()
        async for event in device.async_read_loop():
            if event.type != ecodes.EV_KEY or event.value != _KEY_DOWN:
                continue
            result = learner.key_down(event.code)
            report(result, event.code)
            if learner.done:
                break
    finally:
        if grab:
            with contextlib.suppress(OSError):
                device.ungrab()
        with contextlib.suppress(OSError):
            device.close()

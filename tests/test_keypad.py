import asyncio

from radio_keyer.keypad import KeyPress, PressDetector, PressKind


def _run(coro_fn) -> list[KeyPress]:  # type: ignore[no-untyped-def]
    out: list[KeyPress] = []
    asyncio.run(coro_fn(PressDetector(0.1, out.append)))
    return out


def test_short_press() -> None:
    async def scenario(d: PressDetector) -> None:
        d.key_down(33, 1)
        await asyncio.sleep(0.03)
        d.key_up(33, 1)

    out = _run(scenario)
    assert [(p.position, p.kind) for p in out] == [(1, PressKind.SHORT)]


def test_long_press_fires_while_held_and_not_again_on_release() -> None:
    async def scenario(d: PressDetector) -> None:
        d.key_down(18, 2)
        await asyncio.sleep(0.15)
        d.key_up(18, 2)

    out = _run(scenario)
    assert [(p.position, p.kind) for p in out] == [(2, PressKind.LONG)]


def test_duplicate_down_and_stray_up_ignored() -> None:
    async def scenario(d: PressDetector) -> None:
        d.key_down(32, 3)
        d.key_down(32, 3)
        await asyncio.sleep(0.02)
        d.key_up(32, 3)
        d.key_up(46, 4)

    out = _run(scenario)
    assert [(p.position, p.kind) for p in out] == [(3, PressKind.SHORT)]


def test_reset_cancels_pending_long() -> None:
    async def scenario(d: PressDetector) -> None:
        d.key_down(48, 5)
        d.reset()
        await asyncio.sleep(0.15)

    assert _run(scenario) == []


# --- key names, device lookup, learning -----------------------------------

import pytest  # noqa: E402
from evdev import ecodes  # noqa: E402

from radio_keyer import keypad as kp  # noqa: E402


def test_key_code_and_name() -> None:
    assert kp.key_code("KEY_F") == kp.key_code("f") == ecodes.KEY_F
    assert kp.key_code("btn_0") == ecodes.BTN_0
    assert kp.key_name(ecodes.BTN_0) == "BTN_0"
    for bad in ("KEY_NOPE", "EV_KEY", "ABS_X", ""):
        with pytest.raises(ValueError):
            kp.key_code(bad)
    assert kp.keymap_from_names(["KEY_F", "KEY_E"]) == {ecodes.KEY_F: 1, ecodes.KEY_E: 2}


def test_key_learner() -> None:
    learner = kp.KeyLearner()
    assert learner.key_down(ecodes.KEY_F) == "added"
    assert learner.key_down(ecodes.KEY_E) == "added"
    assert learner.key_down(ecodes.KEY_E) == "duplicate"
    assert learner.key_down(ecodes.KEY_D) == "added"
    assert learner.key_down(ecodes.KEY_F) == "done"
    assert learner.done and learner.names == ["KEY_F", "KEY_E", "KEY_D"]
    assert learner.key_down(ecodes.KEY_C) == "done"
    assert learner.names == ["KEY_F", "KEY_E", "KEY_D"]


class _FakeDevice:
    """Stands in for evdev.InputDevice."""

    table: dict[str, tuple[str, list[int]] | None] = {}

    def __init__(self, path: str) -> None:
        entry = self.table[path]
        if entry is None:
            raise PermissionError(13, "Permission denied", path)
        self.path = path
        self.name, self._keys = entry

    def capabilities(self) -> dict[int, list[int]]:
        return {ecodes.EV_KEY: self._keys} if self._keys else {ecodes.EV_SYN: [0]}

    def close(self) -> None:
        pass


@pytest.fixture
def fake_devices(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[str, list[int]] | None]:
    table: dict[str, tuple[str, list[int]] | None] = {}
    monkeypatch.setattr(_FakeDevice, "table", table)
    monkeypatch.setattr(kp.evdev, "InputDevice", _FakeDevice)
    monkeypatch.setattr(kp, "_event_paths", lambda: list(table))
    monkeypatch.setattr(kp, "_by_id_links", lambda: {"/dev/input/event2": ["/dev/input/by-id/x-event-kbd"]})
    return table


def test_find_device_by_name(fake_devices: dict[str, tuple[str, list[int]] | None]) -> None:
    abc = [ecodes.KEY_A, ecodes.KEY_B, ecodes.KEY_C]
    fake_devices.update(
        {
            "/dev/input/event1": ("Pad", []),  # no keys
            "/dev/input/event2": ("Pad", [ecodes.KEY_VOLUMEUP]),  # other keys
            "/dev/input/event3": ("Pad", abc),
            "/dev/input/event4": None,  # unreadable
        }
    )
    assert kp.find_device_by_name("Pad", abc) == "/dev/input/event3"
    assert kp.find_device_by_name("Pad", [ecodes.KEY_Z]) == "/dev/input/event2"  # fallback
    with pytest.raises(FileNotFoundError, match=r"1 input devices not readable"):
        kp.find_device_by_name("Other")

    devices, unreadable = kp.list_devices()
    assert [(d.path, d.has_keys) for d in devices] == [
        ("/dev/input/event1", False),
        ("/dev/input/event2", True),
        ("/dev/input/event3", True),
    ]
    assert devices[1].by_id == ("/dev/input/by-id/x-event-kbd",)
    assert unreadable == ["/dev/input/event4"]


def test_find_device_by_name_without_keys(fake_devices: dict[str, tuple[str, list[int]] | None]) -> None:
    fake_devices["/dev/input/event1"] = ("Pad", [])
    with pytest.raises(FileNotFoundError, match="sends no keys"):
        kp.find_device_by_name("Pad")

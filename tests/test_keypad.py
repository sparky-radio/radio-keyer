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

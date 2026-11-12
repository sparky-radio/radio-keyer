import asyncio
import shutil
import socket
import subprocess
import time
from collections.abc import Awaitable, Callable

import pytest

from radio_keyer.rig import (
    RigConnectionError,
    RigctldRig,
    RigError,
    SimulatedRig,
    raw_args,
)
from tests.fake_rigctld import FakeRigctld

type Scenario = Callable[[RigctldRig, FakeRigctld], Awaitable[None]]


def with_fake(scenario: Scenario, *, timeout_s: float = 1.0) -> FakeRigctld:
    fake = FakeRigctld()

    async def main() -> None:
        await fake.start()
        rig = RigctldRig("127.0.0.1", fake.port, timeout_s=timeout_s)
        try:
            await scenario(rig, fake)
        finally:
            await rig.close()
            await fake.stop()

    asyncio.run(main())
    return fake


def test_freq_and_mode_roundtrip() -> None:
    async def s(rig: RigctldRig, fake: FakeRigctld) -> None:
        assert await rig.get_freq() == 145_000_000
        await rig.set_freq(14_074_000)
        await rig.set_mode("PKTUSB")
        st = await rig.status()
        assert (st.freq_hz, st.mode, st.passband_hz) == (14_074_000, "PKTUSB", 15_000)

    fake = with_fake(s)
    assert "+\\set_mode PKTUSB -1" in fake.received


def test_send_morse_keeps_spaces() -> None:
    async def s(rig: RigctldRig, fake: FakeRigctld) -> None:
        await rig.send_morse("CQ TEST K5AQ")

    assert "+\\send_morse CQ TEST K5AQ" in with_fake(s).received


def test_rig_error_code() -> None:
    async def s(rig: RigctldRig, fake: FakeRigctld) -> None:
        fake.morse_supported = False
        with pytest.raises(RigError, match="feature not available") as exc_info:
            await rig.stop_morse()
        assert exc_info.value.code == 11
        assert await rig.get_freq() > 0  # connection still usable

    with_fake(s)


def test_send_raw_ascii_and_binary() -> None:
    async def s(rig: RigctldRig, fake: FakeRigctld) -> None:
        assert await rig.send_raw(b"FA;", binary=False) == "OK"
        await rig.send_raw(bytes.fromhex("FEFE94E003FD"), binary=True)

    received = with_fake(s).received
    assert "+\\send_raw 0 FA;" in received
    assert "+\\send_raw icom 0xFE;0xFE;0x94;0xE0;0x03;0xFD" in received


def test_injection_rejected_without_sending() -> None:
    async def s(rig: RigctldRig, fake: FakeRigctld) -> None:
        with pytest.raises(ValueError):
            await rig.send_morse("CQ\nset_freq 1")
        with pytest.raises(ValueError):
            await rig.set_mode("CW 0\nX")
        with pytest.raises(ValueError):
            await rig.send_raw(b"KY CQ;", binary=False)
        with pytest.raises(ValueError):
            await rig.set_freq(0)

    assert with_fake(s).received == []


def test_timeout_drops_connection_then_recovers() -> None:
    async def s(rig: RigctldRig, fake: FakeRigctld) -> None:
        with pytest.raises(RigConnectionError, match="timed out"):
            await rig._command("bogus")  # fake (like rigctld) never answers
        assert not rig.connected
        assert await rig.get_freq() == 145_000_000

    with_fake(s, timeout_s=0.3)


def test_connection_refused() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # closed after this block → nothing listening

    async def main() -> None:
        rig = RigctldRig("127.0.0.1", port, timeout_s=0.5)
        with pytest.raises(RigConnectionError, match="cannot connect"):
            await rig.get_freq()

    asyncio.run(main())


def test_raw_args() -> None:
    assert raw_args(b"FA;MD;", binary=False) == ("0", "FA;MD;")
    assert raw_args(b"\xfe\xfd", binary=True) == ("icom", "0xFE;0xFD")
    with pytest.raises(ValueError):
        raw_args(b"", binary=True)


def test_simulated_rig() -> None:
    out: list[str] = []

    async def main() -> None:
        rig = SimulatedRig(out.append)
        await rig.set_freq(7_030_000)
        await rig.set_mode("CW")
        assert await rig.get_freq() == 7_030_000

    asyncio.run(main())
    assert out == ["set_freq 7.030000 MHz", "set_mode CW"]


@pytest.mark.skipif(shutil.which("rigctld") is None, reason="rigctld not installed")
def test_real_rigctld_dummy() -> None:
    """Integration: real Hamlib rigctld with the dummy rig (model 1)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen(
        ["rigctld", "-m", "1", "-T", "127.0.0.1", "-t", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    pytest.fail("rigctld did not start")
                time.sleep(0.2)

        async def main() -> None:
            rig = RigctldRig("127.0.0.1", port)
            try:
                await rig.set_freq(7_030_000)
                await rig.set_mode("CW")
                st = await rig.status()
                assert (st.freq_hz, st.mode) == (7_030_000, "CW")
                await rig.send_morse("CQ TEST")
                await rig.send_raw(b"FA;", binary=False)
                await rig.send_raw(bytes.fromhex("FEFE94E003FD"), binary=True)
                assert await rig.get_freq() == 7_030_000  # stream still in sync
            finally:
                await rig.close()

        asyncio.run(main())
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.parametrize("reply", ["Reply: KM5CQ TEST};", "KM5CQ TEST};"])
def test_send_cmd_keeps_spaces_and_returns_answer(reply: str) -> None:
    async def s(rig: RigctldRig, fake: FakeRigctld) -> None:
        fake.send_cmd_reply = reply
        assert await rig.send_cmd("KM5CQ  TEST};KM5;") == "KM5CQ TEST};"
        assert fake.received[-1] == "+\\send_cmd KM5CQ TEST};KM5;"
        with pytest.raises(ValueError):
            await rig.send_cmd("KM5\x07;")

    with_fake(s)

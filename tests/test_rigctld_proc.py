import asyncio
import os
import shutil
import signal
from pathlib import Path

import pytest

from radio_keyer.rig import RigConnectionError
from radio_keyer.rigctld_proc import SpawnedRigctldRig, check_device, rigctld_argv

needs_rigctld = pytest.mark.skipif(shutil.which("rigctld") is None, reason="rigctld not installed")


def test_argv_with_and_without_baud() -> None:
    assert rigctld_argv("/usr/bin/rigctld", model=3073, device="/dev/ttyUSB0", baud=19200, port=5000) == [
        "/usr/bin/rigctld", "-m", "3073", "-r", "/dev/ttyUSB0", "-s", "19200",
        "-T", "127.0.0.1", "-t", "5000",
    ]
    assert "-s" not in rigctld_argv("rigctld", model=1, device="/dev/null", baud=None, port=5000)


def test_check_device(tmp_path: Path) -> None:
    missing = str(tmp_path / "ttyNOPE")
    with pytest.raises(RigConnectionError, match="not found"):
        check_device(missing, 1036)
    check_device(missing, 1)  # dummy rig ignores the port
    check_device("localhost:4532", 2)  # network rig: not a path

    dev = tmp_path / "ttyFAKE"
    dev.touch()
    check_device(str(dev), 1036)
    if os.geteuid() != 0:  # root bypasses permissions
        dev.chmod(0)
        with pytest.raises(RigConnectionError, match="dialout"):
            check_device(str(dev), 1036)


def test_missing_port_fails_before_spawn(tmp_path: Path) -> None:
    async def main() -> None:
        rig = SpawnedRigctldRig(model=1036, device=str(tmp_path / "ttyNOPE"))
        with pytest.raises(RigConnectionError, match="not found"):
            await rig.get_freq()
        assert not rig.running
        await rig.close()

    asyncio.run(main())


def test_missing_binary_is_connection_error() -> None:
    async def main() -> None:
        rig = SpawnedRigctldRig(model=1, device="/dev/null", binary="no-such-rigctld-xyz")
        with pytest.raises(RigConnectionError, match="not found"):
            await rig.get_freq()
        await rig.close()

    asyncio.run(main())


def test_child_exit_at_startup_is_reported(tmp_path: Path) -> None:
    fake = tmp_path / "rigctld"
    fake.write_text("#!/bin/sh\necho 'rig_open: error = boom' >&2\nexit 3\n")
    fake.chmod(0o755)

    async def main() -> None:
        rig = SpawnedRigctldRig(model=1, device="/dev/null", binary=str(fake))
        with pytest.raises(RigConnectionError, match=r"code 3.*boom"):
            await rig.get_freq()
        assert not rig.running
        await rig.close()

    asyncio.run(main())


@needs_rigctld
def test_dummy_rig_lifecycle_and_restart() -> None:
    async def main() -> None:
        rig = SpawnedRigctldRig(model=1, device="/dev/ttyNOPE")  # dummy: port unchecked
        try:
            assert not rig.running  # lazy
            await rig.set_freq(7_030_000)
            assert await rig.get_freq() == 7_030_000
            pid = rig.pid
            assert pid is not None

            os.kill(pid, signal.SIGKILL)  # rigctld dies (e.g. crash)
            for _ in range(50):
                if not rig.running:
                    break
                await asyncio.sleep(0.05)
            assert not rig.running
            assert (await rig.status()).freq_hz > 0  # fresh child started transparently
            assert rig.pid not in (None, pid)
        finally:
            await rig.close()
        assert not rig.running

    asyncio.run(main())

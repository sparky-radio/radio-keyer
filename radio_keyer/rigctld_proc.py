"""`serial` backend: the app runs its own private rigctld on localhost.

The child rigctld owns the serial port; we talk to it with the same protocol
as RigctldRig. It is started lazily on the first command, restarted if it dies,
and stopped on close().

Hamlib 4.5 note: with a missing/wrong serial port rigctld does NOT exit; it
listens but commands hang → our per-command timeout reports it, and the
'rig_open: error' line from its stderr is logged as a warning.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import socket
from collections import deque
from typing import Final

from radio_keyer.rig import DEFAULT_TIMEOUT_S, RigConnectionError, RigctldRig

log = logging.getLogger(__name__)

LOCALHOST: Final[str] = "127.0.0.1"
STARTUP_TIMEOUT_S: Final[float] = 10.0
STOP_TIMEOUT_S: Final[float] = 3.0
_POLL_S: Final[float] = 0.1
_STDERR_TAIL: Final[int] = 20
DUMMY_MODEL: Final[int] = 1
"""Hamlib dummy rig: ignores the serial port."""


def check_device(device: str, model: int) -> None:
    """Fail fast on a missing/inaccessible serial device (rigctld would just hang).

    Skipped for the dummy rig and for non-path devices (e.g. host:port for network rigs).
    """
    if model == DUMMY_MODEL or not device.startswith("/"):
        return
    if not os.path.exists(device):
        raise RigConnectionError(
            f"serial port {device} not found (radio off/unplugged? see `ls -l /dev/serial/by-id/`)"
        )
    if not os.access(device, os.R_OK | os.W_OK):
        raise RigConnectionError(
            f"no permission to open {device} (add your user to the 'dialout' group, then log in again)"
        )


def free_port(host: str = LOCALHOST) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        port: int = s.getsockname()[1]
        return port


def rigctld_argv(
    binary: str, *, model: int, device: str, baud: int | None, port: int
) -> list[str]:
    argv = [binary, "-m", str(model), "-r", device]
    if baud is not None:
        argv += ["-s", str(baud)]
    return argv + ["-T", LOCALHOST, "-t", str(port)]


class SpawnedRigctldRig(RigctldRig):
    def __init__(
        self,
        *,
        model: int,
        device: str,
        baud: int | None = None,
        binary: str = "rigctld",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        startup_timeout_s: float = STARTUP_TIMEOUT_S,
    ) -> None:
        super().__init__(LOCALHOST, 0, timeout_s=timeout_s)
        self._model = model
        self._device = device
        self._baud = baud
        self._binary = binary
        self._startup_timeout_s = startup_timeout_s
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self.running and self._proc is not None else None

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if not self.running:
            await super().close()  # stale connection to a dead child
            await self._start()
        return await super()._connect()

    async def close(self) -> None:
        await super().close()
        await self._stop()

    # --- process ----------------------------------------------------------

    async def _start(self) -> None:
        await self._stop()  # reap a dead child + its stderr reader
        check_device(self._device, self._model)
        binary = shutil.which(self._binary)
        if binary is None:
            raise RigConnectionError(
                f"'{self._binary}' not found; install Hamlib (e.g. apt install libhamlib-utils)"
            )
        self._port = free_port()
        argv = rigctld_argv(
            binary, model=self._model, device=self._device, baud=self._baud, port=self._port
        )
        self._stderr_tail.clear()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise RigConnectionError(f"cannot start rigctld: {exc}") from exc
        self._proc = proc
        self._stderr_task = asyncio.create_task(self._read_stderr(proc), name="rigctld-stderr")
        log.info("Started rigctld (pid %d): %s", proc.pid, " ".join(argv[1:]))

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._startup_timeout_s
        while True:
            if proc.returncode is not None:
                await self._stop()
                raise RigConnectionError(
                    f"rigctld exited with code {proc.returncode}{self._last_error()}"
                )
            try:
                _, writer = await asyncio.open_connection(LOCALHOST, self._port)
            except OSError:
                if loop.time() >= deadline:
                    await self._stop()
                    raise RigConnectionError(
                        f"rigctld did not start listening within {self._startup_timeout_s:g} s"
                        f"{self._last_error()}"
                    ) from None
                await asyncio.sleep(_POLL_S)
                continue
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()
            return

    async def _stop(self) -> None:
        proc, self._proc = self._proc, None
        task, self._stderr_task = self._stderr_task, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), STOP_TIMEOUT_S)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
            log.info("Stopped rigctld (pid %d)", proc.pid)
        elif proc is not None:
            log.warning("rigctld (pid %d) had exited with code %d", proc.pid, proc.returncode)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while raw := await proc.stderr.readline():
            text = raw.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            self._stderr_tail.append(text)
            if "error" in text.lower():
                log.warning("rigctld: %s", text)
            else:
                log.debug("rigctld: %s", text)

    def _last_error(self) -> str:
        for line in reversed(self._stderr_tail):
            if "error" in line.lower():
                return f" ({line})"
        return f" ({self._stderr_tail[-1]})" if self._stderr_tail else ""

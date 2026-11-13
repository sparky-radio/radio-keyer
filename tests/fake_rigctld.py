"""Minimal in-process rigctld emulator (extended '+' protocol) for tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class FakeRigctld:
    freq_hz: int = 145_000_000
    mode: str = "FM"
    passband_hz: int = 15_000
    received: list[str] = field(default_factory=list)
    morse_supported: bool = True
    send_raw_answer: str = "OK"
    _server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        sock = self._server.sockets[0]
        port: int = sock.getsockname()[1]
        return port

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                text = line.decode().rstrip("\n")
                self.received.append(text)
                reply = self._reply(text)
                if reply is not None:  # None = mimic rigctld: no answer to unknown commands
                    writer.write(reply.encode())
                    await writer.drain()
        finally:
            writer.close()

    def _reply(self, text: str) -> str | None:
        assert text.startswith("+\\"), text
        name, _, args = text[2:].partition(" ")
        argv = args.split()
        match name:
            case "get_freq":
                return f"get_freq:\nFrequency: {self.freq_hz}\nRPRT 0\n"
            case "set_freq":
                try:
                    self.freq_hz = int(argv[0])
                except (ValueError, IndexError):
                    return f"set_freq: {args}\nRPRT -1\n"
                return f"set_freq: {args}\nRPRT 0\n"
            case "get_mode":
                return f"get_mode:\nMode: {self.mode}\nPassband: {self.passband_hz}\nRPRT 0\n"
            case "set_mode":
                self.mode = argv[0]
                if int(argv[1]) != -1:
                    self.passband_hz = int(argv[1])
                return f"set_mode: {args}\nRPRT 0\n"
            case "send_morse":
                return f"send_morse:  {args}\nRPRT {0 if self.morse_supported else -11}\n"
            case "stop_morse":
                return f"stop_morse:\nRPRT {0 if self.morse_supported else -11}\n"
            case "send_raw":
                return f"send_raw: {args}\nSend raw answer: {self.send_raw_answer}\nRPRT 0\n"
            case _:
                return None

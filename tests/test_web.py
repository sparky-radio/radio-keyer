"""Web UI tests. The ASGI app is called in-process (no sockets, no httpx)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any

import pytest

from radio_keyer.config import ConfigError, parse_config_text
from radio_keyer.rig import Rig, SimulatedRig
from radio_keyer.runner import Runner
from radio_keyer.web import WebHub, create_app, same_origin

type Message = MutableMapping[str, Any]

CONFIG = """
station: {callsign: K5AQ}
global:
  keys:
    1: {long: {profile: next}}
profiles:
  main:
    label: Main
    keys:
      1: {cw: "CQ {call}"}
      2: [{freq: 7074000, mode: PKTUSB}, {cw: "TEST"}]
  other:
    keys:
      1: {band: 40m}
"""

HOST = "localhost:8073"
ORIGIN = f"http://{HOST}"


class Env:
    def __init__(self, tmp_path: Path, *, rig_factory: Callable[[Any], Rig] | None = None) -> None:
        self.path = tmp_path / "config.yaml"
        self.path.write_text(CONFIG, encoding="utf-8")
        self.out: list[str] = []
        self.rig = SimulatedRig(self.out.append)
        self.runner = Runner(parse_config_text(CONFIG), self.rig)
        self.hub = WebHub(
            self.runner,
            self.path,
            describe_rig=lambda c: f"rig {c.rig.rigctld.port}",
            rig_factory=rig_factory,
        )
        self.app = create_app(self.hub)

    async def request(
        self,
        method: str,
        path: str,
        body: object | None = None,
        *,
        host: str = HOST,
        origin: str | None = ORIGIN,
    ) -> tuple[int, Any]:
        headers: list[tuple[bytes, bytes]] = [(b"host", host.encode())]
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        payload = b""
        if body is not None:
            payload = json.dumps(body).encode()
            headers.append((b"content-type", b"application/json"))
        scope: Message = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8073),
        }
        sent = False

        async def receive() -> Message:
            nonlocal sent
            if sent:
                await asyncio.sleep(3600)
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}

        status = 0
        chunks: list[bytes] = []

        async def send(msg: Message) -> None:
            nonlocal status
            if msg["type"] == "http.response.start":
                status = msg["status"]
            elif msg["type"] == "http.response.body":
                chunks.append(msg.get("body", b""))

        await self.app(scope, receive, send)
        raw = b"".join(chunks)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw.decode(errors="replace")

    async def websocket(
        self, *, origin: str = ORIGIN, count: int = 3
    ) -> tuple[list[Message], list[Message]]:
        """Connect, collect `count` text messages, disconnect. Returns (data, control)."""
        inbox: asyncio.Queue[Message] = asyncio.Queue()
        inbox.put_nowait({"type": "websocket.connect"})
        data: list[Message] = []
        control: list[Message] = []
        scope: Message = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": "/ws",
            "raw_path": b"/ws",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", HOST.encode()), (b"origin", origin.encode())],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8073),
            "subprotocols": [],
        }

        async def receive() -> Message:
            return await inbox.get()

        async def send(msg: Message) -> None:
            if msg["type"] == "websocket.send":
                data.append(json.loads(msg["text"]))
                if len(data) >= count:
                    inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})
            else:
                control.append(msg)

        await asyncio.wait_for(self.app(scope, receive, send), 2)
        return data, control


def run[T](coro: Awaitable[T]) -> T:
    async def main() -> T:
        return await coro

    return asyncio.run(main())


# --- origin helpers -------------------------------------------------------


def test_same_origin() -> None:
    assert same_origin({"host": "localhost:8073"})
    assert same_origin({"host": "localhost:8073", "origin": "http://localhost:8073"})
    assert not same_origin({"host": "localhost:8073", "origin": "http://evil.example"})


# --- http -----------------------------------------------------------------


def test_state_shows_keys_and_global_fallback(tmp_path: Path) -> None:
    env = Env(tmp_path)
    status, state = run(env.request("GET", "/api/state"))
    assert status == 200
    assert state["active_profile"] == "main"
    assert [p["name"] for p in state["profiles"]] == ["main", "other"]
    key1 = state["keys"][0]
    assert key1["short"] == {"actions": ['cw "CQ {call}"'], "inherited": False}
    assert key1["long"] == {"actions": ["profile next"], "inherited": True}
    assert state["keys"][5]["short"] == {"actions": [], "inherited": False}


def test_index_served(tmp_path: Path) -> None:
    status, body = run(Env(tmp_path).request("GET", "/"))
    assert status == 200 and "<title>radio-keyer</title>" in body


def test_press_runs_binding(tmp_path: Path) -> None:
    env = Env(tmp_path)
    status, res = run(env.request("POST", "/api/press", {"position": 2, "kind": "short"}))
    assert status == 200 and res == {"ok": True}
    assert env.out == ["set_freq 7.074000 MHz", "set_mode PKTUSB", 'send_morse "TEST"']


def test_press_unassigned_and_invalid(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert run(env.request("POST", "/api/press", {"position": 6}))[1] == {"ok": False}
    assert run(env.request("POST", "/api/press", {"position": 7}))[0] == 422


def test_profile_switch(tmp_path: Path) -> None:
    env = Env(tmp_path)
    status, _ = run(env.request("POST", "/api/profile", {"name": "other"}))
    assert status == 200 and env.runner.active_profile == "other"
    assert run(env.request("POST", "/api/profile", {"name": "nope"}))[0] == 404


def test_cross_origin_post_refused(tmp_path: Path) -> None:
    env = Env(tmp_path)
    status, _ = run(
        env.request("POST", "/api/press", {"position": 1}, origin="http://evil.example")
    )
    assert status == 403 and env.out == []


def test_dns_rebinding_host_refused(tmp_path: Path) -> None:
    env = Env(tmp_path)
    status, _ = run(env.request("GET", "/api/config", host="evil.example:8073", origin=None))
    assert status == 403


def test_get_and_check_config(tmp_path: Path) -> None:
    env = Env(tmp_path)
    status, cfg = run(env.request("GET", "/api/config"))
    assert status == 200 and cfg["text"] == CONFIG
    assert run(env.request("POST", "/api/config/check", {"text": CONFIG}))[1] == {"ok": True}
    _, bad = run(env.request("POST", "/api/config/check", {"text": "profiles: {}"}))
    assert bad["ok"] is False and "at least one profile" in bad["error"]


def test_save_config_applies_and_keeps_backup(tmp_path: Path) -> None:
    env = Env(tmp_path)
    new_text = CONFIG.replace('cw: "CQ {call}"', 'cw: "TEST {call}"')
    status, res = run(env.request("PUT", "/api/config", {"text": new_text}))
    assert status == 200 and res == {"ok": True, "restart_required": []}
    assert env.path.read_text() == new_text
    assert (tmp_path / "config.yaml.bak").read_text() == CONFIG
    run(env.request("POST", "/api/press", {"position": 1}))
    assert env.out == ['send_morse "TEST K5AQ"']


def test_save_invalid_config_writes_nothing(tmp_path: Path) -> None:
    env = Env(tmp_path)
    status, res = run(env.request("PUT", "/api/config", {"text": "rig: {backend: nope}\n"}))
    assert status == 422 and res["ok"] is False
    assert env.path.read_text() == CONFIG
    assert not (tmp_path / "config.yaml.bak").exists()


def test_save_keeps_profile_or_falls_back(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.runner.set_profile("other")
    no_other = CONFIG.split("  other:")[0]
    assert run(env.request("PUT", "/api/config", {"text": no_other}))[0] == 200
    assert env.runner.active_profile == "main"


def test_save_rig_change_swaps_rig_and_flags_restart(tmp_path: Path) -> None:
    made: list[SimulatedRig] = []

    def factory(_config: Any) -> Rig:
        made.append(SimulatedRig(lambda _m: None))
        return made[-1]

    env = Env(tmp_path, rig_factory=factory)
    text = CONFIG + "rig: {rigctld: {port: 4600}}\nkeypad: {long_press_ms: 500}\n"
    status, res = run(env.request("PUT", "/api/config", {"text": text}))
    assert status == 200 and res["restart_required"] == ["keypad"]
    assert len(made) == 1 and env.runner.rig is made[0]


def test_save_unsupported_rig_writes_nothing(tmp_path: Path) -> None:
    def factory(_config: Any) -> Rig:
        raise ConfigError("serial not implemented")

    env = Env(tmp_path, rig_factory=factory)
    text = CONFIG + "rig: {rigctld: {port: 4600}}\n"
    status, res = run(env.request("PUT", "/api/config", {"text": text}))
    assert status == 422 and "serial not implemented" in res["error"]
    assert env.path.read_text() == CONFIG


# --- websocket ------------------------------------------------------------


def test_websocket_snapshot(tmp_path: Path) -> None:
    env = Env(tmp_path)

    async def main() -> list[Message]:
        await env.hub.poll_status()
        data, _ = await env.websocket(count=3)
        return data

    data = run(main())
    assert [m["type"] for m in data] == ["state", "status", "log_history"]
    assert data[1]["ok"] is True and data[1]["freq_hz"] == 14_030_000


def test_websocket_cross_origin_refused(tmp_path: Path) -> None:
    env = Env(tmp_path)
    data, control = run(env.websocket(origin="http://evil.example"))
    assert data == []
    assert control and control[0]["type"] == "websocket.close"


@pytest.mark.parametrize("level", ["INFO", "ERROR"])
def test_log_records_reach_clients(tmp_path: Path, level: str) -> None:
    import logging

    env = Env(tmp_path)
    queue = env.hub.subscribe()
    record = logging.LogRecord("radio_keyer.x", getattr(logging, level), "", 0, "hi %s", ("there",), None)
    env.hub.add_log(record)
    msg = queue.get_nowait()
    assert msg["type"] == "log" and msg["msg"] == "hi there" and msg["level"] == level

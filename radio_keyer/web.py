"""Local web UI: live rig status, key grid, profile switch, config editor.

Served by uvicorn inside the same asyncio loop as the keypad, so the web UI
and the keypad share one Runner (presses are serialized there).

Security: the UI can rewrite config.yaml and send raw CAT, so
- it binds to localhost by default (`web.host`),
- requests must carry a localhost Host header (blocks DNS rebinding),
- state-changing requests and WebSockets must be same-origin (blocks CSRF).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import tempfile
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import RequestResponseEndpoint

from radio_keyer.actions import Binding
from radio_keyer.config import NUM_KEYS, Config, ConfigError, parse_config_text
from radio_keyer.keypad import KeyPress, PressKind
from radio_keyer.rig import Rig, RigConnectionError, RigError
from radio_keyer.runner import Runner

log = logging.getLogger(__name__)

STATIC_DIR: Final[Path] = Path(__file__).with_name("static")
STATUS_INTERVAL_S: Final[float] = 1.0
LOG_HISTORY: Final[int] = 200
CLIENT_QUEUE_MAX: Final[int] = 500
MAX_CONFIG_BYTES: Final[int] = 512 * 1024
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})
_SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})

type Json = dict[str, object]


# --- request bodies -------------------------------------------------------


class PressIn(BaseModel):
    position: int = Field(ge=1, le=NUM_KEYS)
    kind: PressKind = PressKind.SHORT


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ConfigIn(BaseModel):
    text: str = Field(max_length=MAX_CONFIG_BYTES)


# --- hub ------------------------------------------------------------------


def describe_binding(binding: Binding | None) -> list[str]:
    return [a.describe() for a in binding] if binding else []


class WebHub:
    """Shared state between the Runner, the rig poller and connected browsers."""

    def __init__(
        self,
        runner: Runner,
        config_path: Path,
        *,
        describe_rig: Callable[[Config], str],
        rig_factory: Callable[[Config], Rig] | None = None,
        keypad_connected: Callable[[], bool] | None = None,
    ) -> None:
        self.runner = runner
        self.config_path = config_path
        self._describe_rig = describe_rig
        self._rig_factory = rig_factory
        self._keypad_connected = keypad_connected
        self._clients: set[asyncio.Queue[Json]] = set()
        self._log: deque[Json] = deque(maxlen=LOG_HISTORY)
        self._status: Json = {"type": "status", "ok": False, "error": "waiting for first poll"}
        self._save_lock = asyncio.Lock()
        self.restart_required: list[str] = []
        runner.on_profile_change = lambda _name: self.publish_state()

    # --- pub/sub ---------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[Json]:
        queue: asyncio.Queue[Json] = asyncio.Queue(maxsize=CLIENT_QUEUE_MAX)
        self._clients.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Json]) -> None:
        self._clients.discard(queue)

    @property
    def has_clients(self) -> bool:
        return bool(self._clients)

    def publish(self, msg: Json) -> None:
        for queue in self._clients:
            with contextlib.suppress(asyncio.QueueFull):  # slow client: drop
                queue.put_nowait(msg)

    def publish_state(self) -> None:
        self.publish(self.state())

    def add_log(self, record: logging.LogRecord) -> None:
        entry: Json = {
            "type": "log",
            "t": record.created,
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        self._log.append(entry)
        self.publish(entry)

    def snapshot(self) -> list[Json]:
        """Messages that bring a new client up to date."""
        return [self.state(), self._status, {"type": "log_history", "entries": list(self._log)}]

    # --- state -----------------------------------------------------------

    def state(self) -> Json:
        config = self.runner.config
        active = config.profile(self.runner.active_profile)
        keys: list[Json] = []
        for pos in range(1, NUM_KEYS + 1):
            entry: Json = {"position": pos}
            for kind in PressKind:
                own = active.binding(pos, kind)
                binding = own or config.global_profile.binding(pos, kind)
                entry[kind.value] = {
                    "actions": describe_binding(binding),
                    "inherited": own is None and binding is not None,
                }
            keys.append(entry)
        keypad: bool | None = self._keypad_connected() if self._keypad_connected else None
        return {
            "type": "state",
            "callsign": config.station.callsign,
            "rig": self._describe_rig(config),
            "keypad_connected": keypad,
            "profiles": [{"name": p.name, "label": p.label} for p in config.profiles],
            "active_profile": active.name,
            "keys": keys,
            "config_path": str(self.config_path),
            "restart_required": self.restart_required,
        }

    # --- rig status ------------------------------------------------------

    async def poll_status(self) -> None:
        try:
            st = await self.runner.rig.status()
            msg: Json = {
                "type": "status",
                "ok": True,
                "freq_hz": st.freq_hz,
                "mode": st.mode,
                "passband_hz": st.passband_hz,
            }
        except (RigConnectionError, RigError) as exc:
            msg = {"type": "status", "ok": False, "error": str(exc)}
        if msg != self._status:
            self._status = msg
            self.publish(msg)

    async def poll_loop(self, interval_s: float = STATUS_INTERVAL_S) -> None:
        """Poll the rig while at least one browser is connected."""
        last_keypad: bool | None = None
        while True:
            if self.has_clients:
                await self.poll_status()
                keypad = self._keypad_connected() if self._keypad_connected else None
                if keypad != last_keypad:
                    last_keypad = keypad
                    self.publish_state()
            await asyncio.sleep(interval_s)

    # --- actions ---------------------------------------------------------

    async def press(self, position: int, kind: PressKind) -> bool:
        log.info("Web UI press: key %d %s", position, kind.value)
        ok = await self.runner.handle_press(
            KeyPress(position=position, kind=kind, keycode=0, held_s=0.0)
        )
        await self.poll_status()
        return ok

    def set_profile(self, name: str) -> None:
        self.runner.set_profile(name)  # KeyError if unknown
        self.publish_state()

    # --- config ----------------------------------------------------------

    def read_config(self) -> str:
        return self.config_path.read_text(encoding="utf-8")

    def check_config(self, text: str) -> Config:
        return parse_config_text(text, source=self.config_path.name)

    async def save_config(self, text: str) -> list[str]:
        """Validate, write (keeping a .bak), and apply. Returns restart-required notes."""
        async with self._save_lock:
            new = self.check_config(text)
            old = self.runner.config
            new_rig: Rig | None = None
            if new.rig != old.rig and self._rig_factory is not None:
                new_rig = self._rig_factory(new)  # ConfigError if unsupported: nothing written
            _write_with_backup(self.config_path, text)
            log.info("Config saved to %s", self.config_path)

            self.runner.reload(new)
            if new_rig is not None:
                old_rig = await self.runner.replace_rig(new_rig)
                await old_rig.close()
                log.info("Rig → %s", self._describe_rig(new))

            notes = list(self.restart_required)
            if new.keypad != old.keypad and "keypad" not in notes:
                notes.append("keypad")
            if new.web != old.web and "web" not in notes:
                notes.append("web")
            self.restart_required = notes
            self.publish_state()
            await self.poll_status()
            return notes


def _write_with_backup(path: Path, text: str) -> None:
    """Atomically replace `path`; the previous contents go to `<path>.bak`."""
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        backup.write_bytes(path.read_bytes())
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class HubLogHandler(logging.Handler):
    """Forwards radio_keyer log records to the web UI activity log."""

    def __init__(self, hub: WebHub, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._hub = hub

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._hub.add_log(record)
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


# --- origin checks --------------------------------------------------------


def _hostname(netloc: str) -> str:
    return (urlsplit(f"//{netloc}").hostname or "").lower()


def host_allowed(headers: Mapping[str, str], allowed: frozenset[str] | None) -> bool:
    """`allowed` None means any Host (server bound to a wildcard address)."""
    host = headers.get("host")
    return host is not None and (allowed is None or _hostname(host) in allowed)


def same_origin(headers: Mapping[str, str]) -> bool:
    """True if there is no Origin (non-browser client) or it matches Host."""
    origin = headers.get("origin")
    if origin is None:
        return True
    host = headers.get("host")
    return host is not None and urlsplit(origin).netloc.lower() == host.lower()


# --- app ------------------------------------------------------------------


def create_app(hub: WebHub, *, allowed_hosts: frozenset[str] | None = LOOPBACK_HOSTS) -> FastAPI:
    app = FastAPI(title="radio-keyer", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not host_allowed(request.headers, allowed_hosts):
            return JSONResponse({"detail": "host not allowed"}, status_code=403)
        if request.method not in _SAFE_METHODS and not same_origin(request.headers):
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/state")
    async def get_state() -> Json:
        return hub.state()

    @app.post("/api/press")
    async def post_press(body: PressIn) -> Json:
        return {"ok": await hub.press(body.position, body.kind)}

    @app.post("/api/profile")
    async def post_profile(body: ProfileIn) -> Json:
        try:
            hub.set_profile(body.name)
        except KeyError:
            raise HTTPException(404, f"unknown profile {body.name!r}") from None
        return {"ok": True, "active_profile": hub.runner.active_profile}

    @app.get("/api/config")
    async def get_config() -> Json:
        try:
            return {"path": str(hub.config_path), "text": hub.read_config()}
        except OSError as exc:
            raise HTTPException(500, f"cannot read config: {exc.strerror}") from None

    @app.post("/api/config/check")
    async def check_config(body: ConfigIn) -> Json:
        try:
            hub.check_config(body.text)
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    @app.put("/api/config")
    async def put_config(body: ConfigIn) -> JSONResponse:
        try:
            notes = await hub.save_config(body.text)
        except ConfigError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        except OSError as exc:
            return JSONResponse({"ok": False, "error": f"cannot write config: {exc}"}, status_code=500)
        return JSONResponse({"ok": True, "restart_required": notes})

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        if not host_allowed(websocket.headers, allowed_hosts) or not same_origin(websocket.headers):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue = hub.subscribe()
        try:
            for msg in hub.snapshot():
                await websocket.send_json(msg)
            receiver = asyncio.create_task(_drain(websocket))
            try:
                while not receiver.done():
                    getter = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait(
                        {getter, receiver}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if getter in done:
                        await websocket.send_json(getter.result())
                    else:
                        getter.cancel()
            finally:
                receiver.cancel()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.unsubscribe(queue)

    return app


async def _drain(websocket: WebSocket) -> None:
    """Read (and ignore) client messages until it disconnects."""
    with contextlib.suppress(WebSocketDisconnect):
        while True:
            await websocket.receive_text()


# --- server ---------------------------------------------------------------


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind early so 'address in use' is a clean error, not a uvicorn exit."""
    family, kind, proto, _, addr = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
    sock = socket.socket(family, kind, proto)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(addr)
    except OSError:
        sock.close()
        raise
    return sock


def url_for(host: str, port: int) -> str:
    shown = "localhost" if host in LOOPBACK_HOSTS else host
    if ":" in shown:
        shown = f"[{shown}]"
    return f"http://{shown}:{port}/"


async def serve(hub: WebHub, sock: socket.socket, host: str) -> None:
    allowed: frozenset[str] | None
    if host in ("0.0.0.0", "::", ""):
        allowed = None
    else:
        allowed = LOOPBACK_HOSTS | {host.lower()}
    if host not in LOOPBACK_HOSTS:
        log.warning(
            "Web UI on %s is reachable from the network; it can edit config and send CAT", host
        )
    config = uvicorn.Config(
        create_app(hub, allowed_hosts=allowed),
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    await server.serve(sockets=[sock])

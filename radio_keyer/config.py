"""Load and validate config.yaml.

Errors are reported with their location, e.g.
    profiles.cw.keys.1.short[0]: unknown action 'cww'
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

import yaml

from radio_keyer.actions import (
    CW_FIELDS,
    HAMLIB_MODES,
    PROFILE_NEXT,
    PROFILE_PREV,
    Action,
    BandAction,
    Binding,
    CatAction,
    CwAction,
    CwMemoryAction,
    CwStopAction,
    CwVia,
    DelayAction,
    FreqAction,
    ModeAction,
    ProfileAction,
    cw_fields,
)
from radio_keyer.bands import BANDS, band_by_name
from radio_keyer.keypad import DEFAULT_DEVICE_PATH, DEFAULT_LONG_PRESS_S, PressKind

NUM_KEYS: Final[int] = 6
GLOBAL_PROFILE: Final[str] = "global"
_RESERVED_PROFILE_NAMES: Final[frozenset[str]] = frozenset({GLOBAL_PROFILE, PROFILE_NEXT, PROFILE_PREV})
_MAX_DELAY_MS: Final[int] = 60_000
_MAX_CW_MEMORY: Final[int] = 10


class ConfigError(ValueError):
    pass


class RigBackend(StrEnum):
    RIGCTLD = "rigctld"
    SERIAL = "serial"


# --- config model ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StationConfig:
    callsign: str = ""

    def cw_values(self) -> dict[str, str]:
        return {"call": self.callsign}


@dataclass(frozen=True, slots=True)
class RigctldConfig:
    host: str = "127.0.0.1"
    port: int = 4532


@dataclass(frozen=True, slots=True)
class SerialConfig:
    model: int | None = None
    """Hamlib rig model number (`rigctl -l`)."""
    port: str = "/dev/ttyUSB0"
    baud: int | None = None


@dataclass(frozen=True, slots=True)
class RigConfig:
    backend: RigBackend = RigBackend.RIGCTLD
    rigctld: RigctldConfig = field(default_factory=RigctldConfig)
    serial: SerialConfig = field(default_factory=SerialConfig)


@dataclass(frozen=True, slots=True)
class KeypadConfig:
    device: str = DEFAULT_DEVICE_PATH
    long_press_ms: int = int(DEFAULT_LONG_PRESS_S * 1000)
    grab: bool = True


@dataclass(frozen=True, slots=True)
class WebConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    """Keep on localhost: the UI can edit config and send raw CAT."""
    port: int = 8073


type KeyBindings = Mapping[int, Mapping[PressKind, Binding]]


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    label: str
    keys: KeyBindings

    def binding(self, position: int, kind: PressKind) -> Binding | None:
        return self.keys.get(position, {}).get(kind)


@dataclass(frozen=True, slots=True)
class Config:
    station: StationConfig
    rig: RigConfig
    keypad: KeypadConfig
    global_profile: Profile
    profiles: tuple[Profile, ...]
    """Ordered; the first is active at startup."""
    web: WebConfig = field(default_factory=WebConfig)

    @property
    def profile_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.profiles)

    def profile(self, name: str) -> Profile:
        for p in self.profiles:
            if p.name == name:
                return p
        raise KeyError(name)

    def binding(self, profile: str, position: int, kind: PressKind) -> Binding | None:
        """Binding for a press in `profile`, falling back to the global profile."""
        return self.profile(profile).binding(position, kind) or self.global_profile.binding(
            position, kind
        )

    def switch_profile(self, current: str, target: str) -> str:
        """Resolve a ProfileAction target ('next', 'prev' or a name) from `current`."""
        names = self.profile_names
        if target in (PROFILE_NEXT, PROFILE_PREV):
            step = 1 if target == PROFILE_NEXT else -1
            return names[(names.index(current) + step) % len(names)]
        if target not in names:
            raise KeyError(target)
        return target


_RIGCTLD: Final = RigctldConfig()
_SERIAL: Final = SerialConfig()
_KEYPAD: Final = KeypadConfig()
_WEB: Final = WebConfig()


# --- loading --------------------------------------------------------------


def load_config(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"{path}: file not found") from None
    except OSError as exc:
        raise ConfigError(f"{path}: {exc.strerror}") from None
    return parse_config_text(text, source=str(path))


def parse_config_text(text: str, source: str = "<config>") -> Config:
    try:
        raw: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: invalid YAML: {exc}") from None
    try:
        return parse_config(raw)
    except ConfigError as exc:
        raise ConfigError(f"{source}: {exc}") from None


def parse_config(raw: object) -> Config:
    top = _mapping(raw, "<top>")
    _check_keys(top, {"station", "rig", "keypad", "web", GLOBAL_PROFILE, "profiles"}, "<top>")

    station = _parse_station(top.get("station"), "station")
    rig = _parse_rig(top.get("rig"), "rig")
    keypad = _parse_keypad(top.get("keypad"), "keypad")
    web = _parse_web(top.get("web"), "web")

    ctx = _Context(station=station)
    global_profile = _parse_profile(GLOBAL_PROFILE, top.get(GLOBAL_PROFILE), GLOBAL_PROFILE, ctx)

    raw_profiles = _mapping(top.get("profiles"), "profiles", required=True)
    if not raw_profiles:
        raise ConfigError("profiles: at least one profile is required")
    profiles: list[Profile] = []
    for name, value in raw_profiles.items():
        path = f"profiles.{name}"
        if name in _RESERVED_PROFILE_NAMES:
            raise ConfigError(f"{path}: '{name}' is a reserved name")
        profiles.append(_parse_profile(name, value, path, ctx))

    names = {p.name for p in profiles}
    for target, where in ctx.profile_refs:
        if target not in names and target not in (PROFILE_NEXT, PROFILE_PREV):
            raise ConfigError(f"{where}: unknown profile '{target}' (have: {', '.join(names)})")

    return Config(
        station=station,
        rig=rig,
        keypad=keypad,
        global_profile=global_profile,
        profiles=tuple(profiles),
        web=web,
    )


@dataclass(slots=True)
class _Context:
    station: StationConfig
    profile_refs: list[tuple[str, str]] = field(default_factory=list)
    """(target, path) of every ProfileAction, validated once all profiles are known."""


# --- sections -------------------------------------------------------------


def _parse_station(raw: object, path: str) -> StationConfig:
    m = _mapping(raw, path)
    _check_keys(m, {"callsign"}, path)
    callsign = _opt_str(m, "callsign", path, "").strip().upper()
    return StationConfig(callsign=callsign)


def _parse_rig(raw: object, path: str) -> RigConfig:
    m = _mapping(raw, path)
    _check_keys(m, {"backend", "rigctld", "serial"}, path)
    backend_s = _opt_str(m, "backend", path, RigBackend.RIGCTLD.value)
    try:
        backend = RigBackend(backend_s)
    except ValueError:
        raise ConfigError(
            f"{path}.backend: '{backend_s}' is not one of: {', '.join(RigBackend)}"
        ) from None

    rp = f"{path}.rigctld"
    rm = _mapping(m.get("rigctld"), rp)
    _check_keys(rm, {"host", "port"}, rp)
    rigctld = RigctldConfig(
        host=_opt_str(rm, "host", rp, _RIGCTLD.host),
        port=_opt_int(rm, "port", rp, _RIGCTLD.port, lo=1, hi=65535),
    )

    sp = f"{path}.serial"
    sm = _mapping(m.get("serial"), sp)
    _check_keys(sm, {"model", "port", "baud"}, sp)
    serial = SerialConfig(
        model=_opt_int_or_none(sm, "model", sp, lo=1),
        port=_opt_str(sm, "port", sp, _SERIAL.port),
        baud=_opt_int_or_none(sm, "baud", sp, lo=300),
    )
    if backend is RigBackend.SERIAL and serial.model is None:
        raise ConfigError(f"{sp}.model: required when backend is 'serial' (see `rigctl -l`)")

    return RigConfig(backend=backend, rigctld=rigctld, serial=serial)


def _parse_keypad(raw: object, path: str) -> KeypadConfig:
    m = _mapping(raw, path)
    _check_keys(m, {"device", "long_press_ms", "grab"}, path)
    return KeypadConfig(
        device=_opt_str(m, "device", path, _KEYPAD.device),
        long_press_ms=_opt_int(m, "long_press_ms", path, _KEYPAD.long_press_ms, lo=50, hi=5000),
        grab=_opt_bool(m, "grab", path, _KEYPAD.grab),
    )


def _parse_web(raw: object, path: str) -> WebConfig:
    m = _mapping(raw, path)
    _check_keys(m, {"enabled", "host", "port"}, path)
    return WebConfig(
        enabled=_opt_bool(m, "enabled", path, _WEB.enabled),
        host=_opt_str(m, "host", path, _WEB.host),
        port=_opt_int(m, "port", path, _WEB.port, lo=1, hi=65535),
    )


def _parse_profile(name: str, raw: object, path: str, ctx: _Context) -> Profile:
    m = _mapping(raw, path)
    _check_keys(m, {"label", "keys"}, path)
    label = _opt_str(m, "label", path, name)
    kp = f"{path}.keys"
    raw_keys = _mapping(m.get("keys"), kp)
    keys: dict[int, dict[PressKind, Binding]] = {}
    for pos_s, entry in raw_keys.items():
        epath = f"{kp}.{pos_s}"
        if not pos_s.isdigit() or not 1 <= int(pos_s) <= NUM_KEYS:
            raise ConfigError(f"{epath}: key position must be 1-{NUM_KEYS}")
        keys[int(pos_s)] = _parse_key_entry(entry, epath, ctx)
    return Profile(name=name, label=label, keys=keys)


def _parse_key_entry(raw: object, path: str, ctx: _Context) -> dict[PressKind, Binding]:
    """`{short: ..., long: ...}`, or a bare action/list meaning `short`."""
    kinds = {k.value for k in PressKind}
    if isinstance(raw, dict) and raw and all(isinstance(k, str) and k in kinds for k in raw):
        m = _mapping(raw, path)
        return {
            PressKind(k): _parse_binding(v, f"{path}.{k}", ctx)
            for k, v in m.items()
            if v is not None
        }
    if raw is None:
        raise ConfigError(f"{path}: empty key entry")
    return {PressKind.SHORT: _parse_binding(raw, f"{path}.short", ctx)}


def _parse_binding(raw: object, path: str, ctx: _Context) -> Binding:
    """One action (mapping) or a series (list of mappings)."""
    if isinstance(raw, list):
        if not raw:
            raise ConfigError(f"{path}: empty action list")
        items: list[object] = list(raw)
        return tuple(_parse_action(a, f"{path}[{i}]", ctx) for i, a in enumerate(items))
    return (_parse_action(raw, path, ctx),)


# --- actions --------------------------------------------------------------

_ACTION_NAMES: Final[tuple[str, ...]] = (
    "cw", "cw_memory", "cw_stop", "band", "freq", "mode", "cat", "cat_hex", "profile", "delay_ms",
)


def _parse_action(raw: object, path: str, ctx: _Context) -> Action:
    m = _mapping(raw, path, required=True)
    if "freq" in m:
        kind, extras = "freq", {"mode"}
    elif "cw" in m:
        kind, extras = "cw", {"via"}
    else:
        if len(m) != 1:
            raise ConfigError(
                f"{path}: expected one action, got {sorted(m)} (use a list for a series)"
            )
        kind, extras = next(iter(m)), set[str]()
    if kind not in _ACTION_NAMES:
        raise ConfigError(f"{path}: unknown action '{kind}' (expected: {', '.join(_ACTION_NAMES)})")
    _check_keys(m, {kind} | extras, path)
    value = m[kind]
    vpath = f"{path}.{kind}"

    match kind:
        case "cw":
            text = _req_str(value, vpath)
            try:
                used = cw_fields(text)
            except ValueError as exc:
                raise ConfigError(f"{vpath}: {exc}") from None
            unknown = used - CW_FIELDS
            if unknown:
                raise ConfigError(
                    f"{vpath}: unknown placeholder(s) {sorted(unknown)} (allowed: {sorted(CW_FIELDS)})"
                )
            if "call" in used and not ctx.station.callsign:
                raise ConfigError(f"{vpath}: uses {{call}} but station.callsign is not set")
            via_s = _opt_str(m, "via", path, CwVia.CAT.value)
            try:
                via = CwVia(via_s)
            except ValueError:
                raise ConfigError(f"{path}.via: '{via_s}' is not one of: {', '.join(CwVia)}") from None
            return CwAction(text=text, via=via)
        case "cw_memory":
            return CwMemoryAction(slot=_req_int(value, vpath, lo=1, hi=_MAX_CW_MEMORY))
        case "cw_stop":
            if value is not True and value is not None:
                raise ConfigError(f"{vpath}: use 'cw_stop: true'")
            return CwStopAction()
        case "band":
            band = _req_str(value, vpath).lower()
            if band not in ("up", "down") and band_by_name(band) is None:
                names = ", ".join(b.name for b in BANDS)
                raise ConfigError(f"{vpath}: '{band}' is not up, down, or one of: {names}")
            return BandAction(band=band)
        case "freq":
            hz = _req_int(value, vpath, lo=1)
            mode = _opt_str(m, "mode", path, "") or None
            return FreqAction(hz=hz, mode=_check_mode(mode, f"{path}.mode") if mode else None)
        case "mode":
            return ModeAction(mode=_check_mode(_req_str(value, vpath), vpath))
        case "cat":
            text = _req_str(value, vpath)
            try:
                data = text.encode("ascii")
            except UnicodeEncodeError:
                raise ConfigError(f"{vpath}: must be ASCII (use cat_hex for binary)") from None
            return CatAction(data=data, source=text)
        case "cat_hex":
            text = _req_str(value, vpath)
            try:
                data = bytes.fromhex(text)
            except ValueError:
                raise ConfigError(f"{vpath}: invalid hex, e.g. 'FE FE 94 E0 03 FD'") from None
            return CatAction(data=data, source=text.upper(), is_hex=True)
        case "profile":
            target = _req_str(value, vpath)
            ctx.profile_refs.append((target, vpath))
            return ProfileAction(target=target)
        case "delay_ms":
            return DelayAction(ms=_req_int(value, vpath, lo=0, hi=_MAX_DELAY_MS))
        case _:
            raise AssertionError(kind)


def _check_mode(mode: str, path: str) -> str:
    up = mode.upper()
    if up not in HAMLIB_MODES:
        raise ConfigError(f"{path}: unknown mode '{mode}' (expected: {', '.join(sorted(HAMLIB_MODES))})")
    return up


# --- primitive helpers ----------------------------------------------------


def _tname(value: object) -> str:
    return "nothing" if value is None else type(value).__name__


def _mapping(raw: object, path: str, *, required: bool = False) -> dict[str, object]:
    if raw is None and not required:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping, got {_tname(raw)}")
    out: dict[str, object] = {}
    for k, v in raw.items():
        if isinstance(k, bool) or not isinstance(k, str | int):
            raise ConfigError(f"{path}: invalid key {k!r}")
        out[str(k)] = v
    return out


def _check_keys(m: Mapping[str, object], allowed: set[str], path: str) -> None:
    unknown = set(m) - allowed
    if unknown:
        raise ConfigError(
            f"{path}: unknown key(s) {sorted(unknown)} (allowed: {', '.join(sorted(allowed))})"
        )


def _req_str(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{path}: expected non-empty text, got {_tname(value)}")
    return value


def _req_int(value: object, path: str, *, lo: int | None = None, hi: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}: expected an integer, got {_tname(value)}")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        rng = f"{lo if lo is not None else '-inf'}..{hi if hi is not None else 'inf'}"
        raise ConfigError(f"{path}: {value} out of range {rng}")
    return value


def _opt_str(m: Mapping[str, object], key: str, path: str, default: str) -> str:
    value = m.get(key)
    return default if value is None else _req_str(value, f"{path}.{key}")


def _opt_int(
    m: Mapping[str, object], key: str, path: str, default: int, *, lo: int | None = None, hi: int | None = None
) -> int:
    value = m.get(key)
    return default if value is None else _req_int(value, f"{path}.{key}", lo=lo, hi=hi)


def _opt_int_or_none(
    m: Mapping[str, object], key: str, path: str, *, lo: int | None = None, hi: int | None = None
) -> int | None:
    value = m.get(key)
    return None if value is None else _req_int(value, f"{path}.{key}", lo=lo, hi=hi)


def _opt_bool(m: Mapping[str, object], key: str, path: str, default: bool) -> bool:
    value = m.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{path}.{key}: expected true/false, got {_tname(value)}")
    return value

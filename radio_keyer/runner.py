"""Executes key-press bindings against a Rig and tracks the active profile."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from radio_keyer.actions import (
    Action,
    BandAction,
    CatAction,
    CwAction,
    CwMemoryAction,
    CwStopAction,
    CwVia,
    DelayAction,
    FreqAction,
    ModeAction,
    ProfileAction,
)
from radio_keyer.bands import band_by_name, step_band
from radio_keyer import yaesu_keyer
from radio_keyer.config import Config, CwKeyer, DtrConfig
from radio_keyer.dtr_keyer import DtrKeyer, KeyerError, SerialKeyLine
from radio_keyer.keypad import KeyPress
from radio_keyer.rig import Rig, RigConnectionError, RigError

log = logging.getLogger(__name__)


class UnsupportedActionError(Exception):
    """Action is valid config but not implemented for this rig/backend yet."""


type KeyerFactory = Callable[[DtrConfig], DtrKeyer]


def serial_dtr_keyer(dc: DtrConfig) -> DtrKeyer:
    """DtrKeyer on the configured port (opened on the first `cw:`)."""
    if not dc.port:
        raise UnsupportedActionError("CW via DTR needs rig.dtr.port")
    return DtrKeyer(lambda: SerialKeyLine(dc.port, dc.line), dc.wpm)


class Runner:
    def __init__(
        self,
        config: Config,
        rig: Rig,
        *,
        keyer_factory: KeyerFactory = serial_dtr_keyer,
        on_profile_change: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        self._rig = rig
        self._keyer_factory = keyer_factory
        self._dtr: DtrKeyer | None = None
        self._cw_values = config.station.cw_values()
        self.on_profile_change = on_profile_change
        self._lock = asyncio.Lock()
        self.active_profile: str = config.profiles[0].name

    @property
    def config(self) -> Config:
        return self._config

    @property
    def rig(self) -> Rig:
        return self._rig

    def reload(self, config: Config) -> None:
        """Use a new config; keep the active profile if it still exists."""
        if config.rig.dtr != self._config.rig.dtr:
            self.close_keyer()
        self._config = config
        self._cw_values = config.station.cw_values()
        if self.active_profile not in config.profile_names:
            self._set_profile(config.profiles[0].name)

    def close_keyer(self) -> None:
        """Stop and release the DTR keyer (reopened on the next DTR `cw:`)."""
        keyer, self._dtr = self._dtr, None
        if keyer is not None:
            keyer.close()

    def _dtr_keyer(self) -> DtrKeyer:
        if self._dtr is None:
            self._dtr = self._keyer_factory(self._config.rig.dtr)
        return self._dtr

    async def replace_rig(self, rig: Rig) -> Rig:
        """Swap in a new rig between presses; returns the old one (caller closes it)."""
        async with self._lock:
            old, self._rig = self._rig, rig
            return old

    def set_profile(self, name: str) -> None:
        """Switch to a profile by name. Raises KeyError if unknown."""
        self._set_profile(self._config.switch_profile(self.active_profile, name))

    def _set_profile(self, name: str) -> None:
        if name == self.active_profile:
            return
        self.active_profile = name
        log.info("Profile → %s (%s)", name, self._config.profile(name).label)
        if self.on_profile_change is not None:
            self.on_profile_change(name)

    async def handle_press(self, press: KeyPress) -> bool:
        """Run the binding for `press`. Returns False if unassigned or a step failed.

        Presses are serialized: a series (e.g. with delay_ms) finishes before
        the next press, whether it came from the keypad or the web UI.
        """
        async with self._lock:
            return await self._handle_press(press)

    async def _handle_press(self, press: KeyPress) -> bool:
        binding = self._config.binding(self.active_profile, press.position, press.kind)
        where = f"[{self.active_profile}] key {press.position} {press.kind.value}"
        if binding is None:
            log.info("%s: unassigned", where)
            return False
        for i, action in enumerate(binding, start=1):
            step = f" step {i}/{len(binding)}" if len(binding) > 1 else ""
            log.info("%s%s: %s", where, step, action.describe())
            try:
                await self.run_action(action)
            except (
                RigError,
                RigConnectionError,
                UnsupportedActionError,
                KeyerError,
                ValueError,
            ) as exc:
                skipped = len(binding) - i
                log.error(
                    "%s%s failed: %s%s",
                    where,
                    step,
                    exc,
                    f" (skipping {skipped} remaining)" if skipped else "",
                )
                return False
        return True

    async def run_action(self, action: Action) -> None:
        rig = self._rig
        rc = self._config.rig
        yaesu = rc.cw_keyer is CwKeyer.YAESU
        match action:
            case CwAction():
                if action.via is CwVia.WINKEYER:
                    raise UnsupportedActionError(f"cw via {action.via} is not implemented yet")
                text = action.render(self._cw_values)
                if action.via is CwVia.DTR or rc.cw_keyer is CwKeyer.DTR:
                    self._dtr_keyer().send(text)  # queued; returns at once
                elif yaesu:
                    await yaesu_keyer.send_text(rig, text, scratch=rc.cw_scratch_memory)
                else:
                    await rig.send_morse(text)
            case CwMemoryAction():
                if not yaesu:
                    raise UnsupportedActionError(
                        "cw_memory needs rig.cw_keyer: yaesu (Hamlib has no generic command)"
                    )
                await yaesu_keyer.play_memory(rig, action.slot)
            case CwStopAction():
                if self._dtr is not None:
                    self._dtr.stop()
                if rc.cw_keyer is CwKeyer.HAMLIB:
                    await rig.stop_morse()
                elif yaesu and self._dtr is None:
                    raise UnsupportedActionError(
                        "the Yaesu memory keyer has no CAT stop command; touch the paddle"
                    )
            case BandAction():
                if action.band in ("up", "down"):
                    current = await rig.get_freq()
                    band = step_band(current, 1 if action.band == "up" else -1)
                else:
                    found = band_by_name(action.band)
                    if found is None:
                        raise ValueError(f"unknown band {action.band!r}")
                    band = found
                await rig.set_freq(band.default_hz)
            case FreqAction():
                await rig.set_freq(action.hz)
                if action.mode:
                    await rig.set_mode(action.mode)
            case ModeAction():
                await rig.set_mode(action.mode)
            case CatAction():
                answer = await rig.send_raw(action.data, binary=action.is_hex)
                if answer:
                    log.info("CAT answer: %s", answer)
            case ProfileAction():
                self.set_profile(action.target)
            case DelayAction():
                await asyncio.sleep(action.ms / 1000)

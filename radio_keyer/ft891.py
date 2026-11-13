"""FT-891 operating presets (CW / Data / SSB ...) applied over CAT.

Command formats from the FT-891 CAT Operation Reference Book (1909-C). Every
setting is written, then read back and compared, so a value the radio rejects
(e.g. one not valid in the current mode) is reported instead of silently lost.
Mode and filter width go through Hamlib's set_mode (it maps width to NA/SH).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Final

from radio_keyer.rig import PASSBAND_NOCHANGE, Rig

log = logging.getLogger(__name__)


class Agc(StrEnum):
    OFF = "off"
    FAST = "fast"
    MID = "mid"
    SLOW = "slow"
    AUTO = "auto"


class Preamp(StrEnum):
    IPO = "ipo"
    AMP = "amp"


class DataPtt(StrEnum):
    DAKY = "daky"
    RTS = "rts"
    DTR = "dtr"


POWER_W: Final[tuple[int, int]] = (5, 100)
KEY_SPEED_WPM: Final[tuple[int, int]] = (4, 60)
LEVEL: Final[tuple[int, int]] = (0, 100)
WIDTH_HZ: Final[tuple[int, int]] = (50, 3200)

_AGC_SET: Final[dict[Agc, str]] = {Agc.OFF: "0", Agc.FAST: "1", Agc.MID: "2", Agc.SLOW: "3", Agc.AUTO: "4"}
# Read answers: AUTO reads back as AUTO-FAST/MID/SLOW (4-6).
_AGC_READ: Final[dict[Agc, tuple[str, ...]]] = {
    Agc.OFF: ("0",), Agc.FAST: ("1",), Agc.MID: ("2",), Agc.SLOW: ("3",), Agc.AUTO: ("4", "5", "6"),
}  # fmt: skip
_DATA_PTT: Final[dict[DataPtt, str]] = {DataPtt.DAKY: "0", DataPtt.RTS: "1", DataPtt.DTR: "2"}


@dataclass(frozen=True, slots=True)
class Preset:
    name: str
    mode: str | None = None
    width: int | None = None
    """Filter width in Hz (Hamlib passband)."""
    power: int | None = None
    """TX power in W (5-100)."""
    agc: Agc | None = None
    preamp: Preamp | None = None
    att: bool | None = None
    nb: bool | None = None
    nr: bool | None = None
    key_speed: int | None = None
    """Internal keyer WPM (4-60)."""
    break_in: bool | None = None
    mic_gain: int | None = None
    processor: bool | None = None
    processor_level: int | None = None
    data_ptt: DataPtt | None = None
    """Menu 08-10 DATA PTT SELECT."""

    def settings(self) -> dict[str, object]:
        """The fields that are set, in apply order (name excluded)."""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name != "name" and getattr(self, f.name) is not None
        }

    def describe(self) -> str:
        parts = [f"{k}={_show(v)}" for k, v in self.settings().items()]
        return f"preset {self.name}" + (f" ({', '.join(parts)})" if parts else "")


def _show(value: object) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


@dataclass(frozen=True, slots=True)
class CatSetting:
    label: str
    write: str
    read: str
    accept: tuple[str, ...]
    """Read answers that mean the setting took."""


def _onoff(label: str, cmd: str, on: bool) -> CatSetting:
    """P1 fixed 0, P2 0/1 (NB, NR, RA, PA-style commands)."""
    v = "1" if on else "0"
    return CatSetting(label, f"{cmd}0{v};", f"{cmd}0;", (f"{cmd}0{v};",))


def _level(label: str, cmd: str, value: int) -> CatSetting:
    """Three-digit P1 (PC, KS, MG, PL)."""
    return CatSetting(label, f"{cmd}{value:03d};", f"{cmd};", (f"{cmd}{value:03d};",))


def cat_settings(p: Preset) -> list[CatSetting]:
    """CAT writes for everything except mode/width, in a stable order."""
    out: list[CatSetting] = []
    if p.power is not None:
        out.append(_level("power", "PC", p.power))
    if p.agc is not None:
        out.append(
            CatSetting(
                "agc", f"GT0{_AGC_SET[p.agc]};", "GT0;", tuple(f"GT0{v};" for v in _AGC_READ[p.agc])
            )
        )
    if p.preamp is not None:
        out.append(_onoff("preamp", "PA", p.preamp is Preamp.AMP))
    if p.att is not None:
        out.append(_onoff("att", "RA", p.att))
    if p.nb is not None:
        out.append(_onoff("nb", "NB", p.nb))
    if p.nr is not None:
        out.append(_onoff("nr", "NR", p.nr))
    if p.key_speed is not None:
        out.append(_level("key_speed", "KS", p.key_speed))
    if p.break_in is not None:
        v = "1" if p.break_in else "0"
        out.append(CatSetting("break_in", f"BI{v};", "BI;", (f"BI{v};",)))
    if p.mic_gain is not None:
        out.append(_level("mic_gain", "MG", p.mic_gain))
    if p.processor is not None:
        out.append(_onoff("processor", "PR", p.processor))
    if p.processor_level is not None:
        out.append(_level("processor_level", "PL", p.processor_level))
    if p.data_ptt is not None:
        v = _DATA_PTT[p.data_ptt]
        out.append(CatSetting("data_ptt", f"EX0810{v};", "EX0810;", (f"EX0810{v};",)))
    return out


class PresetError(Exception):
    """Some settings of a preset were not applied."""


async def apply_preset(rig: Rig, preset: Preset) -> None:
    """Apply `preset`; raise PresetError listing every setting the radio didn't take."""
    if preset.mode is not None or preset.width is not None:
        mode = preset.mode or (await rig.get_mode())[0]
        await rig.set_mode(mode, preset.width if preset.width is not None else PASSBAND_NOCHANGE)
    failed: list[str] = []
    for s in cat_settings(preset):
        await rig.send_raw(s.write.encode("ascii"), binary=False)
        answer = (await rig.send_raw(s.read.encode("ascii"), binary=False, terminator=";")).strip()
        if answer not in s.accept:
            failed.append(f"{s.label} ({s.write} → read {answer or 'nothing'})")
        else:
            log.debug("preset %s: %s OK (%s)", preset.name, s.label, answer)
    if failed:
        raise PresetError(f"preset {preset.name}: not applied: {'; '.join(failed)}")

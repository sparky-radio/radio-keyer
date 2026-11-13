import asyncio

import pytest

from radio_keyer.config import ConfigError, parse_config_text
from radio_keyer.ft891 import Agc, DataPtt, Preamp, Preset, PresetError, apply_preset, cat_settings
from radio_keyer.rig import SimulatedRig

PRESETS = """
presets:
  cw:   {mode: CW, width: 500, power: 50, agc: fast, key_speed: 25, break_in: on, preamp: amp}
  data: {mode: PKTUSB, width: 3000, power: 30, agc: off, nb: off, nr: off, processor: off, data_ptt: dtr}
  ssb:  {mode: USB, width: 2400, power: 100, agc: auto, mic_gain: 50, processor: on, processor_level: 40, att: off}
profiles:
  main:
    keys:
      1: {preset: cw}
      2: {preset: data}
      3: {preset: ssb}
"""


def test_parse_presets() -> None:
    cfg = parse_config_text(PRESETS)
    cw, data, ssb = (cfg.presets[n] for n in ("cw", "data", "ssb"))
    assert (cw.mode, cw.width, cw.power, cw.agc, cw.key_speed, cw.break_in, cw.preamp) == (
        "CW", 500, 50, Agc.FAST, 25, True, Preamp.AMP,
    )
    assert data.agc is Agc.OFF and data.data_ptt is DataPtt.DTR and data.nb is False
    assert ssb.processor is True and ssb.att is False and ssb.mic_gain == 50
    assert cfg.profile("main").binding(1, next(iter(cfg.profile("main").keys[1]))) is not None


@pytest.mark.parametrize(
    "bad",
    [
        "presets: {cw: {power: 200}}\n",
        "presets: {cw: {power: 2}}\n",
        "presets: {cw: {agc: turbo}}\n",
        "presets: {cw: {mode: FOO}}\n",
        "presets: {cw: {keyspeed: 20}}\n",
        "presets: {cw: {}}\n",
        "presets: {cw: {nb: maybe}}\n",
        "presets: {cw: {data_ptt: usb}}\n",
    ],
)
def test_bad_presets(bad: str) -> None:
    with pytest.raises(ConfigError):
        parse_config_text(bad + "profiles: {main: {keys: {1: {band: up}}}}\n")


def test_unknown_preset_reference() -> None:
    with pytest.raises(ConfigError, match="unknown preset 'cw'"):
        parse_config_text("profiles: {main: {keys: {1: {preset: cw}}}}\n")


def test_cat_commands() -> None:
    p = Preset(
        name="x", power=5, agc=Agc.AUTO, preamp=Preamp.IPO, att=True, nb=True, nr=False,
        key_speed=60, break_in=False, mic_gain=0, processor=True, processor_level=100,
        data_ptt=DataPtt.RTS,
    )  # fmt: skip
    writes = [s.write for s in cat_settings(p)]
    assert writes == [
        "PC005;", "GT04;", "PA00;", "RA01;", "NB01;", "NR00;", "KS060;", "BI0;",
        "MG000;", "PR01;", "PL100;", "EX08101;",
    ]  # fmt: skip
    agc = cat_settings(Preset(name="a", agc=Agc.AUTO))[0]
    assert (agc.read, agc.accept) == ("GT0;", ("GT04;", "GT05;", "GT06;"))


def test_apply_on_simulated_rig() -> None:
    out: list[str] = []
    rig = SimulatedRig(out.append, mode="USB")
    preset = parse_config_text(PRESETS).presets["cw"]
    asyncio.run(apply_preset(rig, preset))
    assert out[0] == "set_mode CW" and rig.passband_hz == 500
    assert "send_raw 0 PC050;" in out and "send_raw ; PC;" in out
    assert "send_raw 0 BI1;" in out


class RejectingRig(SimulatedRig):
    """Rejects processor writes (answers the read with the old value)."""

    async def send_raw(self, data: bytes, *, binary: bool, terminator: str | None = None) -> str:
        if data.startswith(b"PR0") and terminator != ";":
            return "?;"
        answer = await super().send_raw(data, binary=binary, terminator=terminator)
        return "PR00;" if data == b"PR0;" else answer


def test_rejected_setting_reported_but_rest_applied() -> None:
    out: list[str] = []
    preset = Preset(name="ssb", processor=True, power=100)
    with pytest.raises(PresetError, match=r"processor \(PR01; → read PR00;\)"):
        asyncio.run(apply_preset(RejectingRig(out.append), preset))
    assert "send_raw 0 PC100;" in out


def test_width_only_keeps_current_mode() -> None:
    out: list[str] = []
    rig = SimulatedRig(out.append, mode="CW", passband_hz=2400)
    asyncio.run(apply_preset(rig, Preset(name="w", width=500)))
    assert rig.mode == "CW" and rig.passband_hz == 500

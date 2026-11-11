from pathlib import Path

import pytest

from radio_keyer.actions import (
    BandAction,
    CatAction,
    CwAction,
    CwVia,
    DelayAction,
    FreqAction,
    ProfileAction,
)
from radio_keyer.config import ConfigError, RigBackend, load_config, parse_config_text
from radio_keyer.keypad import PressKind

REPO = Path(__file__).resolve().parent.parent

MINIMAL = """
station: {callsign: n0call}
global:
  keys:
    1: {long: {profile: next}}
    6: {cat: "FA;"}
profiles:
  a:
    keys:
      1: {cw: "CQ {call}"}
      2:
        short: [{freq: 14030000, mode: cw}, {delay_ms: 200}, {cw: "TEST", via: dtr}]
        long: {band: UP}
      6: {cat_hex: "fe fe 94 e0 03 fd"}
  b:
    label: Bee
    keys:
      1: {profile: a}
"""


def test_shipped_config_is_valid() -> None:
    cfg = load_config(REPO / "config.example.yaml")
    assert cfg.profiles  # parses and has at least one profile


def test_parse_minimal() -> None:
    cfg = parse_config_text(MINIMAL)
    assert cfg.station.callsign == "N0CALL"
    assert cfg.rig.backend is RigBackend.RIGCTLD
    assert cfg.rig.rigctld.port == 4532
    assert cfg.keypad.long_press_ms == 600
    assert cfg.profile("a").label == "a"

    assert cfg.binding("a", 1, PressKind.SHORT) == (CwAction("CQ {call}"),)
    series = cfg.binding("a", 2, PressKind.SHORT)
    assert series == (
        FreqAction(14_030_000, "CW"),
        DelayAction(200),
        CwAction("TEST", CwVia.DTR),
    )
    assert cfg.binding("a", 2, PressKind.LONG) == (BandAction("up"),)


def test_global_fallback_and_override() -> None:
    cfg = parse_config_text(MINIMAL)
    # a defines 1 short but not 1 long -> global long
    assert cfg.binding("a", 1, PressKind.LONG) == (ProfileAction("next"),)
    # a overrides 6 short
    action = cfg.binding("a", 6, PressKind.SHORT)
    assert action is not None and isinstance(action[0], CatAction) and action[0].is_hex
    assert action[0].data == bytes.fromhex("FEFE94E003FD")
    # b falls back to global 6
    assert cfg.binding("b", 6, PressKind.SHORT) == (CatAction(b"FA;", "FA;"),)
    assert cfg.binding("b", 3, PressKind.SHORT) is None


def test_switch_profile() -> None:
    cfg = parse_config_text(MINIMAL)
    assert cfg.switch_profile("a", "next") == "b"
    assert cfg.switch_profile("b", "next") == "a"
    assert cfg.switch_profile("a", "prev") == "b"
    assert cfg.switch_profile("a", "b") == "b"


def test_cw_render() -> None:
    cfg = parse_config_text(MINIMAL)
    binding = cfg.binding("a", 1, PressKind.SHORT)
    assert binding is not None
    action = binding[0]
    assert isinstance(action, CwAction)
    assert action.render(cfg.station.cw_values()) == "CQ N0CALL"


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        ("profiles: {}", "at least one profile"),
        ("profiles: {a: {keys: {7: {mode: CW}}}}", "profiles.a.keys.7: key position"),
        ("profiles: {a: {keys: {1: {cww: x}}}}", "unknown action 'cww'"),
        ("profiles: {a: {keys: {1: {mode: XYZ}}}}", "unknown mode 'XYZ'"),
        ("profiles: {a: {keys: {1: {band: 11m}}}}", "'11m' is not"),
        ("profiles: {a: {keys: {1: {profile: zz}}}}", "unknown profile 'zz'"),
        ("profiles: {a: {keys: {1: {cw: 'CQ {call}'}}}}", "station.callsign is not set"),
        ("station: {callsign: X}\nprofiles: {a: {keys: {1: {cw: 'CQ {name}'}}}}", "unknown placeholder"),
        ("profiles: {a: {keys: {1: {cat_hex: 'zz'}}}}", "invalid hex"),
        ("profiles: {a: {keys: {1: {mode: CW, band: up}}}}", "use a list for a series"),
        ("profiles: {a: {keys: {1: {short: []}}}}", "empty action list"),
        ("profiles: {global: {}}", "reserved name"),
        ("rig: {backend: serial}\nprofiles: {a: {}}", "rig.serial.model: required"),
        ("rig: {backend: telnet}\nprofiles: {a: {}}", "rig.backend"),
        ("keypad: {grab: yes please}\nprofiles: {a: {}}", "keypad.grab"),
        ("bogus: 1\nprofiles: {a: {}}", "unknown key(s) ['bogus']"),
        ("profiles: {a: {keys: {1: {delay_ms: true}}}}", "expected an integer"),
        ("profiles: [", "invalid YAML"),
    ],
)
def test_errors(snippet: str, message: str) -> None:
    with pytest.raises(ConfigError, match=None) as exc_info:
        parse_config_text(snippet)
    assert message in str(exc_info.value)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="file not found"):
        load_config(tmp_path / "nope.yaml")

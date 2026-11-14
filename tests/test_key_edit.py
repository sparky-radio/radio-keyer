"""Per-key YAML rewrite: only the edited key changes, comments survive."""

from __future__ import annotations

from pathlib import Path

import pytest

from radio_keyer.config import parse_config_text
from radio_keyer.key_edit import KeyEditError, UnknownProfileError, read_key, write_key
from radio_keyer.keypad import PressKind

TEXT = """\
station:
  callsign: K5AQ   # me

# Fallback for every profile.
global:
  keys:
    1:
      long: {profile: next}

# The first profile is active at startup.
profiles:
  cw:
    label: CW   # label comment
    keys:
      1: {cw: "CQ {call}"}
      3:
        short: {cw: "AGN?"}
        long: {cw_stop: true}

  # Bands profile comment
  bands:
    keys: {1: {band: 40m}}
  empty:
"""


def test_read_bare_and_short_long() -> None:
    assert read_key(TEXT, "cw", 1) == {"short": [{"cw": "CQ {call}"}], "long": None}
    assert read_key(TEXT, "cw", 3) == {
        "short": [{"cw": "AGN?"}],
        "long": [{"cw_stop": True}],
    }
    assert read_key(TEXT, "cw", 2) == {"short": None, "long": None}
    assert read_key(TEXT, "global", 1) == {"short": None, "long": [{"profile": "next"}]}
    assert read_key(TEXT, "empty", 1) == {"short": None, "long": None}


def test_read_errors() -> None:
    with pytest.raises(UnknownProfileError):
        read_key(TEXT, "nope", 1)
    with pytest.raises(KeyEditError):
        read_key(TEXT, "cw", 7)


def test_replace_keeps_everything_else() -> None:
    out = write_key(TEXT, "cw", 1, [{"cw": "TEST {call}"}], None)
    assert out == TEXT.replace('{cw: "CQ {call}"}', '{cw: "TEST {call}"}')


def test_series_and_long() -> None:
    out = write_key(
        TEXT, "cw", 1,
        [{"freq": 14030000, "mode": "CW"}, {"delay_ms": 300}, {"cw": "CQ"}],
        [{"cw_stop": True}],
    )  # fmt: skip
    assert (
        "      1:\n"
        "        short:\n"
        "          - {freq: 14030000, mode: CW}\n"
        "          - {delay_ms: 300}\n"
        '          - {cw: "CQ"}\n'
        "        long: {cw_stop: true}\n"
        "      3:\n"
    ) in out
    cfg = parse_config_text(out)
    assert len(cfg.binding("cw", 1, PressKind.SHORT) or ()) == 3


def test_insert_new_key_in_order() -> None:
    out = write_key(TEXT, "cw", 2, [{"band": "20m"}], None)
    assert '      1: {cw: "CQ {call}"}\n      2: {band: 20m}\n      3:\n' in out
    assert out.replace("      2: {band: 20m}\n", "") == TEXT


def test_trailing_comment_kept_when_last_key_changes() -> None:
    # "# Bands profile comment" and the blank line are stored after cw key 3.
    out = write_key(TEXT, "cw", 3, [{"cw": "NR?"}], None)
    assert '      3: {cw: "NR?"}\n\n  # Bands profile comment\n  bands:' in out
    out = write_key(TEXT, "cw", 5, [{"band": "up"}], None)
    assert "        long: {cw_stop: true}\n      5: {band: up}\n\n  # Bands profile comment\n" in out
    out = write_key(TEXT, "global", 1, None, [{"profile": "prev"}])
    assert "      long: {profile: prev}\n\n# The first profile is active at startup.\n" in out


def test_remove_key() -> None:
    out = write_key(TEXT, "cw", 3, None, None)
    assert "AGN" not in out
    assert '      1: {cw: "CQ {call}"}\n\n  # Bands profile comment\n' in out
    assert write_key(TEXT, "cw", 2, None, None) == TEXT


def test_flow_style_profile_and_empty_profile() -> None:
    out = write_key(TEXT, "bands", 2, [{"band": "20m"}], None)
    assert parse_config_text(out).binding("bands", 2, PressKind.SHORT) is not None
    assert parse_config_text(out).binding("bands", 1, PressKind.SHORT) is not None
    out = write_key(TEXT, "empty", 1, [{"mode": "CW"}], None)
    assert read_key(out, "empty", 1) == {"short": [{"mode": "CW"}], "long": None}


def test_global_created_when_missing() -> None:
    text = "profiles:\n  a:\n    keys: {}\n"
    assert write_key(text, "global", 1, None, None) == text
    out = write_key(text, "global", 1, [{"cw_stop": True}], None)
    assert out.index("global:") < out.index("profiles:")
    assert read_key(out, "global", 1) == {"short": [{"cw_stop": True}], "long": None}


def test_string_key_positions() -> None:
    text = "profiles:\n  a:\n    keys:\n      '1': {mode: CW}\n"
    assert read_key(text, "a", 1)["short"] == [{"mode": "CW"}]
    out = write_key(text, "a", 1, [{"mode": "USB"}], None)
    assert read_key(out, "a", 1)["short"] == [{"mode": "USB"}]
    assert out.count("1") == 1


def test_example_config_round_trips_unchanged() -> None:
    example = (Path(__file__).parent.parent / "config.example.yaml").read_text()
    for profile in ("global", "cw_pota", "cw", "bands", "ft8"):
        for pos in range(1, 7):
            k = read_key(example, profile, pos)
            out = write_key(example, profile, pos, k["short"], k["long"])
            parse_config_text(out)
            assert read_key(out, profile, pos) == k
    k = read_key(example, "ft8", 6)
    assert write_key(example, "ft8", 1, read_key(example, "ft8", 1)["short"], None) == example
    assert k["long"] is not None and len(k["long"]) == 3

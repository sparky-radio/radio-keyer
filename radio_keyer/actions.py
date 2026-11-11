"""Action definitions. Parsing lives in config.py; execution in the rig layer (later)."""

from __future__ import annotations

import string
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# Hamlib mode names accepted by `mode:` / `freq: ..., mode:`.
HAMLIB_MODES: Final[frozenset[str]] = frozenset(
    {
        "USB", "LSB", "CW", "CWR", "AM", "FM", "WFM", "FMN",
        "RTTY", "RTTYR", "PKTUSB", "PKTLSB", "PKTFM", "PKTAM",
        "DSB", "SAM", "C4FM", "DSTAR",
    }
)

# Placeholders allowed in CW text, e.g. "CQ {call}".
CW_FIELDS: Final[frozenset[str]] = frozenset({"call"})

PROFILE_NEXT: Final[str] = "next"
PROFILE_PREV: Final[str] = "prev"


class CwVia(StrEnum):
    CAT = "cat"
    WINKEYER = "winkeyer"
    DTR = "dtr"


def cw_fields(text: str) -> set[str]:
    """Placeholder names used in `text`. Raises ValueError if malformed."""
    fields: set[str] = set()
    for _literal, field, spec, conv in string.Formatter().parse(text):
        if field is None:
            continue
        if field == "" or spec or conv:
            raise ValueError(f"invalid placeholder in {text!r}; use e.g. {{call}}")
        fields.add(field)
    return fields


@dataclass(frozen=True, slots=True)
class CwAction:
    text: str
    via: CwVia = CwVia.CAT

    def render(self, values: Mapping[str, str]) -> str:
        return self.text.format_map(values)

    def describe(self) -> str:
        suffix = "" if self.via is CwVia.CAT else f" via {self.via}"
        return f'cw "{self.text}"{suffix}'


@dataclass(frozen=True, slots=True)
class CwMemoryAction:
    slot: int

    def describe(self) -> str:
        return f"cw memory {self.slot}"


@dataclass(frozen=True, slots=True)
class CwStopAction:
    def describe(self) -> str:
        return "cw stop"


@dataclass(frozen=True, slots=True)
class BandAction:
    band: str
    """Band name (e.g. '20m'), 'up' or 'down'."""

    def describe(self) -> str:
        return f"band {self.band}"


@dataclass(frozen=True, slots=True)
class FreqAction:
    hz: int
    mode: str | None = None

    def describe(self) -> str:
        mode = f" {self.mode}" if self.mode else ""
        return f"freq {self.hz / 1e6:.6f} MHz{mode}"


@dataclass(frozen=True, slots=True)
class ModeAction:
    mode: str

    def describe(self) -> str:
        return f"mode {self.mode}"


@dataclass(frozen=True, slots=True)
class CatAction:
    data: bytes
    source: str
    """As written in config (ASCII text or hex), for display/round-trip."""
    is_hex: bool = False

    def describe(self) -> str:
        return f"cat_hex {self.source}" if self.is_hex else f"cat {self.source!r}"


@dataclass(frozen=True, slots=True)
class ProfileAction:
    target: str
    """Profile name, 'next' or 'prev'."""

    def describe(self) -> str:
        return f"profile {self.target}"


@dataclass(frozen=True, slots=True)
class DelayAction:
    ms: int

    def describe(self) -> str:
        return f"delay {self.ms} ms"


type Action = (
    CwAction
    | CwMemoryAction
    | CwStopAction
    | BandAction
    | FreqAction
    | ModeAction
    | CatAction
    | ProfileAction
    | DelayAction
)

type Binding = tuple[Action, ...]
"""One or more actions run in order for a single key press."""

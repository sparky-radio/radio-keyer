"""Amateur band table (IARU Region 2 / US edges) and band stepping."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Band:
    name: str
    low_hz: int
    high_hz: int
    default_hz: int
    """Frequency used when jumping to the band (CW portion by default)."""

    def contains(self, hz: int) -> bool:
        return self.low_hz <= hz <= self.high_hz


# Ordered low → high. 60m omitted (channelized).
BANDS: Final[tuple[Band, ...]] = (
    Band("160m", 1_800_000, 2_000_000, 1_830_000),
    Band("80m", 3_500_000, 4_000_000, 3_530_000),
    Band("40m", 7_000_000, 7_300_000, 7_030_000),
    Band("30m", 10_100_000, 10_150_000, 10_110_000),
    Band("20m", 14_000_000, 14_350_000, 14_030_000),
    Band("17m", 18_068_000, 18_168_000, 18_080_000),
    Band("15m", 21_000_000, 21_450_000, 21_030_000),
    Band("12m", 24_890_000, 24_990_000, 24_900_000),
    Band("10m", 28_000_000, 29_700_000, 28_030_000),
    Band("6m", 50_000_000, 54_000_000, 50_090_000),
    Band("2m", 144_000_000, 148_000_000, 144_050_000),
    Band("70cm", 420_000_000, 450_000_000, 432_050_000),
)

_BY_NAME: Final[dict[str, Band]] = {b.name: b for b in BANDS}


def band_by_name(name: str) -> Band | None:
    return _BY_NAME.get(name.lower())


def band_for_freq(hz: int) -> Band | None:
    return next((b for b in BANDS if b.contains(hz)), None)


def step_band(hz: int, direction: int, bands: tuple[Band, ...] = BANDS) -> Band:
    """Next band up (+1) or down (-1) from frequency `hz`, wrapping around.

    Works even if `hz` is outside every band (picks the nearest in that direction).
    """
    if direction not in (1, -1):
        raise ValueError("direction must be +1 or -1")
    if direction == 1:
        return next((b for b in bands if b.low_hz > hz), bands[0])
    return next((b for b in reversed(bands) if b.high_hz < hz), bands[-1])

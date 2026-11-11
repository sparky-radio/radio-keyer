from radio_keyer.bands import band_by_name, band_for_freq, step_band


def test_lookup() -> None:
    assert band_by_name("20M") is not None
    assert band_by_name("11m") is None
    band = band_for_freq(14_074_000)
    assert band is not None and band.name == "20m"


def test_step_up_down_and_wrap() -> None:
    assert step_band(14_074_000, 1).name == "17m"
    assert step_band(14_074_000, -1).name == "30m"
    assert step_band(432_000_000, 1).name == "160m"
    assert step_band(1_830_000, -1).name == "70cm"


def test_step_from_out_of_band() -> None:
    assert step_band(5_000_000, 1).name == "40m"
    assert step_band(5_000_000, -1).name == "80m"

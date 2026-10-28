from datetime import date

import pytest

from eeg20_radar.rules import compute_expiry, expiry_bucket


@pytest.mark.parametrize(
    ("commissioned", "end"),
    [
        (date(2006, 3, 15), date(2026, 12, 31)),
        (date(2006, 1, 1), date(2026, 12, 31)),
        (date(2006, 12, 31), date(2026, 12, 31)),
        (date(2012, 2, 29), date(2032, 12, 31)),
        (date(2000, 4, 1), date(2020, 12, 31)),
        (date(2020, 12, 31), date(2040, 12, 31)),
    ],
)
def test_term_is_twenty_years_plus_rest_of_commissioning_year(commissioned, end):
    expiry = compute_expiry(commissioned)
    assert expiry.remuneration_end_date == end
    assert expiry.expiry_year == end.year
    assert expiry.needs_review == ()


@pytest.mark.parametrize("commissioned", [date(1995, 6, 1), date(2000, 3, 31)])
def test_plants_older_than_eeg2000_are_deemed_commissioned_in_2000(commissioned):
    expiry = compute_expiry(commissioned)
    assert expiry.remuneration_end_date == date(2020, 12, 31)
    assert "PRE_EEG2000_DEEMED_2000" in expiry.notes


def test_pre_strom_eg_date_needs_review():
    expiry = compute_expiry(date(1988, 6, 1))
    assert expiry.expiry_year == 2020
    assert "IMPLAUSIBLE_EARLY_DATE" in expiry.needs_review


def test_eeg2021_regime_is_flagged_not_confirmed():
    expiry = compute_expiry(date(2021, 1, 1))
    assert "EEG2021_TERM_RULE_UNVERIFIED" in expiry.needs_review


@pytest.mark.parametrize(
    ("end", "bucket"),
    [
        (None, "unknown"),
        (date(2025, 12, 31), "expired"),
        (date(2026, 12, 31), "within_horizon"),
        (date(2031, 12, 31), "within_horizon"),
        (date(2032, 12, 31), "beyond_horizon"),
    ],
)
def test_expiry_bucket(end, bucket):
    assert expiry_bucket(end, date(2026, 1, 1), 5) == bucket

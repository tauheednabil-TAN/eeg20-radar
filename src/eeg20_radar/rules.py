"""
EEG remuneration term rule.

Indicative rule (EEG 2000 s.9, EEG 2004 s.12(3), EEG 2009/2012 s.21(2),
EEG 2014 s.22, EEG 2017 s.25): payment runs for 20 calendar years plus the
remainder of the commissioning year, so it ends on 31 December of
(commissioning year + 20).

Installations commissioned before EEG 2000 took effect (1 April 2000) are
deemed commissioned in 2000, so their term ended on 31 December 2020.

From EEG 2021 onwards the wording of s.25 changed and later amendments
(e.g. extension for negative-price periods) can move the end date. For
those installations the same formula is shown but flagged for review
instead of being presented as confirmed.
"""

from dataclasses import dataclass
from datetime import date

RULE_VERSION = "eeg-term-v2"

TERM_YEARS = 20
EEG2000_EFFECTIVE = date(2000, 4, 1)
DEEMED_YEAR_FOR_OLD_PLANTS = 2000
EEG2021_EFFECTIVE = date(2021, 1, 1)
# EEG 2017 s.22: solar installations above 750 kW commissioned from 2017
# receive payments only with a tender award (Zuschlag).
TENDER_REGIME_START = date(2017, 1, 1)
TENDER_THRESHOLD_KW = 750.0
# Stromeinspeisungsgesetz took effect 1 January 1991. Earlier EEG dates are
# possible for very old plants but unusual enough to review.
PLAUSIBLE_EARLIEST = date(1991, 1, 1)


@dataclass(frozen=True)
class Expiry:
    remuneration_end_date: date
    expiry_year: int
    notes: tuple[str, ...]
    needs_review: tuple[str, ...]


def compute_expiry(eeg_commissioning_date: date) -> Expiry:
    notes: list[str] = []
    review: list[str] = []

    base_year = eeg_commissioning_date.year
    if eeg_commissioning_date < EEG2000_EFFECTIVE:
        base_year = DEEMED_YEAR_FOR_OLD_PLANTS
        notes.append("PRE_EEG2000_DEEMED_2000")

    if eeg_commissioning_date < PLAUSIBLE_EARLIEST:
        review.append("IMPLAUSIBLE_EARLY_DATE")

    if eeg_commissioning_date >= EEG2021_EFFECTIVE:
        review.append("EEG2021_TERM_RULE_UNVERIFIED")

    end_year = base_year + TERM_YEARS
    return Expiry(
        remuneration_end_date=date(end_year, 12, 31),
        expiry_year=end_year,
        notes=tuple(notes),
        needs_review=tuple(review),
    )


def needs_tender_award(eeg_commissioning_date: date, installed_kw: float | None) -> bool:
    return (
        eeg_commissioning_date >= TENDER_REGIME_START
        and installed_kw is not None
        and installed_kw > TENDER_THRESHOLD_KW
    )


def expiry_bucket(
    end_date: date | None,
    reference_date: date,
    horizon_years: int,
) -> str:
    if end_date is None:
        return "unknown"
    if end_date < reference_date:
        return "expired"
    if end_date.year <= reference_date.year + horizon_years:
        return "within_horizon"
    return "beyond_horizon"

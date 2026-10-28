# EEG 20-year expiry radar: quality report

Indicative screening only. Not legal advice or a permit decision.

| Item | Value |
|---|---|
| Run | `20261028T223801Z-93bf68bd` |
| Scope | NW (Nordrhein-Westfalen) |
| Reference date | 2026-01-01 |
| Horizon | 5 years |
| Rule version | eeg-term-v2 |
| Result digest (sha256) | `e4b355355170f2f1…` |

## Sources

| Kind | File | sha256 | Bytes |
|---|---|---|---|
| unit | EinheitenSolar_1.xml | `84c01df456db…` | 35138 |
| eeg | AnlagenEegSolar_1.xml | `5be1bcad64af…` | 14103 |

## Record flow

| Step | Records |
|---|---|
| Unit records read | 40 |
| EEG records read | 34 |
| Out of scope (other region) | 2 |
| Unique valid units in scope | 32 |
| Rejected (all reasons) | 8 |
| Staged in `iris_staging.eeg_repowering_targets` | 32 |
| EEG records linked to a staged unit | 28 |
| EEG records not linked to any in-scope unit | 4 |

## Coverage

| Expiry status | Rows |
|---|---|
| confirmed | 16 |
| incomplete | 6 |
| needs_review | 10 |

- Rows with a derived expiry year: 81.2%
- Rows confirmed without review flags: 50.0%

| Expiry bucket | Rows |
|---|---|
| beyond_horizon | 4 |
| expired | 7 |
| unknown | 6 |
| within_horizon | 15 |

| Spatial precision | Rows |
|---|---|
| point | 30 |
| postcode | 2 |

## Repowering candidates

21 units, 2298.1 kW net (operating or temporarily shut down, expiry known, expired or within horizon).

| Expiry year | Units |
|---|---|
| 2020 | 2 |
| 2023 | 1 |
| 2024 | 2 |
| 2025 | 1 |
| 2026 | 4 |
| 2027 | 3 |
| 2028 | 3 |
| 2029 | 3 |
| 2030 | 2 |

## Flags on staged rows

| Flag | Severity | Rows | Example unit |
|---|---|---|---|
| `COORDINATES_INVALID` | info | 1 | SEE900000000020 |
| `COORDINATES_OUTSIDE_REGION_BBOX` | review | 1 | SEE900000000021 |
| `DATE_MISMATCH_UNIT_VS_EEG` | review | 1 | SEE900000000011 |
| `EEG2021_TERM_RULE_UNVERIFIED` | review | 1 | SEE900000000025 |
| `EEG_DATE_INVALID` | blocking | 1 | SEE900000000010 |
| `EEG_DATE_IN_FUTURE` | blocking | 1 | SEE900000000026 |
| `EEG_DATE_MISSING` | blocking | 1 | SEE900000000009 |
| `EEG_LINK_INCONSISTENT` | review | 1 | SEE900000000035 |
| `EEG_RECORD_NOT_FOUND` | blocking | 1 | SEE900000000008 |
| `IMPLAUSIBLE_EARLY_DATE` | review | 1 | SEE900000000031 |
| `NOT_GRID_OPERATOR_VERIFIED` | review | 1 | SEE900000000029 |
| `NO_EEG_LINK` | blocking | 2 | SEE900000000007 |
| `POWER_INVALID` | info | 1 | SEE900000000030 |
| `PRE_EEG2000_DEEMED_2000` | info | 2 | SEE900000000003 |
| `REGION_CONFLICT` | review | 1 | SEE900000000033 |
| `SHARED_EEG_ANLAGE` | info | 2 | SEE900000000027 |
| `TENDER_AWARD_UNVERIFIED` | review | 2 | SEE900000000025 |
| `UNIT_NOT_COMMISSIONED` | blocking | 1 | SEE900000000014 |
| `UNIT_PERMANENTLY_DECOMMISSIONED` | review | 1 | SEE900000000012 |
| `UNIT_TEMPORARILY_DECOMMISSIONED` | review | 1 | SEE900000000013 |

`blocking` = no expiry derived (`incomplete`); `review` = expiry derived but needs a human check; `info` = noted only.

## Rejected records

| Reason | Records |
|---|---|
| `DUPLICATE_EXACT` | 1 |
| `DUPLICATE_SUPERSEDED` | 2 |
| `INVALID_EEG_ID` | 1 |
| `INVALID_UNIT_ID` | 1 |
| `MISSING_UNIT_ID` | 1 |
| `NOT_SOLAR` | 1 |
| `REGION_UNKNOWN` | 1 |

All rejected records:

| Source | Record | Reason | Detail |
|---|---|---|---|
| AnlagenEegSolar_1.xml#27 | EEG900000000034 | `DUPLICATE_SUPERSEDED` | superseded by AnlagenEegSolar_1.xml#28 |
| AnlagenEegSolar_1.xml#31 | EEG-BROKEN | `INVALID_EEG_ID` | unexpected format 'EEG-BROKEN' |
| EinheitenSolar_1.xml#15 | SEE900000000015 | `DUPLICATE_SUPERSEDED` | superseded by EinheitenSolar_1.xml#16 |
| EinheitenSolar_1.xml#18 | SEE900000000016 | `DUPLICATE_EXACT` | identical copy |
| EinheitenSolar_1.xml#19 | (none) | `MISSING_UNIT_ID` | EinheitMastrNummer is empty |
| EinheitenSolar_1.xml#20 | SEE12 | `INVALID_UNIT_ID` | unexpected format 'SEE12' |
| EinheitenSolar_1.xml#21 | SEE900000000019 | `NOT_SOLAR` | Energietraeger 2497 |
| EinheitenSolar_1.xml#24 | SEE900000000022 | `REGION_UNKNOWN` | no usable Gemeindeschluessel or Bundesland |

## Acceptance evidence

- Incomplete rows carrying an expiry date: 0 (must be 0)
- Expiry dates not derived from an EEG commissioning date: 0 (must be 0)
- The database enforces the same rule with the `eeg_targets_no_invented_expiry` check constraint.
- Overall: **PASS**

## Database

- Rows already staged but not present in this run (kept, not deleted): 0
- Assessments recorded in `eeg_repowering_target_history` for this run: 32

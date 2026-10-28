# Quality and error report

Run: fixture data, scope NRW, reference date 2026-01-01, horizon 5 years. The generated report for the same run is in [examples/quality_report.md](examples/quality_report.md).

## Summary

| | Records |
|---|---|
| Unit records read | 37 |
| EEG records read | 31 |
| Staged | 29 (14 confirmed, 9 needs review, 6 incomplete) |
| Rejected | 8 |
| Out of scope (Bavaria, Berlin) | 2 |
| Repowering candidates | 20 |

79% of staged rows have an expiry year (confirmed or needs review). The other 21% have none, because their records don't support one.

## Edge cases and outcomes

Each fixture unit `SEE9000000000nn` covers one case. Unit numbers are the last two digits.

| Unit | Case | Status | Expiry | Candidate | Flags |
|---|---|---|---|---|---|
| 01 | Clean record, commissioned 15 Mar 2006 | confirmed | 2026 | yes | |
| 02 | Small rooftop unit, no coordinates | confirmed | 2024 | yes | (postcode precision) |
| 03 | Commissioned 1999, before EEG 2000 | confirmed | 2020 | yes | `PRE_EEG2000_DEEMED_2000` |
| 04 | Commissioned 31 Dec 2008 | confirmed | 2028 | yes | |
| 05 | Commissioned 29 Feb 2012 | confirmed | 2032 | no (beyond horizon) | |
| 06 | Commissioned 1 Jan 2010 | confirmed | 2030 | yes | |
| 07 | No EEG registration number | incomplete | none | no | `NO_EEG_LINK` |
| 08 | EEG number points to a missing EEG record | incomplete | none | no | `EEG_RECORD_NOT_FOUND` |
| 09 | EEG record with an empty EEG date | incomplete | none | no | `EEG_DATE_MISSING` |
| 10 | EEG date `2007-13-45` | incomplete | none | no | `EEG_DATE_INVALID` |
| 11 | Unit date 2011, EEG date 2009 | needs review | 2029 | yes | `DATE_MISMATCH_UNIT_VS_EEG` |
| 12 | Permanently shut down | needs review | 2025 | no | `UNIT_PERMANENTLY_DECOMMISSIONED` |
| 13 | Temporarily shut down | needs review | 2027 | yes | `UNIT_TEMPORARILY_DECOMMISSIONED` |
| 14 | Still in planning | incomplete | none | no | `UNIT_NOT_COMMISSIONED`, `NO_EEG_LINK` |
| 15 | Two versions; newer one (12.5 kW) kept | confirmed | 2028 | yes | older copy rejected |
| 16 | Exact duplicate | confirmed | 2026 | yes | second copy rejected |
| 20 | Coordinates `0, 0` | confirmed | 2023 | yes | `COORDINATES_INVALID` (falls back to postcode) |
| 21 | Cologne address, coordinates in Bavaria | needs review | 2026 | yes | `COORDINATES_OUTSIDE_REGION_BBOX` |
| 25 | Commissioned 2022 (EEG 2021 regime) | needs review | 2042 | no | `EEG2021_TERM_RULE_UNVERIFIED` |
| 26 | EEG date after the reference date | incomplete | none | no | `EEG_DATE_IN_FUTURE` |
| 27, 28 | Two units sharing one EEG installation | confirmed | 2029 | yes | `SHARED_EEG_ANLAGE` |
| 29 | Grid operator has not checked the registration | needs review | 2027 | yes | `NOT_GRID_OPERATOR_VERIFIED` |
| 30 | Capacity `-5` | confirmed | 2030 | yes | `POWER_INVALID` (capacity left empty) |
| 31 | EEG date 1988, before the Stromeinspeisungsgesetz | needs review | 2020 | yes | `IMPLAUSIBLE_EARLY_DATE` |
| 32 | German date format, decimal comma | confirmed | 2025 | yes | |
| 33 | Municipality key says NRW, `Bundesland` says Bavaria | needs review | 2026 | yes | `REGION_CONFLICT` |
| 34 | Two EEG record versions; newer date kept | confirmed | 2024 | yes | older EEG copy rejected |
| 35 | EEG record lists a different unit | needs review | 2028 | yes | `EEG_LINK_INCONSISTENT` |

## Rejected data

| Record | Reason |
|---|---|
| Unit row 15 (older copy of 15) | `DUPLICATE_SUPERSEDED` |
| Unit row 18 (second copy of 16) | `DUPLICATE_EXACT` |
| Unit row 19 | `MISSING_UNIT_ID` |
| Unit row 20, ID `SEE12` | `INVALID_UNIT_ID` |
| Unit 19 (wind, code 2497) | `NOT_SOLAR` |
| Unit 22, no region data | `REGION_UNKNOWN` (with `--scope DE` it is staged with `spatial_precision = none`) |
| EEG row 27 (older copy for unit 34) | `DUPLICATE_SUPERSEDED` |
| EEG row 31, ID `EEG-BROKEN` | `INVALID_EEG_ID` |

Every reject is stored with its file, position, reason and raw record in `iris_staging.eeg_repowering_rejects`.

## Evidence for the acceptance criteria

**Incomplete history is flagged, and no expiry date is invented.**
- All 6 incomplete rows have `expiry_year` and `remuneration_end_date` set to `NULL` (validation query 1 returns `0 | 0`).
- Unit 09 has a unit commissioning date but no EEG date. The tool does not fall back to the unit date; `test_no_expiry_is_ever_taken_from_the_unit_date` checks this.
- The `eeg_targets_no_invented_expiry` check constraint rejects such a row at the database level; `test_database_rejects_invented_expiry` shows the whole run rolls back.
- Eligibility doubts (shutdown, unverified registration, region conflict, EEG 2021 rule) produce a `needs_review` status with the reason in `flag_details`.

**The method is deterministic and auditable.**
- Two runs on the same files and reference date give identical rows and the same sha256 digest (`test_runs_are_deterministic`; digest `9320195f…` in the example run).
- Validation query 2 recomputes every end date in SQL, independently of the Python code, and finds 0 differences.
- Each row stores its raw source record, record hash, file position, rule version and reference date. Each run stores the sha256 of its input files.

**Staging only.**
- The run refuses any target schema other than `iris_staging`, and refuses superuser roles and roles that can create objects in `iris_core` (`test_guard_and_cli.py`, `test_db.py`).

## Not covered by this report

These results come from synthetic fixtures. The real MaStR export was not downloaded, so real coverage, reject rates and the share of units without coordinates are unknown until the first real run. See "Limitations and known gaps" in the README.

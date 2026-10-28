# EEG 20-year expiry radar

Task 122 · IRIS-REG-EEG20 · writes to `iris_staging.eeg_repowering_targets`

This tool reads solar units from the German Marktstammdatenregister (MaStR) and works out when each unit's 20-year EEG feed-in remuneration ends. Units whose payments have ended or will end within the horizon (default 5 years) are marked as repowering candidates. When the records can't support an end date, the tool leaves the date empty and records why instead of guessing.

The result is an indicative screening based on public registry data. It is not legal advice or a permit decision.

## Quick start (Docker)

```bash
cp .env.example .env
```

Change the two passwords in `.env`, then:

```bash
docker compose --profile test run --rm tests
```

```bash
docker compose run --rm radar
```

The first command starts a local PostGIS container, creates the restricted `eeg20_writer` role and the `iris_staging` schema, and runs the full test suite against it. The second screens the fixture data, writes to `iris_staging`, and puts the report and two CSVs (staged rows, rejected records) in `./output/`.

The `radar` container starts as root only to prepare `./output/`, then runs the tool as the owner of that folder (`docker/entrypoint.sh`). If the folder doesn't exist yet, Docker creates it and the files belong to the container's `radar` user (uid 10001). Create `output/` yourself first if you want the files to belong to you.

To check the staged data:

```bash
docker compose exec -T db psql -U eeg20_writer -d iris_staging_db -f - < sql/validation.sql
```

To remove the containers and the database volume:

```bash
docker compose down -v
```

## Running without Docker

Requires Python 3.11+.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

A file-only run (no database) writes the report and CSVs:

```bash
PYTHONPATH=src .venv/bin/python -m eeg20_radar run --no-db --reference-date 2026-01-01
```

For all of Germany instead of NRW:

```bash
PYTHONPATH=src .venv/bin/python -m eeg20_radar run --no-db --scope DE --reference-date 2026-01-01
```

Unit tests run without a database. The database tests run only when `EEG20_TEST_DATABASE_URL` is set, otherwise they are skipped:

```bash
.venv/bin/pytest
```

### Options

| CLI flag | Environment variable | Default | Meaning |
|---|---|---|---|
| `--scope` | `EEG20_SCOPE` | `NRW` | `NRW`, `DE` (all of Germany), or any state code (`BY`, `NI`, ...) |
| `--reference-date` | `EEG20_REFERENCE_DATE` | today | Date the screening is evaluated against. Set it for reproducible runs. |
| `--horizon-years` | `EEG20_HORIZON_YEARS` | `5` | Expiry years up to reference year + horizon count as candidates |
| `--source-dir` | `EEG20_SOURCE_DIR` | `fixtures/mastr` | Folder with `EinheitenSolar*.xml` and `AnlagenEegSolar*.xml` |
| `--output-dir` | `EEG20_OUTPUT_DIR` | `output` | Report and CSV destination |
| `--no-db` | | off | Skip the database write |
| | `DATABASE_URL` | | Connection for the staging writer role |
| | `EEG20_TARGET_SCHEMA` | `iris_staging` | Any other value stops the run |

`python -m eeg20_radar init-db` creates the staging tables without running a screening.

Exit codes: `0` success, `1` source missing or unreadable (nothing staged), `2` configuration error, `3` acceptance check failed.

## Data provenance

**Source:** the MaStR Gesamtdatenexport published by the Bundesnetzagentur at <https://www.marktstammdatenregister.de/MaStR/Datendownload>, licensed under the Datenlizenz Deutschland, Namensnennung, Version 2.0 (dl-de/by-2-0). The tool reads two file families from that export:

- `EinheitenSolar*.xml`: one record per solar unit (location, status, capacity, unit commissioning date, link to the EEG registration)
- `AnlagenEegSolar*.xml`: one record per EEG installation, including `EegInbetriebnahmedatum`, the date the remuneration period starts

**Fixtures:** this repository does not download the real export (several GB). `fixtures/mastr/` holds a synthetic sample of 40 unit records and 34 EEG records, using the export's field names and catalogue codes. Each record exercises one case: a clean unit, a missing EEG date, a duplicate, a malformed ID, and so on (see [QUALITY_REPORT.md](QUALITY_REPORT.md)). The MaStR numbers (`SEE9000000000xx`, `EEG9000000000xx`) are made up. Municipality keys, postcodes and coordinates are real places in NRW, Bavaria and Berlin.

To run on the real export, unzip it and point `--source-dir` at the folder. The reader accepts the export's UTF-16 encoding and matches field names without regard to case, because MaStR writes the same abbreviation both ways (`EinheitMastrNummer`, `EegMaStRNummer`) and the spelling has varied between releases.

**Traceability:** every run stores the sha256 and size of each input file in `iris_staging.eeg_ingestion_runs`. Every staged row keeps the full raw source record (`raw_unit`, `raw_eeg`), its sha256, and its file and position (`EinheitenSolar_1.xml#16`).

## How the expiry date is derived

EEG 2000 to EEG 2017 pay remuneration for 20 calendar years plus the rest of the commissioning year (EEG 2000 §9, EEG 2004 §12(3), EEG 2009/2012 §21(2), EEG 2014 §22, EEG 2017 §25). So:

```
remuneration_end_date = 31 December of (year of EegInbetriebnahmedatum + 20)
```

A unit commissioned on 15 March 2006 is paid until 31 December 2026.

Installations commissioned before EEG 2000 took effect (1 April 2000) count as commissioned in 2000, so their payments ended on 31 December 2020. These rows get the info flag `PRE_EEG2000_DEEMED_2000`.

The date used is always the EEG commissioning date (`EegInbetriebnahmedatum`), never the unit's own `Inbetriebnahmedatum`. The two can legitimately differ, for example after modules are replaced. If the EEG date is missing or unusable, the row has no expiry date.

Solar installations above 750 kW commissioned from 2017 receive EEG payments only with a tender award (EEG 2017 §22). If such a unit's EEG record has no `Zuschlagsnummer`, it gets `TENDER_AWARD_UNVERIFIED` and a review status. The size used is the EEG record's `InstallierteLeistung`, or the unit's `Bruttoleistung` if that is missing.

The rules are in `src/eeg20_radar/rules.py`, versioned as `eeg-term-v2` (v2 added the tender check). Every row stores the rule version and the reference date used.

## Output: `iris_staging.eeg_repowering_targets`

One row per unique MaStR solar unit in scope. Primary key: `mastr_unit_id`.

### Schema mapping

| Column | Source field | Notes |
|---|---|---|
| `mastr_unit_id` | `EinheitMastrNummer` | Must match `SEE` + 12 digits |
| `eeg_mastr_id` | `EegMaStRNummer` (unit) | Link to the EEG record |
| `eeg_plant_key` | `AnlagenschluesselEeg` | |
| `tender_award_number` | `Zuschlagsnummer` (EEG) | Tender award, if any |
| `unit_name` | `NameStromerzeugungseinheit` | |
| `unit_status_code`, `unit_status` | `EinheitBetriebsstatus` | 31 in planning, 35 operating, 37 temporarily shut down, 38 permanently shut down |
| `state_ags`, `state_code` | `Gemeindeschluessel`[0:2], else `Bundesland` | e.g. `05`, `NW` |
| `municipality_key`, `municipality` | `Gemeindeschluessel`, `Gemeinde` | 8-digit AGS |
| `postcode`, `city` | `Postleitzahl`, `Ort` | Postcode must be 5 digits |
| `latitude`, `longitude`, `geom` | `Breitengrad`, `Laengengrad` | WGS84; `geom` is `Point, 4326` with a GiST index |
| `spatial_precision` | derived | `point`, `postcode`, `municipality` or `none` |
| `gross_power_kw`, `net_power_kw` | `Bruttoleistung`, `Nettonennleistung` | kW; decimal comma accepted |
| `eeg_installed_power_kw` | `InstallierteLeistung` (EEG) | kW; used for the tender check |
| `unit_commissioning_date` | `Inbetriebnahmedatum` | Shown for comparison only |
| `eeg_commissioning_date` | `EegInbetriebnahmedatum` (EEG) | Basis of the expiry date |
| `remuneration_end_date`, `expiry_year` | derived | `NULL` when `incomplete` |
| `expiry_basis`, `rule_version` | derived | |
| `expiry_status` | derived | `confirmed`, `needs_review`, `incomplete` |
| `expiry_bucket` | derived | `expired`, `within_horizon`, `beyond_horizon`, `unknown` |
| `is_repowering_candidate` | derived | Operating or temporarily shut down, not `incomplete`, bucket `expired` or `within_horizon` |
| `flags`, `flag_details` | derived | Codes and reasons, see below |
| `source_last_updated_utc`, `eeg_last_updated_utc` | `DatumLetzteAktualisierung` | Converted to UTC |
| `raw_unit`, `raw_eeg`, `*_record_hash`, `source_*_file` | source | Full original record, sha256 and position |
| `reference_date`, `horizon_years` | run settings | |
| `first_seen_run_id`, `last_seen_run_id`, `updated_at` | run | |

Three more tables in the same schema:

- `eeg_ingestion_runs`: one row per run, with status, input file hashes, counts, and a sha256 digest of the staged result
- `eeg_repowering_rejects`: every record that could not be staged, with reason code, detail and the raw record
- `eeg_repowering_target_history`: each unit's assessment from every run (reference date, expiry, status, bucket, candidate, flags). `eeg_repowering_targets` holds only the latest one.

### Status and flags

| Status | Expiry date | Meaning |
|---|---|---|
| `confirmed` | set | Derived from a valid EEG date, nothing to review |
| `needs_review` | set | Derived from a valid EEG date, but at least one `review` flag |
| `incomplete` | `NULL` | At least one `blocking` flag; the history can't support a date |

Blocking flags: `NO_EEG_LINK`, `EEG_ID_INVALID`, `EEG_RECORD_NOT_FOUND`, `EEG_DATE_MISSING`, `EEG_DATE_INVALID`, `EEG_DATE_IN_FUTURE`, `UNIT_NOT_COMMISSIONED`.

Review flags: `DATE_MISMATCH_UNIT_VS_EEG` (more than 31 days apart), `UNIT_TEMPORARILY_DECOMMISSIONED`, `UNIT_PERMANENTLY_DECOMMISSIONED`, `UNIT_STATUS_UNKNOWN`, `UNIT_DATE_INVALID`, `NOT_GRID_OPERATOR_VERIFIED`, `EEG_LINK_INCONSISTENT`, `DUPLICATE_CONFLICT`, `REGION_CONFLICT`, `COORDINATES_OUTSIDE_REGION_BBOX`, `IMPLAUSIBLE_EARLY_DATE` (before 1991), `EEG2021_TERM_RULE_UNVERIFIED`, `TENDER_AWARD_UNVERIFIED`.

Info flags: `PRE_EEG2000_DEEMED_2000`, `SHARED_EEG_ANLAGE`, `COORDINATES_INVALID`, `POWER_INVALID`, `POSTCODE_INVALID`, `TIMESTAMP_INVALID`.

The table has a check constraint, `eeg_targets_no_invented_expiry`, so the database itself refuses an expiry date on an `incomplete` row or one without an EEG date.

### Rejected records

These records go to `eeg_repowering_rejects` and the report, not to the target table: `MISSING_UNIT_ID`, `INVALID_UNIT_ID`, `MISSING_EEG_ID`, `INVALID_EEG_ID`, `DUPLICATE_EXACT`, `DUPLICATE_SUPERSEDED`, `NOT_SOLAR`, and `REGION_UNKNOWN` (no region data while scope is a single state).

For duplicates, the record with the newest `DatumLetzteAktualisierung` wins. On a tie, the earliest position in the files wins and the kept row gets `DUPLICATE_CONFLICT` if the copies differ.

A file that is not well-formed XML stops the run. Nothing from it is committed to the database, and no CSV is left under its final name, so a truncated download is never staged as if it were complete.

The report lists every reject count by reason and the first 200 rejected records. `eeg_repowering_rejects.csv` and the database table have all of them.

## Staging isolation

The tool can only write to `iris_staging`:

1. `EEG20_TARGET_SCHEMA` must be exactly `iris_staging`, checked before any connection is opened.
2. The connecting role must not be a superuser.
3. If an `iris_core` schema exists in the database, the role must not have `CREATE` on it, or `INSERT`, `UPDATE`, `DELETE` or `TRUNCATE` on any of its tables.
4. All SQL uses fully qualified `iris_staging.*` names.

In Docker, the database is a separate local PostGIS container, bound to `127.0.0.1` only. `docker/initdb/20_iris_staging.sh` creates the `eeg20_writer` role (no superuser, no create-database rights) and an `iris_staging` schema owned by it. Nothing in this repository connects to `iris_core` or a production host. Passwords come from `.env`, which is git-ignored; `.env.example` contains placeholders only.

## Annual refresh

Download the new export and rerun with the new reference date:

```bash
PYTHONPATH=src python -m eeg20_radar run --source-dir /data/mastr-2027 --reference-date 2027-01-01
```

Rows are upserted on `mastr_unit_id`. Rows missing from the new export are kept, not deleted; they keep their older `last_seen_run_id`, and the report counts them. Each run also appends every unit's assessment to `eeg_repowering_target_history`, so last year's result is still there after the refresh. Each run is one transaction: if it fails, nothing from it is written and the run is marked `failed` with the error.

With the same input files and reference date, two runs produce identical rows and the same result digest. Tests check this.

## Memory and runtime

The pipeline reads the unit files twice and the EEG files once, and passes each staged row straight to the database, CSV and report writers. Between passes it keeps only a small index entry per in-scope unit and the EEG records linked to those units. Units from other regions are counted and dropped as they are read.

Measured on 200,000 synthetic units (246 MB of XML) in this project's sandbox:

| Case | Peak memory | Time |
|---|---:|---:|
| All 200,000 units in scope | 322 MB | 54 s |
| All 200,000 units in another state | 21 MB | 16 s |

That is about 1.5 KB per in-scope unit, so 1 million in-scope units need about 1.6 GB. Before this design, the same 200,000-unit run peaked at 1,606 MB.

## Automated checks

- 106 pytest tests: the expiry and tender rules and their edge cases (31 December, 29 February, pre-2000, post-2021, 750 kW), field parsers, every fixture case, alternative field spellings, determinism, streaming output, flat reader memory, UTF-16 input, truncated XML, the staging guard, CLI exit codes, and database tests for upsert, idempotency, no-delete refresh, history across refreshes, the check constraint, rollback on failure (including mid-run), the CLI writing straight to staging, and role permissions.
- `ruff` lint and format checks.
- `sql/validation.sql`: queries for the acceptance checks, an independent recomputation of the rule, coverage, candidates by municipality, a 10 km spatial example, stale rows, rejects, tender-size units and assessment history.
- `.github/workflows/ci.yml` runs all of the above against a PostGIS service container, plus the Docker Compose test and run.

## Example result

`examples/` holds the output of `docker compose run --rm radar` on the fixtures with reference date 2026-01-01:

- `quality_report.md` and `quality_report.json`
- `eeg_repowering_targets.csv` and `eeg_repowering_rejects.csv`
- `validation_result.txt`: output of `sql/validation.sql` after that run

From 40 unit records in NRW scope: 32 staged (16 confirmed, 10 need review, 6 incomplete), 8 rejected, 2 out of scope, and 21 repowering candidates.

## Assumptions

- The 20-years-plus-rest-of-year rule applies to installations commissioned up to 31 December 2020. EEG 2021 changed the wording of §25, and EEG 2023 added an extension for negative-price periods, so installations from 2021 onwards get `EEG2021_TERM_RULE_UNVERIFIED` and a review status. Check current law before relying on these dates.
- MaStR catalogue codes are in `src/eeg20_radar/catalog.py`: solar `2495`, status `31/35/37/38`, grid operator check passed `2954`, and the `Bundesland` codes `1400` to `1415`. They follow the export documentation (Dokumentation MaStR Gesamtdatenexport) but have not been checked against a real export file, because this project was built on fixtures only. Check them against the current catalogue before a production run.
- The tender award number is read from `Zuschlagsnummer`, the name in the export documentation, or `Zuschlagnummer` as a fallback.
- Timestamps without a UTC offset are Europe/Berlin local time.
- A unit's state comes from the first two digits of its `Gemeindeschluessel`, falling back to the `Bundesland` code.
- A gap of more than 31 days between the unit date and the EEG date is flagged for review.

## Limitations and known gaps

- **No real data was run.** All results in this repository come from the synthetic fixtures. Coverage numbers on the real export will be different.
- **No payment records.** The EEG commissioning date in MaStR is the only evidence used. Actual payment data from grid operators (e.g. the EEG annual accounts on netztransparenz.de) is not cross-checked, so suspended, reduced or ended payments are not detected.
- **Small units have no coordinates.** MaStR does not publish exact coordinates for most small units (roughly up to 30 kW, mostly rooftops). These rows have `postcode` or `municipality` precision only.
- **Spatial check is a bounding box.** The region check uses a rectangle around NRW, not the state boundary, so points just outside the border but inside the box pass. Only NRW has a box; other states are checked by municipality key only.
- **Special cases are flagged, not resolved.** Module replacement, relocated installations and merged installations (`Anlagenzusammenfassung`) can shift the end date. The tool flags these through date mismatches and shared EEG records but does not model them.
- **Tender check starts in 2017.** Ground-mounted plants awarded under the 2015 to 2016 pilot tenders (FFAV) are not checked for an award number.
- **Decommissioned units stay in the table** with a review flag so the history isn't lost, but they are never candidates.
- **The export layout can change.** Field names and file names follow the Gesamtdatenexport documentation. Case differences are tolerated, but a renamed field or file would need an update to `source.py` and `pipeline.py`.
- **Memory grows with the region size.** About 1.5 KB per in-scope unit (see "Memory and runtime"). A national run on a small machine needs a state-by-state split.

## Project layout

```
src/eeg20_radar/
  source.py      streaming XML reader, file hashes
  normalize.py   date, timestamp, power and coordinate parsers
  rules.py       EEG term rule and expiry buckets
  pipeline.py    three-pass screening: scope, de-duplication, flags, derivation
  db.py          staging guard, batched transactional writer, history
  report.py      streaming quality report and CSVs
  cli.py         command line
  catalog.py     MaStR codes and regional keys
  sql/schema.sql staging DDL
fixtures/mastr/  synthetic MaStR sample
tests/           pytest suite
sql/             validation queries
docker/          entrypoint, staging role and schema setup
examples/        example run output
```

import shutil
from collections import Counter
from datetime import date

import pytest

from conftest import FIXTURES, unit
from eeg20_radar.pipeline import normalize_scope, run_pipeline, screen
from eeg20_radar.source import SourceError, SourceRecord


def test_fixture_record_flow(fixture_result):
    assert fixture_result.stats == {
        "units_read": 40,
        "eeg_records_read": 34,
        "units_out_of_scope": 2,
        "units_unique_in_scope": 32,
        "units_staged": 32,
        "eeg_records_linked": 28,
        "eeg_records_not_linked": 4,
        "rejected": 8,
    }


def test_rejects_carry_reason_and_source(fixture_result):
    reasons = Counter(r.reason_code for r in fixture_result.rejects)
    assert reasons == {
        "MISSING_UNIT_ID": 1,
        "INVALID_UNIT_ID": 1,
        "INVALID_EEG_ID": 1,
        "DUPLICATE_SUPERSEDED": 2,
        "DUPLICATE_EXACT": 1,
        "NOT_SOLAR": 1,
        "REGION_UNKNOWN": 1,
    }
    for r in fixture_result.rejects:
        assert r.source_file.endswith(".xml") and r.record_index > 0 and r.raw


def test_confirmed_expiry(rows):
    row = rows[unit(1)]
    assert row["expiry_status"] == "confirmed"
    assert row["eeg_commissioning_date"] == date(2006, 3, 15)
    assert row["remuneration_end_date"] == date(2026, 12, 31)
    assert row["expiry_basis"] == "EegInbetriebnahmedatum"
    assert row["is_repowering_candidate"]
    assert row["spatial_precision"] == "point"


@pytest.mark.parametrize(
    ("n", "flag"),
    [
        (7, "NO_EEG_LINK"),
        (8, "EEG_RECORD_NOT_FOUND"),
        (9, "EEG_DATE_MISSING"),
        (10, "EEG_DATE_INVALID"),
        (14, "UNIT_NOT_COMMISSIONED"),
        (26, "EEG_DATE_IN_FUTURE"),
    ],
)
def test_incomplete_history_gets_no_expiry(rows, n, flag):
    row = rows[unit(n)]
    assert row["expiry_status"] == "incomplete"
    assert flag in row["flags"]
    assert row["expiry_year"] is None
    assert row["remuneration_end_date"] is None
    assert row["expiry_bucket"] == "unknown"
    assert not row["is_repowering_candidate"]


def test_no_expiry_is_ever_taken_from_the_unit_date(rows):
    # Unit 9 has a unit commissioning date but no EEG date.
    assert rows[unit(9)]["unit_commissioning_date"] == date(2009, 9, 9)
    assert rows[unit(9)]["expiry_year"] is None
    for row in rows.values():
        if row["expiry_year"] is not None:
            assert row["eeg_commissioning_date"] is not None


@pytest.mark.parametrize(
    ("n", "flag"),
    [
        (11, "DATE_MISMATCH_UNIT_VS_EEG"),
        (12, "UNIT_PERMANENTLY_DECOMMISSIONED"),
        (13, "UNIT_TEMPORARILY_DECOMMISSIONED"),
        (21, "COORDINATES_OUTSIDE_REGION_BBOX"),
        (25, "EEG2021_TERM_RULE_UNVERIFIED"),
        (29, "NOT_GRID_OPERATOR_VERIFIED"),
        (31, "IMPLAUSIBLE_EARLY_DATE"),
        (33, "REGION_CONFLICT"),
        (35, "EEG_LINK_INCONSISTENT"),
        (37, "TENDER_AWARD_UNVERIFIED"),
    ],
)
def test_eligibility_uncertainty_is_flagged_for_review(rows, n, flag):
    row = rows[unit(n)]
    assert row["expiry_status"] == "needs_review"
    assert flag in row["flags"]
    assert row["flag_details"][flag] is not None


def test_mismatch_uses_eeg_date_not_unit_date(rows):
    row = rows[unit(11)]
    assert row["eeg_commissioning_date"] == date(2009, 5, 10)
    assert row["expiry_year"] == 2029


def test_permanently_decommissioned_is_not_a_candidate(rows):
    assert rows[unit(12)]["expiry_bucket"] == "expired"
    assert not rows[unit(12)]["is_repowering_candidate"]


def test_duplicates_keep_newest_version(rows):
    assert rows[unit(15)]["net_power_kw"] == 12.5
    assert rows[unit(34)]["eeg_commissioning_date"] == date(2004, 4, 4)


def test_shared_eeg_anlage(rows):
    assert rows[unit(27)]["eeg_mastr_id"] == rows[unit(28)]["eeg_mastr_id"]
    assert "SHARED_EEG_ANLAGE" in rows[unit(27)]["flags"]
    assert rows[unit(27)]["expiry_status"] == "confirmed"


def test_spatial_fallbacks(rows):
    assert rows[unit(2)]["spatial_precision"] == "postcode"
    assert rows[unit(2)]["latitude"] is None
    assert rows[unit(20)]["spatial_precision"] == "postcode"
    assert "COORDINATES_INVALID" in rows[unit(20)]["flags"]


def test_bad_power_is_nulled_not_guessed(rows):
    assert rows[unit(30)]["net_power_kw"] is None
    assert "POWER_INVALID" in rows[unit(30)]["flags"]
    assert rows[unit(32)]["net_power_kw"] == 9.8


def test_pre_eeg2000(rows):
    row = rows[unit(3)]
    assert row["remuneration_end_date"] == date(2020, 12, 31)
    assert row["expiry_status"] == "confirmed"
    assert "PRE_EEG2000_DEEMED_2000" in row["flags"]


def test_raw_source_is_kept_for_every_row(rows):
    for row in rows.values():
        raw_ids = {v for k, v in row["raw_unit"].items() if k.lower() == "einheitmastrnummer"}
        assert raw_ids == {row["mastr_unit_id"]}
        assert len(row["unit_record_hash"]) == 64


def test_germany_scope(make_config, logger):
    result = run_pipeline(make_config(scope="DE"), logger)
    rows = {r["mastr_unit_id"]: r for r in result.targets}
    assert result.stats["units_out_of_scope"] == 0
    assert rows[unit(23)]["state_code"] == "BY"
    assert rows[unit(24)]["state_code"] == "BE"
    # No region data: kept for Germany, but without any location.
    assert rows[unit(22)]["spatial_precision"] == "none"
    assert "REGION_UNKNOWN" not in Counter(r.reason_code for r in result.rejects)


def test_scope_aliases():
    assert normalize_scope("nrw") == "NW"
    assert normalize_scope("DE") == "DE"
    with pytest.raises(ValueError):
        normalize_scope("XX")


def test_runs_are_deterministic(make_config, logger, fixture_result):
    again = run_pipeline(make_config(), logger)
    assert again.digest == fixture_result.digest
    assert again.targets == fixture_result.targets


def test_reference_date_changes_buckets_not_expiry(make_config, logger, rows):
    later = {
        r["mastr_unit_id"]: r
        for r in run_pipeline(make_config(reference_date=date(2027, 1, 1)), logger).targets
    }
    assert later[unit(1)]["expiry_year"] == rows[unit(1)]["expiry_year"]
    assert rows[unit(1)]["expiry_bucket"] == "within_horizon"
    assert later[unit(1)]["expiry_bucket"] == "expired"


def test_utf16_export_is_read(tmp_path, make_config, logger, fixture_result):
    # The real Gesamtdatenexport is UTF-16 with a BOM.
    for src in FIXTURES.glob("*.xml"):
        text = src.read_text(encoding="utf-8").replace('encoding="utf-8"', 'encoding="utf-16"')
        (tmp_path / src.name).write_text(text, encoding="utf-16")
    result = run_pipeline(make_config(source_dir=tmp_path), logger)
    assert result.digest == fixture_result.digest
    assert result.targets[0]["municipality"] == "Köln"


def test_malformed_xml_aborts_the_run(tmp_path, make_config, logger):
    shutil.copy(FIXTURES / "AnlagenEegSolar_1.xml", tmp_path)
    good = (FIXTURES / "EinheitenSolar_1.xml").read_text(encoding="utf-8")
    (tmp_path / "EinheitenSolar_1.xml").write_text(good[: len(good) // 2], encoding="utf-8")
    with pytest.raises(SourceError, match="malformed XML"):
        run_pipeline(make_config(source_dir=tmp_path), logger)


def test_missing_source_files(tmp_path, make_config, logger):
    with pytest.raises(SourceError, match="No EinheitenSolar"):
        run_pipeline(make_config(source_dir=tmp_path), logger)
    with pytest.raises(SourceError, match="not found"):
        run_pipeline(make_config(source_dir=tmp_path / "nope"), logger)


def test_tender_award_present_is_confirmed(rows):
    row = rows[unit(38)]
    assert row["expiry_status"] == "confirmed"
    assert row["tender_award_number"] == "SOL19-1-012"
    assert row["eeg_installed_power_kw"] == 1500.0


def test_tender_check_uses_eeg_installed_power(rows):
    row = rows[unit(37)]
    assert row["eeg_installed_power_kw"] == 2400.0
    assert "2400.0 kW" in row["flag_details"]["TENDER_AWARD_UNVERIFIED"]


def test_alternative_field_spellings_are_read(rows):
    # Unit 39 uses <EinheitMaStRNummer>, its EEG record <VerknuepfteEinheitenMastrNummern>.
    row = rows[unit(39)]
    assert row["expiry_status"] == "confirmed"
    assert "EEG_LINK_INCONSISTENT" not in row["flags"]


def test_field_lookup_ignores_case_only_on_a_miss():
    rec = SourceRecord("f.xml", 1, {"EinheitMaStRNummer": "SEE1", "Ort": "Köln"})
    assert rec.get("EinheitMastrNummer") == "SEE1"
    assert rec.get("Ort") == "Köln"
    assert rec.get("Gemeinde") is None


def test_screen_streams_the_same_rows(make_config, logger, fixture_result):
    rows, rejects = [], []
    summary = screen(make_config(), logger, rows.append, rejects.append)
    assert rows == fixture_result.targets
    assert summary.digest == fixture_result.digest
    assert summary.stats == fixture_result.stats


def test_rows_come_out_in_source_order(fixture_result):
    positions = [int(r["source_unit_file"].split("#")[1]) for r in fixture_result.targets]
    assert positions == sorted(positions)


def test_reader_memory_stays_flat(tmp_path, logger):
    import tracemalloc

    from eeg20_radar.source import SourceFile, iter_records

    record = (FIXTURES / "EinheitenSolar_1.xml").read_text(encoding="utf-8").split("<EinheitSolar>")[1]
    body = "".join(
        "<EinheitSolar>" + record.split("</EinheitSolar>")[0] + "</EinheitSolar>" for _ in range(20_000)
    )
    path = tmp_path / "EinheitenSolar_1.xml"
    path.write_text(f"<EinheitenSolar>{body}</EinheitenSolar>", encoding="utf-8")
    source = SourceFile("unit", path, "", path.stat().st_size)

    tracemalloc.start()
    count = sum(1 for _ in iter_records(source, "EinheitSolar", logger))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert count == 20_000
    # A 20,000-record file is about 18 MB; the reader must not hold it.
    assert peak < 2_000_000, f"peak {peak} bytes"


def test_out_of_scope_units_are_not_kept(make_config, logger):
    rows = []
    summary = screen(make_config(scope="BE"), logger, rows.append, lambda r: None)
    assert [r["mastr_unit_id"] for r in rows] == [unit(24)]
    assert summary.stats["units_unique_in_scope"] == 1

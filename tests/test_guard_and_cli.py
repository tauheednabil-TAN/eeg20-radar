import json

import pytest

from conftest import FIXTURES
from eeg20_radar import db
from eeg20_radar.cli import main
from eeg20_radar.config import load_settings


@pytest.mark.parametrize("schema", ["iris_core", "public", "IRIS_STAGING", "iris_staging_old", ""])
def test_only_iris_staging_is_writable(schema):
    with pytest.raises(db.StagingGuardError):
        db.assert_staging_target(schema)


def test_connect_checks_schema_before_opening_a_connection():
    with pytest.raises(db.StagingGuardError):
        db.connect("postgresql://nobody@invalid.invalid/none", "iris_core")


def test_cli_refuses_non_staging_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@invalid.invalid/none")
    monkeypatch.setenv("EEG20_TARGET_SCHEMA", "iris_core")
    with pytest.raises(db.StagingGuardError):
        main(
            [
                "run",
                "--source-dir",
                str(FIXTURES),
                "--reference-date",
                "2026-01-01",
                "--output-dir",
                str(tmp_path),
            ]
        )


def test_cli_file_only_run(tmp_path):
    code = main(
        [
            "run",
            "--no-db",
            "--source-dir",
            str(FIXTURES),
            "--reference-date",
            "2026-01-01",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert code == 0

    report = json.loads((tmp_path / "quality_report.json").read_text())
    assert report["acceptance_checks"]["passed"] is True
    assert report["scope"] == "NW"
    assert report["counts"]["units_staged"] == 32
    assert "Acceptance evidence" in (tmp_path / "quality_report.md").read_text()
    lines = (tmp_path / "eeg_repowering_targets.csv").read_text().splitlines()
    assert len(lines) == 33
    rejects = (tmp_path / "eeg_repowering_rejects.csv").read_text().splitlines()
    assert len(rejects) == 9
    assert not list(tmp_path.glob("*.partial"))


def test_cli_without_database_url(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    code = main(
        [
            "run",
            "--source-dir",
            str(FIXTURES),
            "--reference-date",
            "2026-01-01",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert code == 2


def test_cli_missing_source_exits_cleanly(tmp_path):
    code = main(
        [
            "run",
            "--no-db",
            "--source-dir",
            str(tmp_path / "missing"),
            "--reference-date",
            "2026-01-01",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert code == 1


def test_settings_defaults():
    s = load_settings({})
    assert s.scope == "NRW"
    assert s.target_schema == "iris_staging"
    assert s.reference_date is None


def test_cli_failed_run_leaves_no_output_files(tmp_path):
    import shutil

    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(FIXTURES / "AnlagenEegSolar_1.xml", src)
    units = (FIXTURES / "EinheitenSolar_1.xml").read_text(encoding="utf-8")
    (src / "EinheitenSolar_1.xml").write_text(units[: len(units) // 2], encoding="utf-8")
    out = tmp_path / "out"

    code = main(
        [
            "run",
            "--no-db",
            "--source-dir",
            str(src),
            "--reference-date",
            "2026-01-01",
            "--output-dir",
            str(out),
        ]
    )
    assert code == 1
    assert list(out.iterdir()) == []


def test_report_caps_reject_samples():
    from eeg20_radar.pipeline import Reject
    from eeg20_radar.report import REJECT_SAMPLE_LIMIT, ReportBuilder

    builder = ReportBuilder()
    for i in range(REJECT_SAMPLE_LIMIT + 50):
        builder.add_reject(Reject("f.xml", i + 1, None, "MISSING_UNIT_ID", "empty", {}))
    assert len(builder.reject_samples) == REJECT_SAMPLE_LIMIT
    assert builder.rejects_by_reason["MISSING_UNIT_ID"] == REJECT_SAMPLE_LIMIT + 50

import logging
import os
from datetime import date
from pathlib import Path

import pytest

from eeg20_radar.pipeline import RunConfig, run_pipeline

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "mastr"
REFERENCE_DATE = date(2026, 1, 1)


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("eeg20_radar.tests")


@pytest.fixture
def make_config():
    def _make(source_dir: Path = FIXTURES, scope: str = "NW", **kwargs) -> RunConfig:
        return RunConfig(
            source_dir=source_dir,
            scope=scope,
            reference_date=kwargs.pop("reference_date", REFERENCE_DATE),
            **kwargs,
        )

    return _make


@pytest.fixture
def fixture_result(make_config, logger):
    return run_pipeline(make_config(), logger)


@pytest.fixture
def rows(fixture_result) -> dict[str, dict]:
    return {r["mastr_unit_id"]: r for r in fixture_result.targets}


def unit(n: int) -> str:
    return f"SEE9000000000{n:02d}"


# --- database ---------------------------------------------------------------

TEST_DB_URL = os.environ.get("EEG20_TEST_DATABASE_URL")
TEST_ADMIN_URL = os.environ.get("EEG20_TEST_ADMIN_URL")


@pytest.fixture
def staging_conn():
    """Connection as the restricted writer role, with empty staging tables."""
    if not TEST_DB_URL:
        pytest.skip("EEG20_TEST_DATABASE_URL not set")
    from eeg20_radar import db

    conn = db.connect(TEST_DB_URL, "iris_staging")
    db.apply_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE iris_staging.eeg_repowering_rejects, "
            "iris_staging.eeg_repowering_target_history, "
            "iris_staging.eeg_repowering_targets, "
            "iris_staging.eeg_ingestion_runs"
        )
    conn.commit()
    yield conn
    conn.close()

"""
Integration tests against a real PostGIS database. They run when
EEG20_TEST_DATABASE_URL points at the restricted writer role
(docker compose --profile test does this) and are skipped otherwise.
"""

import dataclasses
from datetime import date

import psycopg
import pytest

from conftest import TEST_ADMIN_URL, unit
from eeg20_radar import db
from eeg20_radar.pipeline import run_pipeline


def _count(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def test_write_and_read_back(staging_conn, fixture_result, logger):
    db.write_result(staging_conn, fixture_result, "run-1", logger)

    assert _count(staging_conn, "SELECT count(*) FROM iris_staging.eeg_repowering_targets") == 29
    assert _count(staging_conn, "SELECT count(*) FROM iris_staging.eeg_repowering_rejects") == 8
    assert _count(
        staging_conn,
        "SELECT count(*) FROM iris_staging.eeg_repowering_targets "
        "WHERE geom IS NOT NULL AND ST_SRID(geom) = 4326",
    ) == sum(1 for t in fixture_result.targets if t["latitude"] is not None)

    with staging_conn.cursor() as cur:
        cur.execute(
            "SELECT remuneration_end_date, expiry_status, ST_Y(geom), raw_unit->>'Gemeinde' "
            "FROM iris_staging.eeg_repowering_targets WHERE mastr_unit_id = %s",
            (unit(1),),
        )
        assert cur.fetchone() == (date(2026, 12, 31), "confirmed", 50.9375, "Köln")

        cur.execute(
            "SELECT status, result_digest, stats->>'units_staged' "
            "FROM iris_staging.eeg_ingestion_runs WHERE run_id = 'run-1'"
        )
        assert cur.fetchone() == ("succeeded", fixture_result.digest, "29")


def test_rerun_is_idempotent(staging_conn, fixture_result, logger):
    db.write_result(staging_conn, fixture_result, "run-1", logger)
    db.write_result(staging_conn, fixture_result, "run-2", logger)

    assert _count(staging_conn, "SELECT count(*) FROM iris_staging.eeg_repowering_targets") == 29
    assert (
        _count(
            staging_conn,
            "SELECT count(*) FROM iris_staging.eeg_repowering_targets "
            "WHERE first_seen_run_id = 'run-1' AND last_seen_run_id = 'run-2'",
        )
        == 29
    )


def test_refresh_never_deletes_rows(staging_conn, fixture_result, logger):
    db.write_result(staging_conn, fixture_result, "run-1", logger)

    smaller = dataclasses.replace(fixture_result, targets=fixture_result.targets[:10])
    info = db.write_result(staging_conn, smaller, "run-2", logger)

    assert info["rows_not_seen_this_run"] == 19
    assert _count(staging_conn, "SELECT count(*) FROM iris_staging.eeg_repowering_targets") == 29


def test_annual_refresh_moves_buckets(staging_conn, make_config, logger):
    first = run_pipeline(make_config(reference_date=date(2026, 1, 1)), logger)
    later = run_pipeline(make_config(reference_date=date(2027, 1, 1)), logger)
    db.write_result(staging_conn, first, "run-2026", logger)
    db.write_result(staging_conn, later, "run-2027", logger)

    with staging_conn.cursor() as cur:
        cur.execute(
            "SELECT expiry_bucket, reference_date FROM iris_staging.eeg_repowering_targets "
            "WHERE mastr_unit_id = %s",
            (unit(1),),
        )
        assert cur.fetchone() == ("expired", date(2027, 1, 1))


def test_database_rejects_invented_expiry(staging_conn, fixture_result, logger):
    bad = [dict(t) for t in fixture_result.targets]
    row = next(t for t in bad if t["mastr_unit_id"] == unit(7))
    row["expiry_year"], row["remuneration_end_date"] = 2027, date(2027, 12, 31)

    with pytest.raises(psycopg.errors.CheckViolation):
        db.write_result(staging_conn, dataclasses.replace(fixture_result, targets=bad), "run-bad", logger)

    # Nothing from the failed run was kept, and the failure is on record.
    assert _count(staging_conn, "SELECT count(*) FROM iris_staging.eeg_repowering_targets") == 0
    assert (
        _count(
            staging_conn,
            "SELECT count(*) FROM iris_staging.eeg_ingestion_runs "
            "WHERE run_id = 'run-bad' AND status = 'failed'",
        )
        == 1
    )


def test_writer_role_cannot_reach_iris_core(staging_conn):
    with staging_conn.cursor() as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        assert cur.fetchone() == (False,)
        cur.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE pg_get_userbyid(c.relowner) = current_user "
            "AND n.nspname NOT IN ('iris_staging', 'pg_toast')"
        )
        assert cur.fetchone() == (0,)
        # If an iris_core schema exists in this database, the writer must not
        # be able to create anything in it.
        cur.execute(
            "SELECT coalesce(bool_or(has_schema_privilege(current_user, oid, 'CREATE')), false) "
            "FROM pg_namespace WHERE nspname = 'iris_core'"
        )
        assert cur.fetchone() == (False,)


def test_superuser_connection_is_refused():
    if not TEST_ADMIN_URL:
        pytest.skip("EEG20_TEST_ADMIN_URL not set")
    with pytest.raises(db.StagingGuardError, match="superuser"):
        db.connect(TEST_ADMIN_URL, "iris_staging")

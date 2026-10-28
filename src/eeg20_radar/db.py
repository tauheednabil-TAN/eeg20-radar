"""
Staging writer. Refuses to write anywhere except iris_staging.

Guards, in order:
1. the configured schema must be exactly iris_staging;
2. the connected role must not be a superuser;
3. the role must have no CREATE privilege on iris_core (if it exists);
4. all SQL uses fully qualified iris_staging.* names.
"""

import json
import logging
from datetime import UTC, datetime
from importlib import resources

import psycopg
from psycopg.types.json import Jsonb

from eeg20_radar.pipeline import PipelineResult, json_default
from eeg20_radar.rules import RULE_VERSION

STAGING_SCHEMA = "iris_staging"
PROTECTED_SCHEMAS = ("iris_core",)


class StagingGuardError(RuntimeError):
    """Raised when a write could reach anything other than iris_staging."""


def assert_staging_target(schema: str) -> None:
    if schema != STAGING_SCHEMA:
        raise StagingGuardError(
            f"Refusing to write to schema {schema!r}; only {STAGING_SCHEMA!r} is allowed."
        )


def assert_connection_safe(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
        user, is_super = cur.fetchone()
        if is_super:
            raise StagingGuardError(
                f"Role {user!r} is a superuser. Connect as the restricted " "staging writer role instead."
            )

        for schema in PROTECTED_SCHEMAS:
            cur.execute(
                "SELECT has_schema_privilege(current_user, n.oid, 'CREATE') "
                "FROM pg_namespace n WHERE n.nspname = %s",
                (schema,),
            )
            row = cur.fetchone()
            if row and row[0]:
                raise StagingGuardError(
                    f"Role {user!r} can create objects in {schema!r}; "
                    "use a role restricted to iris_staging."
                )

        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (STAGING_SCHEMA,))
        if cur.fetchone() is None:
            raise StagingGuardError(
                f"Schema {STAGING_SCHEMA!r} does not exist; it is created by "
                "docker/initdb/20_iris_staging.sh."
            )


def connect(database_url: str, schema: str) -> psycopg.Connection:
    assert_staging_target(schema)
    conn = psycopg.connect(database_url, autocommit=False)
    try:
        assert_connection_safe(conn)
    except Exception:
        conn.close()
        raise
    return conn


def apply_schema(conn: psycopg.Connection) -> None:
    ddl = resources.files("eeg20_radar").joinpath("sql/schema.sql").read_text()
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()


_COLUMNS = (
    "mastr_unit_id",
    "eeg_mastr_id",
    "eeg_plant_key",
    "unit_name",
    "unit_status_code",
    "unit_status",
    "state_ags",
    "state_code",
    "municipality_key",
    "municipality",
    "postcode",
    "city",
    "latitude",
    "longitude",
    "spatial_precision",
    "gross_power_kw",
    "net_power_kw",
    "unit_commissioning_date",
    "eeg_commissioning_date",
    "remuneration_end_date",
    "expiry_year",
    "expiry_basis",
    "rule_version",
    "expiry_status",
    "expiry_bucket",
    "is_repowering_candidate",
    "flags",
    "flag_details",
    "source_last_updated_utc",
    "eeg_last_updated_utc",
    "source_unit_file",
    "source_eeg_file",
    "unit_record_hash",
    "eeg_record_hash",
    "raw_unit",
    "raw_eeg",
    "reference_date",
    "horizon_years",
)
_JSON_COLUMNS = {"flag_details", "raw_unit", "raw_eeg"}

_UPSERT_SQL = f"""
INSERT INTO iris_staging.eeg_repowering_targets
    ({", ".join(_COLUMNS)}, geom, first_seen_run_id, last_seen_run_id, updated_at)
VALUES
    ({", ".join(f"%({c})s" for c in _COLUMNS)},
     CASE WHEN %(longitude)s::double precision IS NULL THEN NULL
          ELSE ST_SetSRID(ST_MakePoint(%(longitude)s::double precision,
                                       %(latitude)s::double precision), 4326) END,
     %(run_id)s, %(run_id)s, now())
ON CONFLICT (mastr_unit_id) DO UPDATE SET
    {", ".join(f"{c} = EXCLUDED.{c}" for c in _COLUMNS if c != "mastr_unit_id")},
    geom = EXCLUDED.geom,
    last_seen_run_id = EXCLUDED.last_seen_run_id,
    updated_at = now()
"""


def _source_manifest(result: PipelineResult) -> list[dict]:
    return [
        {"kind": s.kind, "file": s.name, "sha256": s.sha256, "size_bytes": s.size_bytes}
        for s in result.unit_sources + result.eeg_sources
    ]


def write_result(
    conn: psycopg.Connection,
    result: PipelineResult,
    run_id: str,
    logger: logging.Logger,
) -> dict:
    """
    Upserts staged rows and appends rejects in one transaction. Rows already
    in the table but absent from this run are kept (no deletes); they keep
    their older last_seen_run_id so a refresh never loses data.
    """
    cfg = result.config
    started = datetime.now(UTC)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO iris_staging.eeg_ingestion_runs "
            "(run_id, started_at, status, scope, reference_date, horizon_years, "
            " rule_version, source_files) "
            "VALUES (%s, %s, 'running', %s, %s, %s, %s, %s)",
            (
                run_id,
                started,
                cfg.scope,
                cfg.reference_date,
                cfg.horizon_years,
                RULE_VERSION,
                Jsonb(_source_manifest(result)),
            ),
        )
    conn.commit()

    try:
        with conn.cursor() as cur:
            params = []
            for row in result.targets:
                p = {
                    c: Jsonb(row[c], dumps=lambda v: json.dumps(v, default=json_default))
                    if c in _JSON_COLUMNS and row[c] is not None
                    else row[c]
                    for c in _COLUMNS
                }
                p["run_id"] = run_id
                params.append(p)
            if params:
                cur.executemany(_UPSERT_SQL, params)

            if result.rejects:
                cur.executemany(
                    "INSERT INTO iris_staging.eeg_repowering_rejects "
                    "(run_id, source_file, record_index, record_id, reason_code, detail, raw) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            run_id,
                            r.source_file,
                            r.record_index,
                            r.record_id,
                            r.reason_code,
                            r.detail,
                            Jsonb(r.raw),
                        )
                        for r in result.rejects
                    ],
                )

            cur.execute(
                "SELECT count(*) FROM iris_staging.eeg_repowering_targets " "WHERE last_seen_run_id <> %s",
                (run_id,),
            )
            stale = cur.fetchone()[0]

            stats = {**result.stats, "rows_not_seen_this_run": stale}
            cur.execute(
                "UPDATE iris_staging.eeg_ingestion_runs SET status = 'succeeded', "
                "finished_at = now(), stats = %s, result_digest = %s WHERE run_id = %s",
                (Jsonb(stats), result.digest, run_id),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.exception("Staging write failed; transaction rolled back")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE iris_staging.eeg_ingestion_runs SET status = 'failed', "
                "finished_at = now(), error = %s WHERE run_id = %s",
                (str(exc)[:2000], run_id),
            )
        conn.commit()
        raise

    logger.info(
        "Staged %d rows, %d rejects, %d older rows kept",
        len(result.targets),
        len(result.rejects),
        stale,
    )
    return {"rows_not_seen_this_run": stale}

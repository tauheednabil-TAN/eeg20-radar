"""
Staging writer. Refuses to write anywhere except iris_staging.

Guards, in order:
1. the configured schema must be exactly iris_staging;
2. the connected role must not be a superuser;
3. if an iris_core schema exists, the role must have no CREATE privilege on
   it and no INSERT, UPDATE, DELETE or TRUNCATE privilege on its tables;
4. all SQL uses fully qualified iris_staging.* names.
"""

import json
import logging
from datetime import UTC, datetime
from importlib import resources

import psycopg
from psycopg.types.json import Jsonb

from eeg20_radar.pipeline import PipelineResult, Reject, RunConfig, json_default
from eeg20_radar.rules import RULE_VERSION
from eeg20_radar.source import SourceFile

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
            cur.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
                "AND has_table_privilege(current_user, c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE') "
                "ORDER BY c.relname LIMIT 5",
                (schema,),
            )
            writable = [r[0] for r in cur.fetchall()]
            if writable:
                raise StagingGuardError(
                    f"Role {user!r} can modify tables in {schema!r} ({', '.join(writable)}); "
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
    "tender_award_number",
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
    "eeg_installed_power_kw",
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


_HISTORY_COLUMNS = (
    "mastr_unit_id",
    "reference_date",
    "horizon_years",
    "rule_version",
    "eeg_commissioning_date",
    "remuneration_end_date",
    "expiry_year",
    "expiry_status",
    "expiry_bucket",
    "is_repowering_candidate",
    "flags",
    "unit_record_hash",
    "eeg_record_hash",
)

_HISTORY_SQL = f"""
INSERT INTO iris_staging.eeg_repowering_target_history
    (run_id, {", ".join(_HISTORY_COLUMNS)})
VALUES
    (%(run_id)s, {", ".join(f"%({c})s" for c in _HISTORY_COLUMNS)})
"""

_REJECT_SQL = (
    "INSERT INTO iris_staging.eeg_repowering_rejects "
    "(run_id, source_file, record_index, record_id, reason_code, detail, raw) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s)"
)


def _dumps(value) -> str:
    return json.dumps(value, default=json_default)


class StagingWriter:
    """
    Writes one run into iris_staging while rows are still being produced.

    start() records the run and commits, so a failed run stays visible.
    Rows and rejects are then written in batches inside one transaction;
    finish() commits it, fail() rolls it back and marks the run failed.
    Rows already in the table but absent from this run are kept (no
    deletes) with their older last_seen_run_id, and every run's assessment
    is appended to eeg_repowering_target_history, so a refresh loses nothing.
    """

    def __init__(
        self,
        conn: psycopg.Connection,
        run_id: str,
        cfg: RunConfig,
        sources: list[SourceFile],
        logger: logging.Logger,
        batch_size: int = 1000,
    ) -> None:
        self.conn, self.run_id, self.cfg, self.sources = conn, run_id, cfg, sources
        self.logger, self.batch_size = logger, batch_size
        self._rows: list[dict] = []
        self._rejects: list[tuple] = []
        self.rows_written = 0
        self.rejects_written = 0

    def start(self) -> None:
        manifest = [
            {"kind": s.kind, "file": s.name, "sha256": s.sha256, "size_bytes": s.size_bytes}
            for s in self.sources
        ]
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO iris_staging.eeg_ingestion_runs "
                "(run_id, started_at, status, scope, reference_date, horizon_years, "
                " rule_version, source_files) "
                "VALUES (%s, %s, 'running', %s, %s, %s, %s, %s)",
                (
                    self.run_id,
                    datetime.now(UTC),
                    self.cfg.scope,
                    self.cfg.reference_date,
                    self.cfg.horizon_years,
                    RULE_VERSION,
                    Jsonb(manifest),
                ),
            )
        self.conn.commit()

    def add_target(self, row: dict) -> None:
        params = {
            c: Jsonb(row[c], dumps=_dumps) if c in _JSON_COLUMNS and row[c] is not None else row[c]
            for c in _COLUMNS
        }
        params["run_id"] = self.run_id
        self._rows.append(params)
        if len(self._rows) >= self.batch_size:
            self._flush_rows()

    def add_reject(self, r: Reject) -> None:
        self._rejects.append(
            (self.run_id, r.source_file, r.record_index, r.record_id, r.reason_code, r.detail, Jsonb(r.raw))
        )
        if len(self._rejects) >= self.batch_size:
            self._flush_rejects()

    def _flush_rows(self) -> None:
        if not self._rows:
            return
        with self.conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, self._rows)
            cur.executemany(_HISTORY_SQL, self._rows)
        self.rows_written += len(self._rows)
        self._rows.clear()

    def _flush_rejects(self) -> None:
        if not self._rejects:
            return
        with self.conn.cursor() as cur:
            cur.executemany(_REJECT_SQL, self._rejects)
        self.rejects_written += len(self._rejects)
        self._rejects.clear()

    def finish(self, stats: dict, digest: str) -> dict:
        self._flush_rows()
        self._flush_rejects()
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM iris_staging.eeg_repowering_targets WHERE last_seen_run_id <> %s",
                (self.run_id,),
            )
            stale = cur.fetchone()[0]
            info = {"rows_not_seen_this_run": stale, "history_rows_written": self.rows_written}
            cur.execute(
                "UPDATE iris_staging.eeg_ingestion_runs SET status = 'succeeded', "
                "finished_at = now(), stats = %s, result_digest = %s WHERE run_id = %s",
                (Jsonb({**stats, **info}), digest, self.run_id),
            )
        self.conn.commit()
        self.logger.info(
            "Staged %d rows, %d rejects, %d older rows kept",
            self.rows_written,
            self.rejects_written,
            stale,
        )
        return info

    def fail(self, exc: BaseException) -> None:
        self.conn.rollback()
        self.logger.error("Staging write failed; transaction rolled back: %s", exc)
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE iris_staging.eeg_ingestion_runs SET status = 'failed', "
                "finished_at = now(), error = %s WHERE run_id = %s",
                (str(exc)[:2000], self.run_id),
            )
        self.conn.commit()


def write_result(
    conn: psycopg.Connection,
    result: PipelineResult,
    run_id: str,
    logger: logging.Logger,
) -> dict:
    """Writes an in-memory result (tests and small runs) through StagingWriter."""
    writer = StagingWriter(conn, run_id, result.config, result.unit_sources + result.eeg_sources, logger)
    writer.start()
    try:
        for row in result.targets:
            writer.add_target(row)
        for reject in result.rejects:
            writer.add_reject(reject)
        return writer.finish(result.stats, result.digest)
    except Exception as exc:
        writer.fail(exc)
        raise

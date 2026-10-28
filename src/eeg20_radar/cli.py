"""Command line entry point: python -m eeg20_radar <command>."""

import argparse
import logging
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from eeg20_radar.config import load_settings
from eeg20_radar.pipeline import RunConfig, normalize_scope, run_pipeline
from eeg20_radar.report import build_report, write_outputs
from eeg20_radar.source import SourceError

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [source=%(source)s record=%(record)s] %(message)s"


class _ContextDefaults(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.source = getattr(record, "source", "-")
        record.record = getattr(record, "record", "-")
        return True


def setup_logging(level: str) -> logging.Logger:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(_ContextDefaults())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    return logging.getLogger("eeg20_radar")


def build_parser() -> argparse.ArgumentParser:
    s = load_settings()
    parser = argparse.ArgumentParser(prog="eeg20_radar", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="screen the source files and stage the result")
    run.add_argument("--source-dir", type=Path, default=s.source_dir)
    run.add_argument("--scope", default=s.scope, help="NRW (default), DE for all of Germany, or a state code")
    run.add_argument(
        "--reference-date",
        type=date.fromisoformat,
        default=s.reference_date,
        help="date the screening is evaluated against (YYYY-MM-DD); default today",
    )
    run.add_argument("--horizon-years", type=int, default=s.horizon_years)
    run.add_argument("--output-dir", type=Path, default=s.output_dir)
    run.add_argument("--no-db", action="store_true", help="write report and CSV only")

    sub.add_parser("init-db", help="create the staging tables in iris_staging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    logger = setup_logging(settings.log_level)

    if args.command == "init-db":
        from eeg20_radar import db

        if not settings.database_url:
            logger.error("DATABASE_URL is not set")
            return 2
        with db.connect(settings.database_url, settings.target_schema) as conn:
            db.apply_schema(conn)
        logger.info("Staging tables ready in iris_staging")
        return 0

    reference_date = args.reference_date or date.today()
    if args.reference_date is None:
        logger.warning("No reference date given; using today (%s)", reference_date)

    cfg = RunConfig(
        source_dir=args.source_dir,
        scope=normalize_scope(args.scope),
        reference_date=reference_date,
        horizon_years=args.horizon_years,
    )

    try:
        result = run_pipeline(cfg, logger)
    except SourceError as exc:
        logger.error("Run aborted, nothing staged: %s", exc)
        return 1

    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"

    db_info = None
    if not args.no_db:
        from eeg20_radar import db  # imported here so --no-db runs need no driver

        if not settings.database_url:
            logger.error("DATABASE_URL is not set (use --no-db for a file-only run)")
            return 2
        db.assert_staging_target(settings.target_schema)
        with db.connect(settings.database_url, settings.target_schema) as conn:
            db.apply_schema(conn)
            db_info = db.write_result(conn, result, run_id, logger)

    report = build_report(result, run_id, db_info)
    paths = write_outputs(report, result, args.output_dir)
    for name, path in paths.items():
        logger.info("Wrote %s: %s", name, path)

    if not report["acceptance_checks"]["passed"]:
        logger.error("Acceptance checks failed: %s", report["acceptance_checks"])
        return 3
    return 0

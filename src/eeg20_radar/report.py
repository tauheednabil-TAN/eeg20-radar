"""
Quality report (Markdown + JSON) and CSV exports.

ReportBuilder and OutputFiles take rows one at a time, so a national run
never holds all rows in memory. The report keeps counts plus a capped sample
of rejects; the full reject list goes to the CSV and the database.
"""

import csv
import json
from collections import Counter
from pathlib import Path

from eeg20_radar.catalog import STATE_ABBR_TO_AGS, STATES
from eeg20_radar.pipeline import FLAG_SEVERITY, PipelineResult, Reject, RunConfig, json_default
from eeg20_radar.rules import RULE_VERSION
from eeg20_radar.source import SourceFile

REJECT_SAMPLE_LIMIT = 200
ID_LIST_LIMIT = 100

CSV_COLUMNS = (
    "mastr_unit_id",
    "eeg_mastr_id",
    "unit_name",
    "unit_status",
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
    "expiry_status",
    "expiry_bucket",
    "is_repowering_candidate",
    "flags",
)
REJECT_CSV_COLUMNS = ("source_file", "record_index", "record_id", "reason_code", "detail")


class ReportBuilder:
    def __init__(self) -> None:
        self.staged = 0
        self.by_status = Counter()
        self.by_bucket = Counter()
        self.by_precision = Counter()
        self.flags = Counter()
        self.flag_examples: dict[str, str] = {}
        self.candidates = 0
        self.candidate_kw = 0.0
        self.candidates_by_year = Counter()
        self.invented: list[str] = []
        self.invented_count = 0
        self.without_basis: list[str] = []
        self.without_basis_count = 0
        self.rejects_by_reason = Counter()
        self.reject_samples: list[Reject] = []

    def add_target(self, row: dict) -> None:
        uid = row["mastr_unit_id"]
        self.staged += 1
        self.by_status[row["expiry_status"]] += 1
        self.by_bucket[row["expiry_bucket"]] += 1
        self.by_precision[row["spatial_precision"]] += 1
        for code in row["flags"]:
            self.flags[code] += 1
            self.flag_examples.setdefault(code, uid)
        if row["is_repowering_candidate"]:
            self.candidates += 1
            self.candidate_kw += row["net_power_kw"] or 0
            self.candidates_by_year[row["expiry_year"]] += 1
        # Acceptance evidence, recomputed from the staged rows.
        if row["expiry_status"] == "incomplete" and row["expiry_year"] is not None:
            self.invented_count += 1
            if len(self.invented) < ID_LIST_LIMIT:
                self.invented.append(uid)
        if row["expiry_year"] is not None and row["eeg_commissioning_date"] is None:
            self.without_basis_count += 1
            if len(self.without_basis) < ID_LIST_LIMIT:
                self.without_basis.append(uid)

    def add_reject(self, reject: Reject) -> None:
        self.rejects_by_reason[reject.reason_code] += 1
        if len(self.reject_samples) < REJECT_SAMPLE_LIMIT:
            self.reject_samples.append(reject)

    def build(
        self,
        cfg: RunConfig,
        sources: list[SourceFile],
        stats: dict,
        digest: str,
        run_id: str,
        db_info: dict | None,
    ) -> dict:
        staged = self.staged
        rejected = sum(self.rejects_by_reason.values())
        return {
            "run_id": run_id,
            "rule_version": RULE_VERSION,
            "scope": cfg.scope,
            "reference_date": cfg.reference_date.isoformat(),
            "horizon_years": cfg.horizon_years,
            "result_digest": digest,
            "sources": [
                {"kind": s.kind, "file": s.name, "sha256": s.sha256, "size_bytes": s.size_bytes}
                for s in sources
            ],
            "counts": stats,
            "staged_by_status": dict(sorted(self.by_status.items())),
            "coverage_confirmed_pct": round(100 * self.by_status["confirmed"] / staged, 1) if staged else 0.0,
            "coverage_with_expiry_pct": round(100 * (staged - self.by_status["incomplete"]) / staged, 1)
            if staged
            else 0.0,
            "staged_by_bucket": dict(sorted(self.by_bucket.items())),
            "spatial_precision": dict(sorted(self.by_precision.items())),
            "flags": {
                code: {"count": n, "severity": FLAG_SEVERITY[code], "example_unit": self.flag_examples[code]}
                for code, n in sorted(self.flags.items())
            },
            "rejects_by_reason": dict(sorted(self.rejects_by_reason.items())),
            "rejects_sample": [
                {
                    "source": f"{r.source_file}#{r.record_index}",
                    "record_id": r.record_id,
                    "reason": r.reason_code,
                    "detail": r.detail,
                }
                for r in sorted(self.reject_samples, key=lambda r: (r.source_file, r.record_index))
            ],
            "rejects_sample_truncated": rejected > len(self.reject_samples),
            "repowering_candidates": {
                "count": self.candidates,
                "net_power_kw": round(self.candidate_kw, 2),
                "by_expiry_year": dict(sorted(self.candidates_by_year.items())),
            },
            "acceptance_checks": {
                "incomplete_rows_with_expiry": self.invented_count,
                "incomplete_rows_with_expiry_ids": self.invented,
                "expiry_without_eeg_date": self.without_basis_count,
                "expiry_without_eeg_date_ids": self.without_basis,
                "passed": self.invented_count == 0 and self.without_basis_count == 0,
            },
            "database": db_info,
        }


def _scope_label(scope: str) -> str:
    if scope == "DE":
        return "DE (all of Germany)"
    ags = STATE_ABBR_TO_AGS[scope]
    return f"{scope} ({STATES[ags][1]})"


def _table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def render_markdown(report: dict) -> str:
    c = report["counts"]
    acc = report["acceptance_checks"]
    rejects = report["rejects_sample"]

    parts = [
        "# EEG 20-year expiry radar: quality report",
        "",
        "Indicative screening only. Not legal advice or a permit decision.",
        "",
        _table(
            ["Item", "Value"],
            [
                ["Run", f"`{report['run_id']}`"],
                ["Scope", _scope_label(report["scope"])],
                ["Reference date", report["reference_date"]],
                ["Horizon", f"{report['horizon_years']} years"],
                ["Rule version", report["rule_version"]],
                ["Result digest (sha256)", f"`{report['result_digest'][:16]}…`"],
            ],
        ),
        "",
        "## Sources",
        "",
        _table(
            ["Kind", "File", "sha256", "Bytes"],
            [[s["kind"], s["file"], f"`{s['sha256'][:12]}…`", s["size_bytes"]] for s in report["sources"]],
        ),
        "",
        "## Record flow",
        "",
        _table(
            ["Step", "Records"],
            [
                ["Unit records read", c["units_read"]],
                ["EEG records read", c["eeg_records_read"]],
                ["Out of scope (other region)", c["units_out_of_scope"]],
                ["Unique valid units in scope", c["units_unique_in_scope"]],
                ["Rejected (all reasons)", c["rejected"]],
                ["Staged in `iris_staging.eeg_repowering_targets`", c["units_staged"]],
                ["EEG records linked to a staged unit", c["eeg_records_linked"]],
                ["EEG records not linked to any in-scope unit", c["eeg_records_not_linked"]],
            ],
        ),
        "",
        "## Coverage",
        "",
        _table(
            ["Expiry status", "Rows"],
            [[k, v] for k, v in report["staged_by_status"].items()],
        ),
        "",
        f"- Rows with a derived expiry year: {report['coverage_with_expiry_pct']}%",
        f"- Rows confirmed without review flags: {report['coverage_confirmed_pct']}%",
        "",
        _table(["Expiry bucket", "Rows"], [[k, v] for k, v in report["staged_by_bucket"].items()]),
        "",
        _table(["Spatial precision", "Rows"], [[k, v] for k, v in report["spatial_precision"].items()]),
        "",
        "## Repowering candidates",
        "",
        f"{report['repowering_candidates']['count']} units, "
        f"{report['repowering_candidates']['net_power_kw']} kW net "
        "(operating or temporarily shut down, expiry known, expired or within horizon).",
        "",
        _table(
            ["Expiry year", "Units"],
            [[k, v] for k, v in report["repowering_candidates"]["by_expiry_year"].items()],
        ),
        "",
        "## Flags on staged rows",
        "",
        _table(
            ["Flag", "Severity", "Rows", "Example unit"],
            [[f"`{k}`", v["severity"], v["count"], v["example_unit"]] for k, v in report["flags"].items()],
        ),
        "",
        "`blocking` = no expiry derived (`incomplete`); `review` = expiry derived but needs a human check; "
        "`info` = noted only.",
        "",
        "## Rejected records",
        "",
        _table(["Reason", "Records"], [[f"`{k}`", v] for k, v in report["rejects_by_reason"].items()])
        if report["rejects_by_reason"]
        else "None.",
        "",
    ]
    if rejects:
        parts += [
            f"First {len(rejects)} rejected records (all of them are in `eeg_repowering_rejects.csv`):"
            if report["rejects_sample_truncated"]
            else "All rejected records:",
            "",
            _table(
                ["Source", "Record", "Reason", "Detail"],
                [[r["source"], r["record_id"] or "(none)", f"`{r['reason']}`", r["detail"]] for r in rejects],
            ),
            "",
        ]
    parts += [
        "## Acceptance evidence",
        "",
        f"- Incomplete rows carrying an expiry date: {acc['incomplete_rows_with_expiry']} (must be 0)",
        "- Expiry dates not derived from an EEG commissioning date: "
        f"{acc['expiry_without_eeg_date']} (must be 0)",
        "- The database enforces the same rule with the `eeg_targets_no_invented_expiry` check constraint.",
        f"- Overall: **{'PASS' if acc['passed'] else 'FAIL'}**",
        "",
    ]
    if report.get("database"):
        parts += [
            "## Database",
            "",
            f"- Rows already staged but not present in this run (kept, not deleted): "
            f"{report['database']['rows_not_seen_this_run']}",
            f"- Assessments recorded in `eeg_repowering_target_history` for this run: "
            f"{report['database']['history_rows_written']}",
            "",
        ]
    return "\n".join(parts)


class OutputFiles:
    """
    Streams both CSVs to *.partial files and renames them on success, so a
    failed run never leaves a half-written file under the final name.
    """

    def __init__(self, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = output_dir
        self.paths = {
            "targets_csv": output_dir / "eeg_repowering_targets.csv",
            "rejects_csv": output_dir / "eeg_repowering_rejects.csv",
            "report_md": output_dir / "quality_report.md",
            "report_json": output_dir / "quality_report.json",
        }
        self._handles = {}
        self._writers = {}
        for key, columns in (("targets_csv", CSV_COLUMNS), ("rejects_csv", REJECT_CSV_COLUMNS)):
            handle = self._partial(key).open("w", newline="", encoding="utf-8")
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            self._handles[key], self._writers[key] = handle, writer

    def _partial(self, key: str) -> Path:
        path = self.paths[key]
        return path.with_name(path.name + ".partial")

    def add_target(self, row: dict) -> None:
        self._writers["targets_csv"].writerow({**row, "flags": ";".join(row["flags"])})

    def add_reject(self, reject: Reject) -> None:
        self._writers["rejects_csv"].writerow(
            {
                "source_file": reject.source_file,
                "record_index": reject.record_index,
                "record_id": reject.record_id,
                "reason_code": reject.reason_code,
                "detail": reject.detail,
            }
        )

    def finish(self, report: dict) -> dict[str, Path]:
        for handle in self._handles.values():
            handle.close()
        for key in ("targets_csv", "rejects_csv"):
            self._partial(key).replace(self.paths[key])
        self.paths["report_md"].write_text(render_markdown(report), encoding="utf-8")
        self.paths["report_json"].write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=json_default) + "\n",
            encoding="utf-8",
        )
        return self.paths

    def abort(self) -> None:
        for key, handle in self._handles.items():
            handle.close()
            self._partial(key).unlink(missing_ok=True)


def build_report(result: PipelineResult, run_id: str, db_info: dict | None) -> dict:
    """Report for an in-memory result (tests and small runs)."""
    builder = ReportBuilder()
    for row in result.targets:
        builder.add_target(row)
    for reject in result.rejects:
        builder.add_reject(reject)
    return builder.build(
        result.config,
        result.unit_sources + result.eeg_sources,
        result.stats,
        result.digest,
        run_id,
        db_info,
    )

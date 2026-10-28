"""Quality report (Markdown + JSON) and CSV export of the staged rows."""

import csv
import json
from collections import Counter
from pathlib import Path

from eeg20_radar.catalog import STATE_ABBR_TO_AGS, STATES
from eeg20_radar.pipeline import FLAG_SEVERITY, PipelineResult, json_default
from eeg20_radar.rules import RULE_VERSION

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


def build_report(result: PipelineResult, run_id: str, db_info: dict | None) -> dict:
    cfg = result.config
    targets = result.targets
    staged = len(targets)

    by_status = Counter(t["expiry_status"] for t in targets)
    candidates = [t for t in targets if t["is_repowering_candidate"]]

    # Acceptance evidence, recomputed from the staged rows.
    invented = [
        t["mastr_unit_id"]
        for t in targets
        if t["expiry_status"] == "incomplete" and t["expiry_year"] is not None
    ]
    without_basis = [
        t["mastr_unit_id"]
        for t in targets
        if t["expiry_year"] is not None and t["eeg_commissioning_date"] is None
    ]

    return {
        "run_id": run_id,
        "rule_version": RULE_VERSION,
        "scope": cfg.scope,
        "reference_date": cfg.reference_date.isoformat(),
        "horizon_years": cfg.horizon_years,
        "result_digest": result.digest,
        "sources": [
            {"kind": s.kind, "file": s.name, "sha256": s.sha256, "size_bytes": s.size_bytes}
            for s in result.unit_sources + result.eeg_sources
        ],
        "counts": result.stats,
        "staged_by_status": dict(sorted(by_status.items())),
        "coverage_confirmed_pct": round(100 * by_status["confirmed"] / staged, 1) if staged else 0.0,
        "coverage_with_expiry_pct": round(100 * (staged - by_status["incomplete"]) / staged, 1)
        if staged
        else 0.0,
        "staged_by_bucket": dict(sorted(Counter(t["expiry_bucket"] for t in targets).items())),
        "spatial_precision": dict(sorted(Counter(t["spatial_precision"] for t in targets).items())),
        "flags": {
            code: {"count": n, "severity": FLAG_SEVERITY[code]}
            for code, n in sorted(Counter(f for t in targets for f in t["flags"]).items())
        },
        "rejects_by_reason": dict(sorted(Counter(r.reason_code for r in result.rejects).items())),
        "rejects": [
            {
                "source": f"{r.source_file}#{r.record_index}",
                "record_id": r.record_id,
                "reason": r.reason_code,
                "detail": r.detail,
            }
            for r in sorted(result.rejects, key=lambda r: (r.source_file, r.record_index))
        ],
        "repowering_candidates": {
            "count": len(candidates),
            "net_power_kw": round(sum(t["net_power_kw"] or 0 for t in candidates), 2),
            "by_expiry_year": dict(sorted(Counter(t["expiry_year"] for t in candidates).items())),
        },
        "acceptance_checks": {
            "incomplete_rows_with_expiry": invented,
            "expiry_without_eeg_date": without_basis,
            "passed": not invented and not without_basis,
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


def render_markdown(report: dict, result: PipelineResult) -> str:
    c = report["counts"]
    acc = report["acceptance_checks"]
    examples = {}
    for t in result.targets:
        for code in t["flags"]:
            examples.setdefault(code, t["mastr_unit_id"])

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
                ["Unique valid units", c["units_unique"]],
                ["Out of scope (other region)", c["units_out_of_scope"]],
                ["Rejected (all reasons)", c["rejected"]],
                ["Staged in `iris_staging.eeg_repowering_targets`", c["units_staged"]],
                ["EEG records not linked to a staged unit", c["eeg_records_unreferenced_in_scope"]],
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
            [[f"`{k}`", v["severity"], v["count"], examples.get(k, "")] for k, v in report["flags"].items()],
        ),
        "",
        "`blocking` = no expiry derived (`incomplete`); `review` = expiry derived but needs a human check; "
        "`info` = noted only.",
        "",
        "## Rejected records",
        "",
        _table(
            ["Source", "Record", "Reason", "Detail"],
            [
                [r["source"], r["record_id"] or "(none)", f"`{r['reason']}`", r["detail"]]
                for r in report["rejects"]
            ],
        )
        if report["rejects"]
        else "None.",
        "",
        "## Acceptance evidence",
        "",
        f"- Incomplete rows carrying an expiry date: "
        f"{len(acc['incomplete_rows_with_expiry'])} (must be 0)",
        f"- Expiry dates not derived from an EEG commissioning date: "
        f"{len(acc['expiry_without_eeg_date'])} (must be 0)",
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
            "",
        ]
    return "\n".join(parts)


def write_outputs(
    report: dict,
    result: PipelineResult,
    output_dir: Path,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "report_md": output_dir / "quality_report.md",
        "report_json": output_dir / "quality_report.json",
        "targets_csv": output_dir / "eeg_repowering_targets.csv",
    }
    paths["report_md"].write_text(render_markdown(report, result), encoding="utf-8")
    paths["report_json"].write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=json_default) + "\n",
        encoding="utf-8",
    )
    with paths["targets_csv"].open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in result.targets:
            writer.writerow({**row, "flags": ";".join(row["flags"])})
    return paths

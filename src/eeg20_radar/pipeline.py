"""
Screening pipeline: read -> validate -> de-duplicate -> scope -> derive.

Pure (no database access) so the same inputs always give the same rows.
"""

import hashlib
import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from eeg20_radar import catalog
from eeg20_radar.normalize import (
    AGS_RE,
    EEG_ID_RE,
    POSTCODE_RE,
    UNIT_ID_RE,
    clean,
    in_bbox,
    parse_coordinates,
    parse_date,
    parse_kw,
    parse_timestamp,
    split_ids,
)
from eeg20_radar.rules import RULE_VERSION, compute_expiry, expiry_bucket
from eeg20_radar.source import (
    EEG_TAG,
    UNIT_TAG,
    SourceFile,
    SourceRecord,
    discover,
    iter_records,
)

INFO, REVIEW, BLOCKING = "info", "review", "blocking"

# Flag code -> severity. BLOCKING means no expiry date can be derived.
FLAG_SEVERITY = {
    "NO_EEG_LINK": BLOCKING,
    "EEG_ID_INVALID": BLOCKING,
    "EEG_RECORD_NOT_FOUND": BLOCKING,
    "EEG_DATE_MISSING": BLOCKING,
    "EEG_DATE_INVALID": BLOCKING,
    "EEG_DATE_IN_FUTURE": BLOCKING,
    "UNIT_NOT_COMMISSIONED": BLOCKING,
    "DATE_MISMATCH_UNIT_VS_EEG": REVIEW,
    "UNIT_DATE_INVALID": REVIEW,
    "UNIT_TEMPORARILY_DECOMMISSIONED": REVIEW,
    "UNIT_PERMANENTLY_DECOMMISSIONED": REVIEW,
    "UNIT_STATUS_UNKNOWN": REVIEW,
    "NOT_GRID_OPERATOR_VERIFIED": REVIEW,
    "EEG_LINK_INCONSISTENT": REVIEW,
    "DUPLICATE_CONFLICT": REVIEW,
    "REGION_CONFLICT": REVIEW,
    "COORDINATES_OUTSIDE_REGION_BBOX": REVIEW,
    "IMPLAUSIBLE_EARLY_DATE": REVIEW,
    "EEG2021_TERM_RULE_UNVERIFIED": REVIEW,
    "PRE_EEG2000_DEEMED_2000": INFO,
    "SHARED_EEG_ANLAGE": INFO,
    "COORDINATES_INVALID": INFO,
    "POWER_INVALID": INFO,
    "POSTCODE_INVALID": INFO,
    "TIMESTAMP_INVALID": INFO,
}


@dataclass(frozen=True)
class RunConfig:
    source_dir: Path
    scope: str  # "DE" or a state abbreviation such as "NW"
    reference_date: date
    horizon_years: int = 5
    date_mismatch_tolerance_days: int = 31


@dataclass(frozen=True)
class Reject:
    source_file: str
    record_index: int
    record_id: str | None
    reason_code: str
    detail: str
    raw: dict


@dataclass
class PipelineResult:
    config: RunConfig
    unit_sources: list[SourceFile]
    eeg_sources: list[SourceFile]
    targets: list[dict]
    rejects: list[Reject]
    stats: dict = field(default_factory=dict)

    @property
    def digest(self) -> str:
        """Hash of the staged rows; equal inputs must give an equal digest."""
        payload = json.dumps(self.targets, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()


def normalize_scope(scope: str) -> str:
    scope = scope.strip().upper()
    if scope in ("NRW", "NW"):
        return "NW"
    if scope == "DE" or scope in catalog.STATE_ABBR_TO_AGS:
        return scope
    raise ValueError(
        f"Unknown scope {scope!r}; use DE or one of "
        f"{', '.join(sorted(catalog.STATE_ABBR_TO_AGS))} (NRW = NW)"
    )


def record_hash(record: SourceRecord) -> str:
    payload = json.dumps(record.fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


class _Flags:
    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def add(self, code: str, detail: str = "") -> None:
        self.items.setdefault(code, detail)

    def has(self, severity: str) -> bool:
        return any(FLAG_SEVERITY[c] == severity for c in self.items)

    def codes(self) -> list[str]:
        return sorted(self.items)


def _dedupe(
    records: list[SourceRecord],
    id_field: str,
    rejects: list[Reject],
    logger: logging.Logger,
) -> tuple[dict[str, SourceRecord], set[str]]:
    """
    Keeps one record per ID: the newest DatumLetzteAktualisierung wins, ties
    go to the earliest position in the source. Losers are rejected with the
    reason, nothing is dropped silently. Returns the winners and the IDs
    whose duplicates disagreed without a newer timestamp to decide.
    """
    groups: dict[str, list[SourceRecord]] = defaultdict(list)
    for rec in records:
        groups[rec.get(id_field)].append(rec)

    winners: dict[str, SourceRecord] = {}
    conflicts: set[str] = set()

    for record_id, group in groups.items():
        if len(group) == 1:
            winners[record_id] = group[0]
            continue

        def sort_key(rec: SourceRecord):
            ts, _ = parse_timestamp(rec.get("DatumLetzteAktualisierung"))
            return (-(ts.timestamp()) if ts else float("inf"), rec.file, rec.index)

        ordered = sorted(group, key=sort_key)
        winner = ordered[0]
        winners[record_id] = winner
        winner_ts = sort_key(winner)[0]

        for loser in ordered[1:]:
            if loser.fields == winner.fields:
                code, detail = "DUPLICATE_EXACT", "identical copy"
            else:
                code = "DUPLICATE_SUPERSEDED"
                detail = f"superseded by {winner.file}#{winner.index}"
                if sort_key(loser)[0] == winner_ts:
                    conflicts.add(record_id)
                    detail += " (same timestamp, contents differ)"
            rejects.append(Reject(loser.file, loser.index, record_id, code, detail, loser.fields))
            logger.warning(
                "%s: %s",
                code,
                detail,
                extra={"source": loser.file, "record": record_id},
            )

    return winners, conflicts


def _region(rec: SourceRecord, flags: _Flags) -> str | None:
    """Returns the 2-digit AGS state prefix, preferring the AGS itself."""
    ags = clean(rec.get("Gemeindeschluessel"))
    ags_state = ags[:2] if ags and AGS_RE.match(ags) and ags[:2] in catalog.STATES else None
    bl_state = catalog.BUNDESLAND_CODE_TO_AGS.get(clean(rec.get("Bundesland")) or "")

    if ags_state and bl_state and ags_state != bl_state:
        flags.add(
            "REGION_CONFLICT",
            f"Gemeindeschluessel {ags} vs Bundesland code {rec.get('Bundesland')}",
        )
    return ags_state or bl_state


def run_pipeline(cfg: RunConfig, logger: logging.Logger) -> PipelineResult:
    scope = normalize_scope(cfg.scope)
    scope_ags = None if scope == "DE" else catalog.STATE_ABBR_TO_AGS[scope]

    unit_sources, eeg_sources = discover(cfg.source_dir)
    units_raw = [r for s in unit_sources for r in iter_records(s, UNIT_TAG, logger)]
    eeg_raw = [r for s in eeg_sources for r in iter_records(s, EEG_TAG, logger)]

    rejects: list[Reject] = []

    def reject(rec: SourceRecord, record_id, code: str, detail: str) -> None:
        rejects.append(Reject(rec.file, rec.index, record_id, code, detail, rec.fields))
        logger.warning("%s: %s", code, detail, extra={"source": rec.file, "record": record_id or "-"})

    # --- EEG registrations ------------------------------------------------
    eeg_valid = []
    for rec in eeg_raw:
        eeg_id = clean(rec.get("EegMaStRNummer"))
        if eeg_id is None:
            reject(rec, None, "MISSING_EEG_ID", "EegMaStRNummer is empty")
        elif not EEG_ID_RE.match(eeg_id):
            reject(rec, eeg_id, "INVALID_EEG_ID", f"unexpected format {eeg_id!r}")
        else:
            eeg_valid.append(rec)
    eeg_by_id, eeg_conflicts = _dedupe(eeg_valid, "EegMaStRNummer", rejects, logger)

    # --- Solar units ------------------------------------------------------
    units_valid = []
    for rec in units_raw:
        unit_id = clean(rec.get("EinheitMastrNummer"))
        if unit_id is None:
            reject(rec, None, "MISSING_UNIT_ID", "EinheitMastrNummer is empty")
        elif not UNIT_ID_RE.match(unit_id):
            reject(rec, unit_id, "INVALID_UNIT_ID", f"unexpected format {unit_id!r}")
        else:
            units_valid.append(rec)
    units_by_id, unit_conflicts = _dedupe(units_valid, "EinheitMastrNummer", rejects, logger)

    eeg_link_count = Counter(clean(u.get("EegMaStRNummer")) for u in units_by_id.values())

    targets: list[dict] = []
    out_of_scope = 0
    referenced_eeg: set[str] = set()

    for unit_id in sorted(units_by_id):
        rec = units_by_id[unit_id]
        flags = _Flags()

        carrier = clean(rec.get("Energietraeger"))
        if carrier is not None and carrier != catalog.ENERGY_CARRIER_SOLAR:
            reject(rec, unit_id, "NOT_SOLAR", f"Energietraeger {carrier}")
            continue

        state_ags = _region(rec, flags)
        if scope_ags is not None:
            if state_ags is None:
                reject(rec, unit_id, "REGION_UNKNOWN", "no usable Gemeindeschluessel or Bundesland")
                continue
            if state_ags != scope_ags:
                out_of_scope += 1
                continue

        if unit_id in unit_conflicts:
            flags.add("DUPLICATE_CONFLICT", "duplicate unit rows with equal timestamps")

        # Unit status and dates
        status_code = clean(rec.get("EinheitBetriebsstatus"))
        unit_status = catalog.UNIT_STATUS.get(status_code or "", "unknown")
        if unit_status == "in_planning":
            flags.add("UNIT_NOT_COMMISSIONED", "unit is still in planning")
        elif unit_status == "temporarily_decommissioned":
            flags.add("UNIT_TEMPORARILY_DECOMMISSIONED")
        elif unit_status == "permanently_decommissioned":
            flags.add("UNIT_PERMANENTLY_DECOMMISSIONED")
        elif unit_status == "unknown":
            flags.add("UNIT_STATUS_UNKNOWN", f"EinheitBetriebsstatus={status_code!r}")

        unit_date, unit_date_err = parse_date(rec.get("Inbetriebnahmedatum"))
        if unit_date_err and unit_date_err != "missing":
            flags.add("UNIT_DATE_INVALID", unit_date_err)

        verification = clean(rec.get("NetzbetreiberpruefungStatus"))
        if verification not in catalog.GRID_OPERATOR_VERIFIED_CODES:
            flags.add(
                "NOT_GRID_OPERATOR_VERIFIED",
                f"NetzbetreiberpruefungStatus={verification!r}",
            )

        unit_ts, ts_err = parse_timestamp(rec.get("DatumLetzteAktualisierung"))
        if ts_err and ts_err != "missing":
            flags.add("TIMESTAMP_INVALID", ts_err)

        # EEG registration
        eeg_id = clean(rec.get("EegMaStRNummer"))
        eeg_rec = None
        eeg_date = None
        if eeg_id is None:
            flags.add("NO_EEG_LINK", "unit has no EEG registration number")
        elif not EEG_ID_RE.match(eeg_id):
            flags.add("EEG_ID_INVALID", f"unexpected format {eeg_id!r}")
        elif eeg_id not in eeg_by_id:
            flags.add("EEG_RECORD_NOT_FOUND", f"{eeg_id} not in EEG export")
        else:
            eeg_rec = eeg_by_id[eeg_id]
            referenced_eeg.add(eeg_id)
            if eeg_id in eeg_conflicts:
                flags.add("DUPLICATE_CONFLICT", "duplicate EEG rows with equal timestamps")
            if eeg_link_count[eeg_id] > 1:
                flags.add("SHARED_EEG_ANLAGE", f"{eeg_link_count[eeg_id]} units share {eeg_id}")
            linked = split_ids(eeg_rec.get("VerknuepfteEinheitenMaStRNummern"))
            if linked and unit_id not in linked:
                flags.add("EEG_LINK_INCONSISTENT", f"{eeg_id} lists {', '.join(linked)}")

            eeg_date, eeg_date_err = parse_date(eeg_rec.get("EegInbetriebnahmedatum"))
            if eeg_date_err == "missing":
                flags.add("EEG_DATE_MISSING", "EegInbetriebnahmedatum is empty")
            elif eeg_date_err:
                flags.add("EEG_DATE_INVALID", eeg_date_err)
            elif eeg_date > cfg.reference_date:
                flags.add("EEG_DATE_IN_FUTURE", f"{eeg_date} is after {cfg.reference_date}")
                eeg_date = None

            if eeg_date and unit_date:
                gap = abs((eeg_date - unit_date).days)
                if gap > cfg.date_mismatch_tolerance_days:
                    flags.add(
                        "DATE_MISMATCH_UNIT_VS_EEG",
                        f"unit {unit_date}, EEG {eeg_date} ({gap} days apart)",
                    )

        # Spatial metadata
        coords, coord_err = parse_coordinates(rec.get("Breitengrad"), rec.get("Laengengrad"))
        if coord_err:
            flags.add("COORDINATES_INVALID", coord_err)
        bbox = catalog.STATE_BBOX.get(state_ags or "")
        if coords and bbox and not in_bbox(*coords, bbox):
            flags.add(
                "COORDINATES_OUTSIDE_REGION_BBOX",
                f"{coords} outside {catalog.STATES[state_ags][1]} bounding box",
            )

        postcode = clean(rec.get("Postleitzahl"))
        if postcode and not POSTCODE_RE.match(postcode):
            flags.add("POSTCODE_INVALID", f"{postcode!r}")
            postcode = None
        ags = clean(rec.get("Gemeindeschluessel"))
        ags = ags if ags and AGS_RE.match(ags) else None

        if coords:
            precision = "point"
        elif postcode:
            precision = "postcode"
        elif ags:
            precision = "municipality"
        else:
            precision = "none"

        gross_kw, gross_err = parse_kw(rec.get("Bruttoleistung"))
        net_kw, net_err = parse_kw(rec.get("Nettonennleistung"))
        if gross_err or net_err:
            flags.add("POWER_INVALID", "; ".join(e for e in (gross_err, net_err) if e))

        # Expiry derivation: only from a valid EEG commissioning date and
        # only when nothing blocking was found.
        end_date = expiry_year = None
        basis = None
        if eeg_date and not flags.has(BLOCKING):
            expiry = compute_expiry(eeg_date)
            for code in expiry.notes + expiry.needs_review:
                flags.add(code)
            end_date, expiry_year = expiry.remuneration_end_date, expiry.expiry_year
            basis = "EegInbetriebnahmedatum"

        if flags.has(BLOCKING):
            status = "incomplete"
        elif flags.has(REVIEW):
            status = "needs_review"
        else:
            status = "confirmed"

        bucket = expiry_bucket(end_date, cfg.reference_date, cfg.horizon_years)
        candidate = (
            status != "incomplete"
            and unit_status in ("operating", "temporarily_decommissioned")
            and bucket in ("expired", "within_horizon")
        )

        eeg_ts = None
        if eeg_rec is not None:
            eeg_ts, _ = parse_timestamp(eeg_rec.get("DatumLetzteAktualisierung"))

        state = catalog.STATES.get(state_ags or "")
        targets.append(
            {
                "mastr_unit_id": unit_id,
                "eeg_mastr_id": eeg_id,
                "eeg_plant_key": clean(eeg_rec.get("AnlagenschluesselEeg")) if eeg_rec else None,
                "unit_name": clean(rec.get("NameStromerzeugungseinheit")),
                "unit_status_code": status_code,
                "unit_status": unit_status,
                "state_ags": state_ags,
                "state_code": state[0] if state else None,
                "municipality_key": ags,
                "municipality": clean(rec.get("Gemeinde")),
                "postcode": postcode,
                "city": clean(rec.get("Ort")),
                "latitude": coords[0] if coords else None,
                "longitude": coords[1] if coords else None,
                "spatial_precision": precision,
                "gross_power_kw": gross_kw,
                "net_power_kw": net_kw,
                "unit_commissioning_date": unit_date,
                "eeg_commissioning_date": eeg_date,
                "remuneration_end_date": end_date,
                "expiry_year": expiry_year,
                "expiry_basis": basis,
                "rule_version": RULE_VERSION,
                "expiry_status": status,
                "expiry_bucket": bucket,
                "is_repowering_candidate": candidate,
                "flags": flags.codes(),
                "flag_details": dict(sorted(flags.items.items())),
                "source_last_updated_utc": unit_ts,
                "eeg_last_updated_utc": eeg_ts,
                "source_unit_file": f"{rec.file}#{rec.index}",
                "source_eeg_file": f"{eeg_rec.file}#{eeg_rec.index}" if eeg_rec else None,
                "unit_record_hash": record_hash(rec),
                "eeg_record_hash": record_hash(eeg_rec) if eeg_rec else None,
                "raw_unit": rec.fields,
                "raw_eeg": eeg_rec.fields if eeg_rec else None,
                "reference_date": cfg.reference_date,
                "horizon_years": cfg.horizon_years,
            }
        )

    stats = {
        "units_read": len(units_raw),
        "eeg_records_read": len(eeg_raw),
        "units_unique": len(units_by_id),
        "eeg_records_unique": len(eeg_by_id),
        "units_out_of_scope": out_of_scope,
        "units_staged": len(targets),
        "eeg_records_unreferenced_in_scope": len(set(eeg_by_id) - referenced_eeg),
        "rejected": len(rejects),
    }
    logger.info("Pipeline finished: %s", stats)

    return PipelineResult(cfg, unit_sources, eeg_sources, targets, rejects, stats)


def json_default(value):
    if isinstance(value, date | datetime):
        return value.isoformat()
    raise TypeError(type(value))

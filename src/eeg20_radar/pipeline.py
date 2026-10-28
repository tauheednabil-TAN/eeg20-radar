"""
Screening pipeline: read -> validate -> scope -> de-duplicate -> derive.

The pipeline makes three streaming passes so memory grows with the number
of in-scope units, not with the size of the export:

1. Unit files: validate IDs, drop other regions, and remember only a small
   index entry per unit (best duplicate position, its hash, its EEG link).
2. EEG files: keep only the records linked to an in-scope unit.
3. Unit files again: build each staged row and hand it to the caller.

The pipeline never touches the database, so equal inputs always give equal rows.
"""

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable
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
from eeg20_radar.rules import (
    RULE_VERSION,
    compute_expiry,
    expiry_bucket,
    needs_tender_award,
)
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
    "TENDER_AWARD_UNVERIFIED": REVIEW,
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


def row_json(row: dict) -> str:
    return json.dumps(row, sort_keys=True, default=str)


class RowDigest:
    """sha256 over the staged rows in output order."""

    def __init__(self) -> None:
        self._h = hashlib.sha256()

    def update(self, row: dict) -> None:
        self._h.update(row_json(row).encode())
        self._h.update(b"\n")

    def hexdigest(self) -> str:
        return self._h.hexdigest()


@dataclass
class Summary:
    unit_sources: list[SourceFile]
    eeg_sources: list[SourceFile]
    stats: dict
    digest: str


@dataclass
class PipelineResult:
    """All rows in memory. Meant for tests and small inputs; the CLI streams."""

    config: RunConfig
    unit_sources: list[SourceFile]
    eeg_sources: list[SourceFile]
    targets: list[dict]
    rejects: list[Reject]
    stats: dict = field(default_factory=dict)

    @property
    def digest(self) -> str:
        digest = RowDigest()
        for row in self.targets:
            digest.update(row)
        return digest.hexdigest()


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


@dataclass(slots=True)
class _Best:
    """The current winner among copies of one ID, plus what decides it."""

    ts_key: float  # negative timestamp, so smaller sorts first; inf if missing
    position: tuple[str, int]
    digest: str
    eeg_id: str | None = None
    copies: int = 1
    tie_differs: bool = False


def _ts_key(rec: SourceRecord) -> float:
    ts, _ = parse_timestamp(rec.get("DatumLetzteAktualisierung"))
    return -ts.timestamp() if ts else float("inf")


def _offer(best: dict[str, _Best], record_id: str, rec: SourceRecord, **extra) -> None:
    """
    Duplicate rule: the newest DatumLetzteAktualisierung wins; on a tie the
    earliest position in the files wins. Records arrive in file order, so a
    later record only replaces the current best when it is strictly newer.
    """
    key, digest = _ts_key(rec), record_hash(rec)
    current = best.get(record_id)
    if current is None:
        best[record_id] = _Best(key, (rec.file, rec.index), digest, **extra)
        return
    current.copies += 1
    if key < current.ts_key:
        best[record_id] = _Best(key, (rec.file, rec.index), digest, copies=current.copies, **extra)
    elif key == current.ts_key and digest != current.digest:
        current.tie_differs = True


def _duplicate_reject(rec: SourceRecord, record_id: str, winner: _Best) -> Reject:
    if record_hash(rec) == winner.digest:
        code, detail = "DUPLICATE_EXACT", "identical copy"
    else:
        code = "DUPLICATE_SUPERSEDED"
        detail = f"superseded by {winner.position[0]}#{winner.position[1]}"
        if _ts_key(rec) == winner.ts_key:
            detail += " (same timestamp, contents differ)"
    return Reject(rec.file, rec.index, record_id, code, detail, rec.fields)


def _unpack(packed: tuple[str, int, str]) -> SourceRecord:
    file, index, fields = packed
    return SourceRecord(file, index, json.loads(fields))


def _region(rec: SourceRecord) -> tuple[str | None, str | None]:
    """Returns (2-digit AGS state prefix, conflict detail or None)."""
    ags = clean(rec.get("Gemeindeschluessel"))
    ags_state = ags[:2] if ags and AGS_RE.match(ags) and ags[:2] in catalog.STATES else None
    bl_state = catalog.BUNDESLAND_CODE_TO_AGS.get(clean(rec.get("Bundesland")) or "")
    conflict = None
    if ags_state and bl_state and ags_state != bl_state:
        conflict = f"Gemeindeschluessel {ags} vs Bundesland code {rec.get('Bundesland')}"
    return ags_state or bl_state, conflict


def _award_number(eeg_rec: SourceRecord) -> str | None:
    for name in catalog.TENDER_AWARD_FIELDS:
        value = clean(eeg_rec.get(name))
        if value:
            return value
    return None


def screen(
    cfg: RunConfig,
    logger: logging.Logger,
    on_target: Callable[[dict], None],
    on_reject: Callable[[Reject], None],
    sources: tuple[list[SourceFile], list[SourceFile]] | None = None,
) -> Summary:
    scope = normalize_scope(cfg.scope)
    scope_ags = None if scope == "DE" else catalog.STATE_ABBR_TO_AGS[scope]
    unit_sources, eeg_sources = sources or discover(cfg.source_dir)
    stats = Counter()
    digest = RowDigest()

    def reject(r: Reject) -> None:
        stats["rejected"] += 1
        logger.warning(
            "%s: %s", r.reason_code, r.detail, extra={"source": r.source_file, "record": r.record_id or "-"}
        )
        on_reject(r)

    def classify_unit(rec: SourceRecord) -> tuple[str, str | None, str | None]:
        """Returns (verdict, unit_id, detail); verdict is ok, out_of_scope or a reject code."""
        unit_id = clean(rec.get("EinheitMastrNummer"))
        if unit_id is None:
            return "MISSING_UNIT_ID", None, "EinheitMastrNummer is empty"
        if not UNIT_ID_RE.match(unit_id):
            return "INVALID_UNIT_ID", unit_id, f"unexpected format {unit_id!r}"
        carrier = clean(rec.get("Energietraeger"))
        if carrier is not None and carrier != catalog.ENERGY_CARRIER_SOLAR:
            return "NOT_SOLAR", unit_id, f"Energietraeger {carrier}"
        if scope_ags is not None:
            state_ags, _ = _region(rec)
            if state_ags is None:
                return "REGION_UNKNOWN", unit_id, "no usable Gemeindeschluessel or Bundesland"
            if state_ags != scope_ags:
                return "out_of_scope", unit_id, None
        return "ok", unit_id, None

    def unit_records():
        for source in unit_sources:
            yield from iter_records(source, UNIT_TAG, logger)

    # --- Pass 1: unit index --------------------------------------------------
    unit_best: dict[str, _Best] = {}
    for rec in unit_records():
        stats["units_read"] += 1
        verdict, unit_id, detail = classify_unit(rec)
        if verdict == "out_of_scope":
            stats["units_out_of_scope"] += 1
        elif verdict != "ok":
            reject(Reject(rec.file, rec.index, unit_id, verdict, detail, rec.fields))
        else:
            _offer(unit_best, unit_id, rec, eeg_id=clean(rec.get("EegMaStRNummer")))

    eeg_link_count = Counter(b.eeg_id for b in unit_best.values() if b.eeg_id)

    # --- Pass 2: linked EEG records -----------------------------------------
    # Linked EEG records are kept as JSON strings, which take less memory
    # than parsed dicts, and rebuilt when a row needs them.
    eeg_best: dict[str, _Best] = {}
    eeg_copies: dict[str, list[tuple[str, int, str]]] = {}
    for source in eeg_sources:
        for rec in iter_records(source, EEG_TAG, logger):
            stats["eeg_records_read"] += 1
            eeg_id = clean(rec.get("EegMaStRNummer"))
            if eeg_id is None:
                reject(
                    Reject(rec.file, rec.index, None, "MISSING_EEG_ID", "EegMaStRNummer is empty", rec.fields)
                )
            elif not EEG_ID_RE.match(eeg_id):
                reject(
                    Reject(
                        rec.file,
                        rec.index,
                        eeg_id,
                        "INVALID_EEG_ID",
                        f"unexpected format {eeg_id!r}",
                        rec.fields,
                    )
                )
            elif eeg_id not in eeg_link_count:
                stats["eeg_records_not_linked"] += 1
            else:
                eeg_copies.setdefault(eeg_id, []).append(
                    (rec.file, rec.index, json.dumps(rec.fields, ensure_ascii=False))
                )
                _offer(eeg_best, eeg_id, rec)

    eeg_by_id: dict[str, tuple[str, int, str]] = {}
    for eeg_id, copies in eeg_copies.items():
        winner = eeg_best[eeg_id]
        for copy in copies:
            if copy[:2] == winner.position:
                eeg_by_id[eeg_id] = copy
            else:
                reject(_duplicate_reject(_unpack(copy), eeg_id, winner))
    del eeg_copies

    # --- Pass 3: staged rows -------------------------------------------------
    for rec in unit_records():
        verdict, unit_id, _ = classify_unit(rec)
        if verdict != "ok":
            continue  # already counted or rejected in pass 1
        winner = unit_best[unit_id]
        if (rec.file, rec.index) != winner.position:
            reject(_duplicate_reject(rec, unit_id, winner))
            continue

        eeg_id = winner.eeg_id
        packed = eeg_by_id.get(eeg_id) if eeg_id else None
        eeg_rec = _unpack(packed) if packed else None
        eeg_winner = eeg_best.get(eeg_id) if eeg_id else None
        row = _build_row(
            cfg,
            rec,
            unit_id,
            winner,
            eeg_id,
            eeg_rec,
            eeg_winner,
            eeg_link_count[eeg_id] if eeg_id else 0,
        )
        stats["units_staged"] += 1
        digest.update(row)
        on_target(row)

    stats["units_unique_in_scope"] = len(unit_best)
    stats["eeg_records_linked"] = len(eeg_by_id)
    result_stats = {
        key: stats[key]
        for key in (
            "units_read",
            "eeg_records_read",
            "units_out_of_scope",
            "units_unique_in_scope",
            "units_staged",
            "eeg_records_linked",
            "eeg_records_not_linked",
            "rejected",
        )
    }
    logger.info("Pipeline finished: %s", result_stats)
    return Summary(unit_sources, eeg_sources, result_stats, digest.hexdigest())


def _build_row(
    cfg: RunConfig,
    rec: SourceRecord,
    unit_id: str,
    winner: _Best,
    eeg_id: str | None,
    eeg_rec: SourceRecord | None,
    eeg_winner: _Best | None,
    shared_count: int,
) -> dict:
    flags = _Flags()

    state_ags, region_conflict = _region(rec)
    if region_conflict:
        flags.add("REGION_CONFLICT", region_conflict)
    if winner.tie_differs:
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
        flags.add("NOT_GRID_OPERATOR_VERIFIED", f"NetzbetreiberpruefungStatus={verification!r}")

    unit_ts, ts_err = parse_timestamp(rec.get("DatumLetzteAktualisierung"))
    if ts_err and ts_err != "missing":
        flags.add("TIMESTAMP_INVALID", ts_err)

    gross_kw, gross_err = parse_kw(rec.get("Bruttoleistung"))
    net_kw, net_err = parse_kw(rec.get("Nettonennleistung"))
    if gross_err or net_err:
        flags.add("POWER_INVALID", "; ".join(e for e in (gross_err, net_err) if e))

    # EEG registration
    eeg_date = None
    installed_kw = None
    if eeg_id is None:
        flags.add("NO_EEG_LINK", "unit has no EEG registration number")
    elif not EEG_ID_RE.match(eeg_id):
        flags.add("EEG_ID_INVALID", f"unexpected format {eeg_id!r}")
    elif eeg_rec is None:
        flags.add("EEG_RECORD_NOT_FOUND", f"{eeg_id} not in EEG export")
    else:
        if eeg_winner is not None and eeg_winner.tie_differs:
            flags.add("DUPLICATE_CONFLICT", "duplicate EEG rows with equal timestamps")
        if shared_count > 1:
            flags.add("SHARED_EEG_ANLAGE", f"{shared_count} units share {eeg_id}")
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

        installed_kw, _ = parse_kw(eeg_rec.get("InstallierteLeistung"))
        tender_kw = installed_kw if installed_kw is not None else gross_kw
        if eeg_date and needs_tender_award(eeg_date, tender_kw) and not _award_number(eeg_rec):
            flags.add(
                "TENDER_AWARD_UNVERIFIED",
                f"{tender_kw} kW commissioned {eeg_date} with no Zuschlagsnummer",
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

    # Expiry derivation: only from a valid EEG commissioning date and only
    # when nothing blocking was found.
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
    return {
        "mastr_unit_id": unit_id,
        "eeg_mastr_id": eeg_id,
        "eeg_plant_key": clean(eeg_rec.get("AnlagenschluesselEeg")) if eeg_rec else None,
        "tender_award_number": _award_number(eeg_rec) if eeg_rec else None,
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
        "eeg_installed_power_kw": installed_kw,
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


def run_pipeline(cfg: RunConfig, logger: logging.Logger) -> PipelineResult:
    """Runs screen() and keeps every row and reject in memory."""
    targets: list[dict] = []
    rejects: list[Reject] = []
    summary = screen(cfg, logger, targets.append, rejects.append)
    return PipelineResult(cfg, summary.unit_sources, summary.eeg_sources, targets, rejects, summary.stats)


def json_default(value):
    if isinstance(value, date | datetime):
        return value.isoformat()
    raise TypeError(type(value))

"""Field-level parsers. Each returns (value, error) and never raises."""

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from eeg20_radar.catalog import GERMANY_BBOX

BERLIN = ZoneInfo("Europe/Berlin")

UNIT_ID_RE = re.compile(r"^SEE\d{12}$")
EEG_ID_RE = re.compile(r"^EEG\d{12}$")
AGS_RE = re.compile(r"^\d{8}$")
POSTCODE_RE = re.compile(r"^\d{5}$")

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:T00:00:00(?:\.0+)?)?$")
_DE_DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$")


def clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def parse_date(value: str | None) -> tuple[date | None, str | None]:
    """Accepts ISO (YYYY-MM-DD) and German (DD.MM.YYYY) dates."""
    value = clean(value)
    if value is None:
        return None, "missing"

    if m := _ISO_DATE_RE.match(value):
        y, mo, d = (int(g) for g in m.groups())
    elif m := _DE_DATE_RE.match(value):
        d, mo, y = (int(g) for g in m.groups())
    else:
        return None, f"unparseable date {value!r}"

    try:
        return date(y, mo, d), None
    except ValueError:
        return None, f"invalid calendar date {value!r}"


def parse_timestamp(value: str | None) -> tuple[datetime | None, str | None]:
    """
    Parses MaStR timestamps (up to 7 fractional digits). Values without an
    offset are taken as Europe/Berlin local time. Returned in UTC.
    """
    value = clean(value)
    if value is None:
        return None, "missing"

    m = _TS_RE.match(value)
    if not m:
        return None, f"unparseable timestamp {value!r}"

    day, clock, frac, offset = m.groups()
    frac = (frac or "0")[:6].ljust(6, "0")
    text = f"{day}T{clock}.{frac}"
    if offset:
        text += "+00:00" if offset == "Z" else offset

    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None, f"invalid timestamp {value!r}"

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=BERLIN)
    return ts.astimezone(UTC), None


def parse_kw(value: str | None) -> tuple[float | None, str | None]:
    """Power in kW. Accepts decimal point or decimal comma."""
    value = clean(value)
    if value is None:
        return None, None
    try:
        number = float(value.replace(",", "."))
    except ValueError:
        return None, f"non-numeric power {value!r}"
    if number < 0 or number != number:
        return None, f"invalid power {value!r}"
    return number, None


def parse_coordinates(
    lat_raw: str | None,
    lon_raw: str | None,
) -> tuple[tuple[float, float] | None, str | None]:
    lat_raw, lon_raw = clean(lat_raw), clean(lon_raw)
    if lat_raw is None and lon_raw is None:
        return None, None
    if lat_raw is None or lon_raw is None:
        return None, "only one coordinate present"
    try:
        lat = float(lat_raw.replace(",", "."))
        lon = float(lon_raw.replace(",", "."))
    except ValueError:
        return None, f"non-numeric coordinates ({lat_raw!r}, {lon_raw!r})"
    if not in_bbox(lat, lon, GERMANY_BBOX):
        return None, f"coordinates outside Germany ({lat}, {lon})"
    return (lat, lon), None


def in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    min_lat, max_lat, min_lon, max_lon = bbox
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def split_ids(value: str | None) -> list[str]:
    value = clean(value)
    if value is None:
        return []
    return [part.strip() for part in re.split(r"[,;\s]+", value) if part.strip()]

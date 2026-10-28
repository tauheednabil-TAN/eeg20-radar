"""
Reader for the MaStR Gesamtdatenexport XML files.

The public export ships solar units as EinheitenSolar*.xml and their EEG
registrations as AnlagenEegSolar*.xml (UTF-16 with BOM in the real export).
Each record is a flat element whose children are field name -> text.
"""

import hashlib
import logging
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

UNIT_FILE_GLOB = "EinheitenSolar*.xml"
EEG_FILE_GLOB = "AnlagenEegSolar*.xml"
UNIT_TAG = "EinheitSolar"
EEG_TAG = "AnlageEegSolar"


class SourceError(RuntimeError):
    """Raised when a source file is missing or cannot be parsed at all."""


@dataclass(frozen=True)
class SourceFile:
    kind: str
    path: Path
    sha256: str
    size_bytes: int

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class SourceRecord:
    file: str
    index: int  # 1-based position of the record inside its file
    fields: dict[str, str | None]
    _lower: dict[str, str] | None = field(default=None, compare=False, repr=False)

    def get(self, key: str) -> str | None:
        """
        Field lookup that ignores case on a miss. MaStR spells the same
        abbreviation both ways (EinheitMastrNummer, EegMaStRNummer), and the
        spelling has changed between export releases.
        """
        if key in self.fields:
            return self.fields[key]
        if self._lower is None:
            object.__setattr__(self, "_lower", {name.lower(): name for name in self.fields})
        name = self._lower.get(key.lower())
        return self.fields[name] if name is not None else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover(source_dir: Path) -> tuple[list[SourceFile], list[SourceFile]]:
    if not source_dir.is_dir():
        raise SourceError(f"Source directory not found: {source_dir}")

    def collect(kind: str, pattern: str) -> list[SourceFile]:
        return [SourceFile(kind, p, _sha256(p), p.stat().st_size) for p in sorted(source_dir.glob(pattern))]

    units = collect("unit", UNIT_FILE_GLOB)
    eeg = collect("eeg", EEG_FILE_GLOB)
    if not units:
        raise SourceError(f"No {UNIT_FILE_GLOB} files in {source_dir}")
    if not eeg:
        raise SourceError(f"No {EEG_FILE_GLOB} files in {source_dir}")
    return units, eeg


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def iter_records(
    source: SourceFile,
    record_tag: str,
    logger: logging.Logger,
) -> Iterator[SourceRecord]:
    """
    Yields one record at a time. After each record the root element is
    cleared, so finished records do not pile up in the parse tree and memory
    stays flat however large the file is. A file that is not well-formed XML
    aborts the run (SourceError): a partial file must not be staged as if it
    were complete.
    """
    index = 0
    root = None
    try:
        for event, elem in ET.iterparse(source.path, events=("start", "end")):
            if root is None:
                root = elem
                continue
            if event != "end" or _local(elem.tag) != record_tag:
                continue
            index += 1
            fields = {_local(child.tag): (child.text.strip() if child.text else None) for child in elem}
            yield SourceRecord(source.name, index, fields)
            root.clear()
    except ET.ParseError as exc:
        logger.error(
            "Malformed XML after %d records: %s",
            index,
            exc,
            extra={"source": source.name},
        )
        raise SourceError(f"{source.name}: malformed XML ({exc})") from exc

    logger.info(
        "Read %d %s records",
        index,
        record_tag,
        extra={"source": source.name},
    )

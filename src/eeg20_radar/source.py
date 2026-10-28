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
from dataclasses import dataclass
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

    def get(self, key: str) -> str | None:
        return self.fields.get(key)


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
    Streams records so multi-GB export files never sit in memory. A file
    that is not well-formed XML aborts the run (SourceError): a partial
    file must not be staged as if it were complete.
    """
    index = 0
    try:
        for _, elem in ET.iterparse(source.path, events=("end",)):
            if _local(elem.tag) != record_tag:
                continue
            index += 1
            fields = {_local(child.tag): (child.text.strip() if child.text else None) for child in elem}
            yield SourceRecord(source.name, index, fields)
            elem.clear()
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

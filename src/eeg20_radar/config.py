"""Settings from environment variables (see .env.example)."""

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    target_schema: str
    scope: str
    reference_date: date | None
    horizon_years: int
    source_dir: Path
    output_dir: Path
    log_level: str


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    ref = env.get("EEG20_REFERENCE_DATE", "").strip()
    return Settings(
        database_url=env.get("DATABASE_URL") or None,
        target_schema=env.get("EEG20_TARGET_SCHEMA", "iris_staging"),
        scope=env.get("EEG20_SCOPE", "NRW"),
        reference_date=date.fromisoformat(ref) if ref else None,
        horizon_years=int(env.get("EEG20_HORIZON_YEARS", "5")),
        source_dir=Path(env.get("EEG20_SOURCE_DIR", "fixtures/mastr")),
        output_dir=Path(env.get("EEG20_OUTPUT_DIR", "output")),
        log_level=env.get("EEG20_LOG_LEVEL", "INFO"),
    )

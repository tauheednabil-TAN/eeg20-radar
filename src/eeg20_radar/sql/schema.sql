-- Idempotent DDL. Every object lives in iris_staging; nothing here touches
-- iris_core. The schema itself is created by the database administrator
-- (docker/initdb) and owned by the staging writer role.

CREATE TABLE IF NOT EXISTS iris_staging.eeg_ingestion_runs (
    run_id              text PRIMARY KEY,
    started_at          timestamptz NOT NULL,
    finished_at         timestamptz,
    status              text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    scope               text NOT NULL,
    reference_date      date NOT NULL,
    horizon_years       integer NOT NULL,
    rule_version        text NOT NULL,
    source_files        jsonb NOT NULL,
    stats               jsonb,
    result_digest       text,
    error               text
);

CREATE TABLE IF NOT EXISTS iris_staging.eeg_repowering_targets (
    mastr_unit_id            text PRIMARY KEY,
    eeg_mastr_id             text,
    eeg_plant_key            text,
    tender_award_number      text,
    unit_name                text,
    unit_status_code         text,
    unit_status              text NOT NULL,
    state_ags                text,
    state_code               text,
    municipality_key         text,
    municipality             text,
    postcode                 text,
    city                     text,
    latitude                 double precision,
    longitude                double precision,
    geom                     geometry(Point, 4326),
    spatial_precision        text NOT NULL,
    gross_power_kw           double precision,
    net_power_kw             double precision,
    eeg_installed_power_kw   double precision,
    unit_commissioning_date  date,
    eeg_commissioning_date   date,
    remuneration_end_date    date,
    expiry_year              integer,
    expiry_basis             text,
    rule_version             text NOT NULL,
    expiry_status            text NOT NULL
        CHECK (expiry_status IN ('confirmed', 'needs_review', 'incomplete')),
    expiry_bucket            text NOT NULL,
    is_repowering_candidate  boolean NOT NULL,
    flags                    text[] NOT NULL,
    flag_details             jsonb NOT NULL,
    source_last_updated_utc  timestamptz,
    eeg_last_updated_utc     timestamptz,
    source_unit_file         text NOT NULL,
    source_eeg_file          text,
    unit_record_hash         text NOT NULL,
    eeg_record_hash          text,
    raw_unit                 jsonb NOT NULL,
    raw_eeg                  jsonb,
    reference_date           date NOT NULL,
    horizon_years            integer NOT NULL,
    first_seen_run_id        text NOT NULL REFERENCES iris_staging.eeg_ingestion_runs (run_id),
    last_seen_run_id         text NOT NULL REFERENCES iris_staging.eeg_ingestion_runs (run_id),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    -- An expiry date may only exist when it was derived from the EEG date.
    CONSTRAINT eeg_targets_no_invented_expiry CHECK (
        (expiry_status = 'incomplete' AND expiry_year IS NULL AND remuneration_end_date IS NULL)
        OR (expiry_status <> 'incomplete' AND expiry_year IS NOT NULL
            AND eeg_commissioning_date IS NOT NULL)
    )
);

-- Columns added after the first release; no-ops on a fresh database.
ALTER TABLE iris_staging.eeg_repowering_targets
    ADD COLUMN IF NOT EXISTS tender_award_number text,
    ADD COLUMN IF NOT EXISTS eeg_installed_power_kw double precision;

CREATE INDEX IF NOT EXISTS eeg_targets_geom_gix
    ON iris_staging.eeg_repowering_targets USING gist (geom);
CREATE INDEX IF NOT EXISTS eeg_targets_expiry_year_idx
    ON iris_staging.eeg_repowering_targets (expiry_year);
CREATE INDEX IF NOT EXISTS eeg_targets_municipality_idx
    ON iris_staging.eeg_repowering_targets (municipality_key);

CREATE TABLE IF NOT EXISTS iris_staging.eeg_repowering_rejects (
    id             bigserial PRIMARY KEY,
    run_id         text NOT NULL REFERENCES iris_staging.eeg_ingestion_runs (run_id),
    source_file    text NOT NULL,
    record_index   integer NOT NULL,
    record_id      text,
    reason_code    text NOT NULL,
    detail         text NOT NULL,
    raw            jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS eeg_rejects_run_idx
    ON iris_staging.eeg_repowering_rejects (run_id);

-- One row per unit per run, holding the assessment at that run's reference
-- date. eeg_repowering_targets holds only the latest one; this table keeps
-- last year's result after an annual refresh.
CREATE TABLE IF NOT EXISTS iris_staging.eeg_repowering_target_history (
    run_id                   text NOT NULL REFERENCES iris_staging.eeg_ingestion_runs (run_id),
    mastr_unit_id            text NOT NULL,
    reference_date           date NOT NULL,
    horizon_years            integer NOT NULL,
    rule_version             text NOT NULL,
    eeg_commissioning_date   date,
    remuneration_end_date    date,
    expiry_year              integer,
    expiry_status            text NOT NULL,
    expiry_bucket            text NOT NULL,
    is_repowering_candidate  boolean NOT NULL,
    flags                    text[] NOT NULL,
    unit_record_hash         text NOT NULL,
    eeg_record_hash          text,
    PRIMARY KEY (run_id, mastr_unit_id)
);

CREATE INDEX IF NOT EXISTS eeg_history_unit_idx
    ON iris_staging.eeg_repowering_target_history (mastr_unit_id, reference_date);

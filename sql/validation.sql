-- Validation queries for iris_staging.eeg_repowering_targets.
-- Run as the staging writer, e.g.:
--   docker compose exec db psql -U postgres -d iris_staging_db -f /dev/stdin < sql/validation.sql

-- 1. Acceptance: no expiry date without an EEG commissioning date, and none on
--    incomplete rows. Both counts must be 0.
SELECT
    count(*) FILTER (WHERE expiry_status = 'incomplete' AND expiry_year IS NOT NULL) AS incomplete_with_expiry,
    count(*) FILTER (WHERE expiry_year IS NOT NULL AND eeg_commissioning_date IS NULL) AS expiry_without_eeg_date
FROM iris_staging.eeg_repowering_targets;

-- 2. Rule check: every derived end date is 31 Dec of
--    max(EEG commissioning year, 2000) + 20. Must return 0.
SELECT count(*) AS rule_violations
FROM iris_staging.eeg_repowering_targets
WHERE expiry_year IS NOT NULL
  AND remuneration_end_date
      <> make_date(greatest(extract(year FROM eeg_commissioning_date)::int, 2000) + 20, 12, 31);

-- 3. Coverage by status.
SELECT expiry_status, count(*) AS units, round(sum(net_power_kw)::numeric, 1) AS net_kw
FROM iris_staging.eeg_repowering_targets
GROUP BY expiry_status
ORDER BY expiry_status;

-- 4. Repowering candidates by expiry year and municipality.
SELECT expiry_year, municipality_key, municipality,
       count(*) AS units, round(sum(net_power_kw)::numeric, 1) AS net_kw
FROM iris_staging.eeg_repowering_targets
WHERE is_repowering_candidate
GROUP BY expiry_year, municipality_key, municipality
ORDER BY expiry_year, municipality_key;

-- 5. Spatial screening example: candidates within 10 km of Cologne Cathedral.
SELECT mastr_unit_id, expiry_year, expiry_status,
       round(ST_Distance(geom::geography, ST_SetSRID(ST_MakePoint(6.9583, 50.9413), 4326)::geography)) AS distance_m
FROM iris_staging.eeg_repowering_targets
WHERE is_repowering_candidate
  AND geom IS NOT NULL
  AND ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(6.9583, 50.9413), 4326)::geography, 10000)
ORDER BY distance_m;

-- 6. Rows that were not present in the latest successful run (kept, not deleted).
SELECT t.mastr_unit_id, t.last_seen_run_id
FROM iris_staging.eeg_repowering_targets t
WHERE t.last_seen_run_id <> (
    SELECT run_id FROM iris_staging.eeg_ingestion_runs
    WHERE status = 'succeeded' ORDER BY started_at DESC LIMIT 1
);

-- 7. Rejected records of the latest run.
SELECT reason_code, count(*)
FROM iris_staging.eeg_repowering_rejects
WHERE run_id = (
    SELECT run_id FROM iris_staging.eeg_ingestion_runs
    WHERE status = 'succeeded' ORDER BY started_at DESC LIMIT 1
)
GROUP BY reason_code
ORDER BY reason_code;

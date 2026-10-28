#!/bin/bash
# Runs once, as the database administrator, when the staging database is
# first initialised. Creates the restricted writer role and the
# iris_staging schema it owns. No iris_core objects are created or granted.
set -euo pipefail

: "${EEG20_DB_USER:=eeg20_writer}"
: "${EEG20_DB_PASSWORD:?EEG20_DB_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v writer="$EEG20_DB_USER" \
     -v pw="$EEG20_DB_PASSWORD" <<'SQL'
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE ROLE :"writer" LOGIN PASSWORD :'pw' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE SCHEMA IF NOT EXISTS iris_staging AUTHORIZATION :"writer";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"writer";
SQL

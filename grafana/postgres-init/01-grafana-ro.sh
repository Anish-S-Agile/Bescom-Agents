#!/bin/sh
# Creates the read-only Grafana role. Runs once, when the postgres data volume is first created.
# The app itself grants this role SELECT on the metrics_* tables (and only those) when it starts.
set -e
: "${GRAFANA_RO_PASSWORD:?GRAFANA_RO_PASSWORD must be set}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro') THEN
    CREATE ROLE grafana_ro LOGIN PASSWORD '${GRAFANA_RO_PASSWORD}';
  END IF;
END \$\$;
GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
EOSQL

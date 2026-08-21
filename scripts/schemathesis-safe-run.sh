#!/usr/bin/env bash
# Wraps a Schemathesis run with an automatic Postgres snapshot/restore,
# so fuzzing can never leave permanent state behind in the real dev
# database -- closes the exact gap found during Step 5.3.
#
# CORRECTED from the first version: pg_dump WITHOUT --data-only emits full
# schema DDL (CREATE TABLE, ALTER TABLE constraints/indexes) alongside the
# data. Restoring that against a database where the schema already exists
# produces a wall of "relation already exists" errors -- and, critically,
# masked the fact that TRUNCATE + full-dump-restore is fundamentally the
# wrong pairing. --data-only fixes this: the snapshot is COPY statements
# only, safe to replay after a plain TRUNCATE.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a
source .env
set +a

SNAPSHOT_FILE="/tmp/memory_governor_pre_schemathesis_$(date +%s).sql"

echo "Snapshotting memory_governor (data only) to $SNAPSHOT_FILE ..."
PGPASSWORD="${MEMORY_GOVERNOR_DB_PASSWORD}" pg_dump --data-only \
  -U memory_governor_svc -h localhost -d memory_governor > "$SNAPSHOT_FILE"

echo "Snapshot complete. Running Schemathesis..."
set +e
schemathesis run http://127.0.0.1:8321/openapi.json \
  -H "X-Governor-Credential: ${MEMORY_GOVERNOR_WRITER_MANUAL_SUBMISSION}" \
  "$@"
SCHEMATHESIS_EXIT=$?
set -e

echo "Restoring database from snapshot (removing any state Schemathesis created)..."
PGPASSWORD="${MEMORY_GOVERNOR_DB_PASSWORD}" psql -U memory_governor_svc -h localhost -d memory_governor -c "
TRUNCATE audit_log, memory_events, memory_candidates, projects, policy_versions RESTART IDENTITY CASCADE;
"
PGPASSWORD="${MEMORY_GOVERNOR_DB_PASSWORD}" psql -U memory_governor_svc -h localhost -d memory_governor < "$SNAPSHOT_FILE"

echo "Restore complete. Removing snapshot file."
rm -f "$SNAPSHOT_FILE"

echo "Schemathesis exit code was: $SCHEMATHESIS_EXIT"
exit $SCHEMATHESIS_EXIT

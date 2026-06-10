#!/usr/bin/env bash
# Apply (or roll back) every domain's migrations in DAG order.
#
# The topological order comes from `migrate-lint.py --print-order`, so the
# linter and this runner can never disagree about ordering.
#
# Env:
#   DB_USER (required), DB_PASS, DB_HOST (default 127.0.0.1),
#   DB_PORT (default 3306), DB_NAME (default app)
#   DIRECTION=up|down (default up; down runs `down -all` in reverse order)
set -euo pipefail

DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-3306}"
DB_USER="${DB_USER:?DB_USER is required}"
DB_PASS="${DB_PASS:-}"
DB_NAME="${DB_NAME:-app}"
DIRECTION="${DIRECTION:-up}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

order="$(python3 scripts/migrate-lint.py --print-order)"

if [[ "$DIRECTION" == "down" ]]; then
    order="$(printf '%s\n' "$order" | tac)"
elif [[ "$DIRECTION" != "up" ]]; then
    echo "DIRECTION must be 'up' or 'down', got: $DIRECTION" >&2
    exit 1
fi

while IFS=$'\t' read -r domain path tracking_table; do
    [[ -z "$domain" ]] && continue
    dsn="mysql://${DB_USER}:${DB_PASS}@tcp(${DB_HOST}:${DB_PORT})/${DB_NAME}?x-migrations-table=${tracking_table}&multiStatements=true"
    echo "==> ${domain} (${path}, tracking table ${tracking_table}): migrate ${DIRECTION}"
    if [[ "$DIRECTION" == "down" ]]; then
        migrate -path "$path" -database "$dsn" down -all
    else
        migrate -path "$path" -database "$dsn" up
    fi
done <<<"$order"

echo "All domains migrated ${DIRECTION}."

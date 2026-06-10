# migration-dag-demo

DAG-enforced, per-domain database migrations for
[golang-migrate](https://github.com/golang-migrate/migrate) on MySQL 8, with
enforcement in GitHub Actions.

## The problem

golang-migrate assumes one global, linear version sequence per database. In a
codebase with many teams, that single sequence becomes a point of contention:
everyone races for the next version number, unrelated changes serialize behind
each other, and nothing stops the `ledger` team from quietly `ALTER`-ing a
table the `identity` team owns.

## The design

This repo splits migrations into **per-domain sequences**:

- Each domain has its own directory (`migrations/<domain>/`) with its own
  independent version sequence (`000001_...`, `000002_...`).
- Each domain has its own **tracking table** (`schema_migrations_<domain>`),
  selected at run time with golang-migrate's `x-migrations-table` DSN
  parameter — no forks or patches, stock `migrate` CLI.
- `migrations/dag.yaml` is the manifest. Each domain declares:
  - `path` — where its migrations live
  - `tracking_table` — its private version table
  - `depends_on` — which domains must be migrated before it
  - `owns_tables` — the ownership registry for lint enforcement

The `depends_on` edges must form a **DAG**:

```
identity ──> accounts ──> payments ──> ledger
    │            │            ▲           ▲
    │            └────────────┘           │
    └─────────────────────────┘   accounts┘
```

(`accounts` depends on `identity`; `payments` on `identity` + `accounts`;
`ledger` on `accounts` + `payments`.)

### Ownership rules (enforced by `scripts/migrate-lint.py`)

1. Every table is owned by exactly one domain (`owns_tables`).
2. A domain's migrations may run **DDL** (`CREATE`/`ALTER`/`DROP`/`RENAME`/
   `TRUNCATE`) only on tables it owns.
3. A domain's migrations may **reference** (FK `REFERENCES`, `JOIN`, `FROM`,
   `INTO`, `UPDATE`) tables owned by itself or by a **transitive ancestor**
   in the DAG. `accounts.user_id REFERENCES users(id)` is legal because
   `accounts` depends on `identity`. The reverse — identity touching
   `payments` — is a **back-edge** and fails the lint.
4. Escape hatch for genuinely cross-cutting migrations: a pragma comment
   inside the file whitelists specific tables for that file:

   ```sql
   -- lint:allow tables=payments,payouts reason="one-time backfill, approved in INFRA-123"
   ```

   Pragmas are grep-able and should be gated by CODEOWNERS review (see below).

### What the lint checks

`scripts/migrate-lint.py` (deps: `pyyaml`, `sqlglot`) exits non-zero listing
**all** violations:

1. `dag.yaml` parses; every `path` exists; no table is owned by two domains;
   `depends_on` forms a DAG (Kahn's algorithm, deterministic order).
2. Filenames match `NNNNNN_title.(up|down).sql`; every `.up` has a matching
   `.down`; versions per domain are contiguous `1..N` with no duplicates.
3. **Append-only vs merge base**: any version that is new relative to
   `--base origin/main` must be greater than the base's max version — no
   renumbering history. Skipped gracefully if the base ref is missing.
4. **Table ownership**, by parsing each `.up.sql` with sqlglot
   (`read="mysql"`), falling back to regex over
   `FROM`/`JOIN`/`INTO`/`UPDATE`/`REFERENCES`/`TABLE` when sqlglot can't
   parse a statement.
5. `--print-order` prints `domain<TAB>path<TAB>tracking_table` in topological
   order — `scripts/migrate-up.sh` consumes this, so the lint and the runner
   can never disagree on ordering.

### The runner

`scripts/migrate-up.sh` reads the topo order from the linter and runs, per
domain:

```
migrate -path migrations/<domain> \
  -database "mysql://$DB_USER:$DB_PASS@tcp($DB_HOST:$DB_PORT)/$DB_NAME?x-migrations-table=<tracking_table>&multiStatements=true" up
```

`DIRECTION=down` rolls everything back with `down -all` in **reverse**
topological order.

### CI (`.github/workflows/migrations.yml`)

Two jobs on `pull_request` **and** `merge_group`:

1. **lint** — full-history checkout, Python 3.12, runs the linter with
   `--base origin/<base branch>`.
2. **bootstrap** (`needs: lint`) — `mysql:8.0` service container; installs the
   `migrate` CLI from the golang-migrate release tarball; runs **full up from
   an empty DB → full down → full up again**. The re-bootstrap proves the
   down migrations actually clean up after themselves.

The `merge_group` trigger is load-bearing. With GitHub's merge queue enabled,
checks re-run against the *merged* result. That's what catches two PRs that
independently claimed the same version number: each is green alone, but the
merge-group run of whichever lands second fails the duplicate/append-only
checks. Without a merge queue, the second PR would merge green and break main.

## Known limitations (honestly)

- **Static SQL lint can't see everything.** Dynamic SQL, prepared statements
  built from strings, and stored procedures are invisible to sqlglot and the
  regex fallback. The bootstrap job is the backstop: if a migration actually
  touches something that doesn't exist yet in topo order, the real MySQL run
  fails.
- **This buys isolation, not parallelism.** golang-migrate's MySQL driver
  takes an advisory lock (`GET_LOCK`) keyed per *database*, not per tracking
  table — so per-domain runs against the same database serialize anyway.
  The win is independent version sequences and enforced ownership, not
  concurrent migration execution.
- **Multi-domain migrations are awkward by design.** A change that must touch
  two domains' tables in one file needs the `lint:allow` pragma plus review.
  If you need that often, your domain boundaries are probably wrong.
- **Down migrations are only as honest as you write them.** CI's
  up→down→up cycle catches drops that forget dependent objects, but it can't
  verify data restoration.

## Required repo settings (can't be pushed as code)

These must be configured in GitHub settings by an admin:

1. **Enable merge queue on `main`** (Settings → Branches/Rulesets → require
   merge queue). Without it, `merge_group` never fires and same-version races
   land green.
2. **Required status checks** on `main`: add both `lint` and `bootstrap`.
3. **CODEOWNERS enforcement** (require review from code owners): the
   `.github/CODEOWNERS` file in this repo routes `migrations/dag.yaml` (the
   ownership/DAG registry) for mandatory review. Any PR adding a
   `lint:allow` pragma should likewise get owner review — a simple guard is a
   required PR review on `scripts/**` and `migrations/**` plus grepping for
   `lint:allow` in review.

## Local development

```sh
# 1. MySQL 8 in docker
docker run --rm -d --name migration-dag-mysql \
  -e MYSQL_ROOT_PASSWORD=rootpass -e MYSQL_DATABASE=app \
  -p 3306:3306 mysql:8.0

# 2. migrate CLI: https://github.com/golang-migrate/migrate/releases
#    and lint deps:
pip install pyyaml sqlglot

# 3. lint, then migrate everything up in DAG order
python3 scripts/migrate-lint.py
DB_USER=root DB_PASS=rootpass scripts/migrate-up.sh

# roll everything back (reverse DAG order)
DIRECTION=down DB_USER=root DB_PASS=rootpass scripts/migrate-up.sh
```

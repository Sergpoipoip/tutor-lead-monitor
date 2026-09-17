# Tutor Lead Monitor

Milestone 1 foundation for discovering permitted literature-tutor requests.
The authoritative documents are [PROJECT_SPEC](docs/PROJECT_SPEC.md),
[ARCHITECTURE](docs/ARCHITECTURE.md), and [SOURCES](docs/SOURCES.md).

Implemented: typed configuration, JSON logging, domain contracts, a deterministic
fixture collector, PostgreSQL persistence primitives, initial migrations, and a
local container lifecycle. Classification, scoring, fuzzy deduplication, retention
jobs, Telegram delivery, real collectors, and production scheduling come in later
milestones. No third-party account or API credential is required now.

## Local setup

Prerequisites: Python 3.12+ and uv, plus Docker Desktop/Engine with Compose v2
(or an existing PostgreSQL 16+ server). Run commands from the repository root.

```sh
uv sync --locked
cp .env.example .env
```

Edit `.env`: replace the two database password placeholders with different locally
generated passwords, and put the application password in `DATABASE_URL` as well.
For example, run `openssl rand -hex 24` separately for each password. Hex passwords
avoid URL-encoding issues with Compose interpolation. These are local database
passwords, not provider credentials. `.env` is ignored and excluded from the image.

For host Python with container PostgreSQL:

```sh
docker compose up -d --wait postgres
uv run tutor-lead-monitor check-config
uv run tutor-lead-monitor migrate
uv run tutor-lead-monitor sync-sources
uv run tutor-lead-monitor fixture
uv run tutor-lead-monitor healthcheck
uv run tutor-lead-monitor serve
```

`fixture` previews stable IDs and page cursors without writing records or printing
post text. It produces four synthetic records across two pages. Their IDs and UTC
timestamps are fixed; `since` is exclusive, and the versioned cursor resumes by
offset. The persistence primitives are exercised separately by integration tests.
The fixture-to-lead processing use case is Milestone 2.

`serve` validates configuration and the current migration revision, then stays
running with periodic database health logs. SIGINT/SIGTERM stops it cleanly. It
does not schedule collection or send notifications in this milestone. Health
failures during the loop are reported and rechecked at the next interval.

## Docker Compose

After filling in `.env`:

```sh
docker compose up --build -d --wait
docker compose ps
docker compose logs --tail=50 app migrate
docker compose exec app tutor-lead-monitor fixture
docker compose exec app tutor-lead-monitor sync-sources
docker compose down
```

The `postgres` service initializes a separate `tutor_app` database owner with
`NOSUPERUSER`, `NOCREATEDB`, and `NOCREATEROLE`. A one-shot `migrate` service must
succeed before `app` starts. The application container runs as a non-root OS user.
Only PostgreSQL is published, on loopback; change `POSTGRES_PORT` if needed and
update the host-side `DATABASE_URL` accordingly. Compose constructs its own URL
with hostname `postgres`.

The named volume survives `docker compose down`. Initialization scripts run only
on an empty volume: changing an environment password does not update an existing
database role. Change existing passwords with PostgreSQL administration tools.
`docker compose down --volumes` deletes local database data; use it only for an
intentional disposable reset.

This is a local setup, not a production deployment. Always-on scheduling, restart
policies, backups/restoration, log rotation, monitoring, and unattended-operation
verification remain Milestone 6.

## Configuration

Settings use typed defaults plus the version-controlled YAML files below. Process
environment values override `.env` for deployment settings; explicit `Settings`
constructor values override both in tests. The current CLI's `--source` selects
one configured fixture source. Business rules are changed in YAML, rather than
through hidden environment overrides.

| File / variable | Purpose |
|---|---|
| `config/business.yml` | Timezone (initially UTC), local digest time, retention, deduplication defaults |
| `config/scoring.yml` | Versioned weights and ordered notification thresholds |
| `config/queries.yml` | Provider-neutral query groups for later collectors |
| `config/sources.yml` | Active registry; only the synthetic fixture is enabled |
| `config/sources.example.yml` | Disabled future source reference; never loaded automatically |
| `DATABASE_URL` | Required PostgreSQL URL using `postgresql+psycopg://` |
| `CONFIG_DIR` | YAML directory; defaults to `config` |
| `LOG_LEVEL` | DEBUG, INFO, WARNING, or ERROR; defaults to INFO |
| `HEALTH_INTERVAL_SECONDS` | Positive foundation health polling interval; defaults to 30 |

Validation rejects unknown YAML fields, duplicate source keys, invalid timezones,
nonpositive retention/intervals, overlapping thresholds, unapproved enabled
sources, and every enabled real source. Do not put secrets into source options,
policy notes, or metadata. Future registry entries reference credentials by
environment-variable name only. Telegram token/allowlist validation must be added
with delivery in Milestone 3; neither is needed by the fixture foundation.

`sync-sources` explicitly upserts registry entries and initializes their cursors;
it does not remove historical sources omitted from the YAML. Disable an existing
entry explicitly before removing it. Collection orchestration must honor both
registry and operational pause state when implemented.

Logs contain event names and selected operational fields. The CLI reports safe
error categories without echoing configuration values, SQL parameters, exception
messages, or post bodies. For a configuration error, inspect the corresponding
local settings against their typed definitions in `src/tutor_lead_monitor/config.py`.

## Database and migrations

```sh
uv run tutor-lead-monitor migrate
uv run alembic current
uv run alembic check
uv run alembic upgrade head --sql
# After an intentional ORM schema change:
uv run alembic revision --autogenerate -m "Describe the schema change"
```

Review generated migrations before applying them. `migrate` is the normal safe
startup wrapper; direct Alembic commands are development tools. To reverse the
initial migration on a disposable database, use `uv run alembic downgrade base`;
this drops the application tables and their data.

Revision `0001` creates `sources`, `collector_states`, `collection_runs`,
`raw_items`, `leads`, `lead_occurrences`, `notifications`, `notification_items`,
and `feedback`. UUIDs are generated by the application; timestamps use PostgreSQL
`timestamptz` and connections select UTC. Raw fingerprints remain NULL until
Milestone 2 normalization. Leads refer to raw evidence; their ID is the canonical
duplicate-group identity.

Raw `(source_id, external_id)` uniqueness prevents repeat insertion and preserves
the first collected evidence. `persist_page` inserts a page and advances its
cursor in one caller-owned transaction, using a savepoint for page rollback.
It does not implement normalization or cross-source deduplication. Callers must
serialize collection per source before using it in concurrent jobs.

Immediate notifications are unique per lead/recipient/kind. Digest envelopes are
unique per recipient/kind/local-date period and have separate membership rows.
Foreign keys prevent accidental evidence or feedback deletion. The Milestone 2
retention task must explicitly handle dependent records and preserve feedback
until manual deletion; no broad delete cascades silently erase it.

## Tests and development checks

```sh
uv run pytest tests/unit
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Integration tests use actual PostgreSQL and Alembic, never SQLite or `create_all`.
Use a dedicated disposable database with a name ending in `_test`. Its schema is
reset between tests. `TEST_DATABASE_URL` must be in the process environment;
integration tests skip with an explicit reason when it is absent.

With the local Compose PostgreSQL already started, create the test database once:

```sh
docker compose exec postgres createdb -U postgres -O tutor_app tutor_lead_monitor_test
# Substitute the same local application password and your published port:
export TEST_DATABASE_URL='postgresql+psycopg://tutor_app:REPLACE_WITH_LOCAL_APP_PASSWORD@localhost:5432/tutor_lead_monitor_test'
uv run pytest
```

Tests cover deterministic pagination/resumption, configuration and secret
redaction, UTC conversion, migration round trips and ORM drift, atomic raw-page
persistence/rollback, idempotent raw insertion, source policy checks, lead bounds,
notification uniqueness, and evidence/feedback foreign keys. No test calls a real
collector or delivery service. Do not run parallel integration workers against
the same test database.

`uv.lock` pins resolved runtime and development versions with hashes. To update
dependencies deliberately: `uv lock --upgrade`, `uv sync --locked`, then rerun the
checks and database suite. The Docker image installs the locked runtime subset.

## Before Milestone 2

No external provider decision or credential blocks pipeline implementation.
Use anonymized positive, offering, vacancy, indirect, and cross-post examples to
expand the fixture corpus. Keep score/retention/deduplication defaults until
evidence supports changes. Define per-item failure/retry behavior, per-source
locking and checkpoint orchestration, and dependency-aware retention as part of
Milestone 2. Choose the user's timezone and digest preferences before delivery;
UTC/19:00 are provisional. Real-source review and production host decisions remain
in their later milestones.

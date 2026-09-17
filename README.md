# Tutor Lead Monitor

Milestones 1–2: a local, deterministic pipeline for permitted literature-tutor
requests. Read the authoritative [product specification](docs/PROJECT_SPEC.md),
[architecture](docs/ARCHITECTURE.md), and [source policy](docs/SOURCES.md), version 0.2.

Implemented: typed configuration, JSON logs, fixture collection, atomic raw
persistence, normalization, rule classification, conservative extraction,
explainable scoring, exact/fuzzy deduplication, and retention with dry-run counts.
Telegram delivery, real collectors, production scheduling, and deployment remain
later milestones. No provider account or API credential is needed.

## Local setup

Prerequisites: Python 3.12+, uv, and Docker with Compose v2 (or PostgreSQL 16+).
Run commands from the repository root.

```sh
uv sync --locked
cp .env.example .env
```

Replace the two database password placeholders in `.env` with independently
generated local passwords (for example, `openssl rand -hex 24` for each).
Put the application password in `DATABASE_URL` too. Hex passwords avoid URL
escaping problems in Compose. `.env` is ignored and excluded from the image.

```sh
docker compose up -d --wait postgres
uv run tutor-lead-monitor check-config
uv run tutor-lead-monitor migrate
uv run tutor-lead-monitor sync-sources
uv run tutor-lead-monitor healthcheck
```

PostgreSQL is exposed on loopback. If changing `POSTGRES_PORT`, update the
host-side URL. Compose constructs its own URL using hostname `postgres`.
The `tutor_app` role owns the application database and has no superuser,
create-database, or create-role privileges. Initialization runs only on an empty
volume; editing `.env` does not change an existing role's password.

## Controlled pipeline commands

```sh
# Preview stable IDs/cursors without database writes or post text output:
uv run tutor-lead-monitor fixture
# Collect one approved fixture source:
uv run tutor-lead-monitor collect --source fixture
uv run tutor-lead-monitor collect --source fixture_crosspost
# Process pending items independently (bounded work per invocation):
uv run tutor-lead-monitor process --limit 1000 --as-of 2026-01-01T13:00:00Z
# Collect every enabled fixture source, then process pending items:
uv run tutor-lead-monitor pipeline --as-of 2026-01-01T13:00:00Z
# Repeating the pipeline is safe:
uv run tutor-lead-monitor pipeline --as-of 2026-01-01T13:00:00Z
```

The primary fixture contains 13 synthetic records; a second source contains one
near duplicate. The original four IDs, texts, timestamps, and URLs are preserved.
Cursors remain version 1, so an existing offset of 4 resumes at the appended
examples. Cross-post cursors also identify their dataset. The fixture has no real
contact details and uses reserved `example.invalid` URLs.

Fixture publication/collection times are fixed at `2026-01-01T12:00:00Z`.
`--as-of` supplies a reproducible evaluation clock for scoring and retention;
it must include a timezone. Omit it to use current UTC time. At the demonstration
clock, a clean run produces **14 raw records, 6 canonical leads, 8 occurrences**:
8 processed candidates (including two duplicates) and 6 rejected records. Using
the current clock makes the old fixtures stale and can change eligibility.

The processing limit applies to each explicit invocation; run `process` again
when more pending records remain. Collection/processing failures produce a
nonzero CLI exit status and sanitized error categories. The complete pipeline
continues with other enabled sources after a source failure. No command sends
notifications or starts a collection scheduler.

## Processing and transaction boundaries

Pure functions live in `processing/`; use cases in `application/` orchestrate
PostgreSQL adapters in `db/`. Collectors only return domain records.

- Normalization preserves raw text, creates readable NFKC text, canonicalizes
  HTTP(S) URLs and tracking parameters, and creates a SHA-256 fingerprint from
  case-folded, punctuation/whitespace-normalized text with ё/е matching, configured
  boilerplate removal, and URL/phone placeholders.
- Classification uses versioned regex rules, stable IDs, explicit priorities,
  confidence, and evidence offsets into normalized text. Both explicit and
  indirect needs are recognized. Offering, agency, and vacancy rules take priority.
- Extraction records grade, goals, format, labeled location, urgency, budget,
  contact availability, and evidence. Unknown/conflicting fields remain unset.
  Contact availability comes from explicit text or authorized source metadata
  `response_available: true`; a generic source URL alone earns no contact bonus.
- Scoring returns ordered reasons, clamps to 0–100, and persists its version.
  Future/unknown publication timestamps receive no freshness bonus. The freshness
  cutoff is exclusive; staleness begins at the configured day boundary.
- Seeking/uncertain literature or mixed Russian/literature items at or above the
  review threshold create leads. Other items remain raw records marked rejected,
  with their classification/extraction/score evidence retained for review.

Derived evidence is stored under reserved raw metadata key `_tlm_processing`;
collectors cannot supply that key. Raw text, URLs, and other source metadata are
preserved. No database migration is needed: revision `0001` already contains the
necessary columns and JSONB storage. Historical canonical scores are not rewritten
when another occurrence is attached.

Collection takes a per-source session advisory lock on a dedicated connection.
Each page commits its valid items, run counters, and checkpoint together.
Individual persistence failures roll back to savepoints; later valid items still
persist, but the cursor and high-water mark never advance beyond the first failed
page. Retrying replays those pages and ignores already stored source IDs. Fix
persistent malformed source records before retrying; there is no silent skip or
automatic destructive cleanup. Interrupted `running` audit records are reconciled
as failed when the next invocation acquires that source's lock.

Each processing transaction claims one item with `FOR UPDATE SKIP LOCKED`.
An item failure rolls back its derived changes and marks it failed with only an
error category. Other items continue. Failed processing rows are not retried
automatically; after correcting the cause, explicitly reset selected rows to
`pending` through reviewed database maintenance. Existing occurrence uniqueness
makes such reprocessing safe.

Text deduplication uses a transaction advisory lock around candidate lookup and
canonical-lead insertion. This serializes the decision step to prevent races even
when no matching row exists yet. Candidate claims remain independent. This is a
correctness-first tradeoff for the single-service MVP, not a high-throughput queue.

## Exact and fuzzy matching

1. The source/external-ID database constraint prevents duplicate raw insertion.
2. An existing occurrence or canonical URL identifies the same lead.
3. Text matching considers matching intent/subject within the configured publication
   window (collection time is the fallback), initially 30 days in either direction.
   Exact fingerprints match before fuzzy comparison.
4. Fuzzy candidates are narrowed by grade and meaningful token anchors, then scored
   with token-set Jaccard similarity: `100 * intersection / union`. The default
   threshold is 90. Both texts need at least five meaningful tokens.
5. Conflicting known grades, goals, online/offline formats, labeled locations,
   budgets, or phone-contact signatures block text-based merging. Unknown fields
   do not manufacture a conflict. Borderline scores stay separate.

Every accepted occurrence retains its original raw record and URL. An explicit
source identity or URL match takes precedence over text differences, allowing
edits/reposts to retain a common canonical identity. Matching is deliberately
conservative: there is no stemming or synonym model, and fuzzy matching is
restricted to recent candidates.

## Configuration

Environment values override `.env`; explicit settings constructor values override
both in tests. All non-secret business settings are version-controlled YAML.

| File / variable | Purpose |
|---|---|
| `config/processing.yml` | Classifier version, regex rules, priorities, confidence, boilerplate |
| `config/scoring.yml` | Score version, weights, thresholds, freshness/stale periods |
| `config/business.yml` | Timezone, future digest preferences, retention, deduplication |
| `config/sources.yml` | Enabled synthetic sources and required operations policy |
| `config/sources.example.yml` | Disabled future-source reference, not automatically loaded |
| `config/queries.yml` | Provider-neutral query groups for future collectors |
| `DATABASE_URL` | Required `postgresql+psycopg://` URL |
| `CONFIG_DIR` | YAML directory, defaults to `config` |
| `LOG_LEVEL` | DEBUG, INFO, WARNING, ERROR; defaults to INFO |
| `HEALTH_INTERVAL_SECONDS` | Foundation health polling interval, defaults to 30 |

Validation rejects unknown YAML fields, duplicate source/rule IDs, invalid regexes,
invalid timezones, nonpositive intervals, overlapping score bands, unapproved
enabled sources, and every enabled real source. Never place secrets in YAML or
metadata. Telegram secrets and allowlist validation belong to Milestone 3.

Every source requires `operations`: positive `freshness_sla_seconds` and
`pause_after_consecutive_failures`, a non-empty `quota_policy`, and optional
`authorization_expires_at`. Expiry must be timezone-aware and is normalized to UTC;
expired authorizations and stored pauses block collection. Failure counters and
last-success times persist. Automated freshness/quota monitoring, threshold-based
pausing, and operational alerts are not implemented yet.

`sync-sources` is an explicit registry upsert and initializes missing cursors.
It can apply a reviewed re-enablement; ordinary collection honors existing stored
disabled/paused state. Omitted YAML entries are not deleted from history.
Disable an entry explicitly before removing it. Logs emit event names, internal
IDs, counts, and safe categories, never post bodies or arbitrary exception text.

## Retention

```sh
uv run tutor-lead-monitor retention --dry-run
# Review counts, then explicitly request deletion:
uv run tutor-lead-monitor retention --apply
```

Dry run is the default and reports the same selection counts without deleting.
Defaults: rejected raw records 30 days, leads/occurrences 90 days since last seen,
terminal notifications and finished collection-run metrics 180 days. Cutoffs are
strictly older-than; boundary records remain. Source retention can shorten the
period for unreferenced processed/rejected raw records.

Deletion is explicit and ordered: notification membership, expired terminal
notifications, occurrences, leads, unreferenced raw items, finished runs. Feedback
is never deleted. Feedback-linked leads and their raw evidence remain, as do leads
referenced by retained notifications. Pending/sending notifications, pending/failed
raw items, active runs, sources, and cursors remain for diagnosis or future work.
Dependencies can therefore extend retention beyond its nominal period.

Retention holds an exclusive maintenance advisory lock; collection pages and
processing transactions take its shared counterpart. Future notification/feedback
writers must use the same gate. No broad cascade deletion is used.

## Containers, migrations, and CI

```sh
docker compose up --build -d --wait
docker compose exec app tutor-lead-monitor pipeline --as-of 2026-01-01T13:00:00Z
docker compose logs --tail=50 app migrate
docker compose down
```

The application runs as a non-root OS user. A one-shot migration service succeeds
before it starts. `serve` only maintains the health-reporting lifecycle and exits
cleanly on SIGINT/SIGTERM. Business commands remain explicit. Named volumes survive
`down`; `down --volumes` deliberately destroys local data. This is not a production
deployment; restart policies, backups/restores and unattended tests remain later work.

```sh
uv run alembic upgrade head
uv run alembic check
uv run alembic upgrade head --sql
# Only on a disposable database: downgrade drops all application tables/data.
uv run alembic downgrade base
uv run alembic upgrade head
```

Review any future generated migration before applying it. Use
`uv run tutor-lead-monitor migrate` as the normal sanitized startup wrapper.

```sh
uv run pytest tests/unit
uv run ruff check .
uv run ruff format --check .
uv run mypy
# With Compose PostgreSQL running, create a dedicated test database once:
docker compose exec postgres createdb -U postgres -O tutor_app tutor_lead_monitor_test
export TEST_DATABASE_URL='postgresql+psycopg://tutor_app:REPLACE_WITH_LOCAL_APP_PASSWORD@localhost:5432/tutor_lead_monitor_test'
uv run pytest tests/unit tests/integration
```

Integration tests reset a disposable database whose name must end in `_test`.
They use PostgreSQL/Alembic, not SQLite or `create_all`. Never run parallel test
workers against the same test schema. Without `TEST_DATABASE_URL`, local database
tests explicitly skip; in CI or with `REQUIRE_INTEGRATION_TESTS=1`, missing database
configuration fails the suite.

`.github/workflows/ci.yml` runs Python 3.12 with a PostgreSQL 17 service, locked uv
installation, unit/integration tests, lint, formatting, strict mypy, migration
upgrade/downgrade, and Alembic consistency. Integration tests additionally compare
ORM metadata to the migrated schema. Validate workflow syntax locally with:

```sh
docker run --rm -i rhysd/actionlint:latest - < .github/workflows/ci.yml
```

`uv.lock` continues to pin the existing dependencies; Milestone 2 adds no new
runtime library. Update dependencies deliberately with `uv lock --upgrade` and
rerun all checks. The Docker image installs the locked runtime subset.

## Before Milestone 3

Expand the anonymized corpus and review false positives/false merges before real
source enablement. Rules are heuristic; evidence spans refer to normalized text,
location extraction requires explicit labels, and similarity is lexical.
Feedback-protected evidence and unresolved failed/pending work need manual review.
Choose the Telegram owner allowlist, timezone/digest time, and final delivery
thresholds; then implement delivery and notification retries against fake clients
first. No Telegram messages or real-source requests have been made by this pipeline.

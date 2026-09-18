# Tutor Lead Monitor

Milestones 1–3: a local, deterministic pipeline for permitted literature-tutor
requests. Read the authoritative [product specification](docs/PROJECT_SPEC.md),
[architecture](docs/ARCHITECTURE.md), and [source policy](docs/SOURCES.md), version 0.2.

Implemented: typed configuration, JSON logs, fixture collection, atomic raw
persistence, normalization, rule classification, conservative extraction,
explainable scoring, exact/fuzzy deduplication, and retention with dry-run counts.
Telegram alerts, digests, owner commands, and feedback are implemented for explicit
local execution. Real collectors, scheduling, and deployment remain later milestones.
Only Telegram functionality requires a bot token; fixtures and processing work without it.

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
continues with other enabled sources after a source failure. Collection and processing commands do not send notifications. Delivery uses the
explicit commands below; no collection scheduler is started.

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
necessary columns and JSONB storage. Each occurrence retains its processing
evidence even when a better occurrence becomes canonical.

Rules and scoring defaults are versioned `rules-v2`. “Русская литература” is a
literature subject, family requests for help are recognized, and “требуется
учитель” alone is insufficient evidence of employment. The synthetic Russian
corpus in `tests/fixtures/classification_ru.yml` covers positive, negative,
ambiguous, and negated examples. Default bands are review **45–54**, daily digest
**55–89**, and immediate **90–100**. A fresh “Ищу репетитора по литературе” scores
61 and is digest-eligible; delivery uses the explicit commands below.

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
automatically; after correcting the cause, use the bounded manual commands below.
Existing occurrence uniqueness makes reprocessing safe.

```sh
# Total failed count plus at most 20 internal UUIDs; no post/error/contact content:
uv run tutor-lead-monitor failed --limit 20
# Reset only the selected failed record (replace the placeholder with a listed UUID):
uv run tutor-lead-monitor retry-failed --record-id RECORD_UUID
# Or explicitly reset up to 20 oldest available failed records:
uv run tutor-lead-monitor retry-failed --limit 20
uv run tutor-lead-monitor process --limit 20
```

Inspection defaults to 100 IDs. Reset requires exactly one UUID or an explicit
limit of 1–1,000; it has no implicit “reset all.” Only failed rows are changed:
their status becomes pending and their error category is cleared, preserving raw
data and existing evidence. Locked rows are skipped and reported reset counts
reflect actual changes; inspect again if a selected row is busy. Repeating a reset
is a no-op until that row fails again. Resetting does not execute processing.

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

Canonical promotion ranks eligible occurrences by score, then extraction
completeness (known grade, goals, format, location, budget, urgency, contact), then
lowest internal UUID for a stable tie-break. A higher score wins; at equal scores,
more complete data wins. A lower-scoring occurrence remains evidence even if it
adds fields. Promotion replaces classification, extraction, score, ordered reasons,
and both versions together, so explanations remain consistent and scores never
decrease. Lead identity, user state, feedback, and every occurrence remain intact.
The same deduplication transaction lock protects promotion and insertion.

An eligible occurrence also recovers previously rejected raw evidence with the
same canonical URL, or the same nonempty exact fingerprint within the matching
window and compatible classification, attributes, and known phone signatures.
Recovered records become processed occurrences; their original classification and
score evidence is retained. Fuzzy-only historical matches stay rejected for manual
review. This recovery cannot restore raw records already removed by retention.

## Configuration

Environment values override `.env`; explicit settings constructor values override
both in tests. All non-secret business settings are version-controlled YAML.

| File / variable | Purpose |
|---|---|
| `config/processing.yml` | Classifier version, regex rules, priorities, confidence, boilerplate |
| `config/scoring.yml` | Score version, weights, thresholds, freshness/stale periods |
| `config/business.yml` | Timezone, local digest preferences, retention, deduplication |
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
metadata. Telegram commands additionally validate secret settings and the owner allowlist.

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
notifications (all statuses) and finished collection-run metrics 180 days. Cutoffs are
strictly older-than; boundary records remain. Source retention can shorten the
period for unreferenced processed/rejected raw records.

Deletion is explicit and ordered: expired notification chunks, membership,
envelopes, occurrences, leads, unreferenced raw items, finished runs. Notification
age is measured from the envelope's `created_at`, including pending/failed sends.
Feedback is never deleted. Feedback-linked leads and their raw evidence remain, as
do leads referenced by unexpired notifications. Feedback never extends retention
of notification text, buttons, membership, or envelopes. Pending/failed raw items,
active runs, sources, and cursors remain for diagnosis or future work.

Retention holds an exclusive maintenance advisory lock; collection pages and
processing transactions take its shared counterpart. Notification and feedback
writers use the same gate. A nonblocking recipient lock defers deletion only while
a sender owns that lock (including its bounded network request); rerun retention
after the send finishes. Stored pending/sending status alone does not defer expiry.
No broad cascade deletion is used. Payload-free replay markers are described below;
dry runs neither create markers nor delete records.

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

## Telegram setup and explicit operation

Create a bot using the official [BotFather guide](https://core.telegram.org/bots/tutorial#obtain-your-bot-token).
Keep its token only in your ignored `.env` or environment. Never commit it or put
it in shell history, logs, or screenshots. Obtain your numeric owner ID from a
trusted Telegram client/account export; usernames are insufficient. Open a private
conversation with your bot and press Start before trying delivery.

```dotenv
TELEGRAM_BOT_TOKEN=REPLACE_WITH_BOTFATHER_TOKEN
TELEGRAM_ALLOWED_USER_IDS=REPLACE_WITH_COMMA_SEPARATED_OWNER_IDS
TELEGRAM_RECIPIENT_CHAT_ID=REPLACE_WITH_PRIVATE_OWNER_CHAT_ID
```

The recipient must be a positive private chat ID in the allowlist. Group/channel
delivery is excluded from this owner-only MVP. Settings validate on Telegram startup;
fixture commands work without them. The maintained
[python-telegram-bot library](https://docs.python-telegram-bot.org/en/stable/) is
isolated in the Telegram adapter; no job-queue extra is installed.

```sh
uv sync --locked
uv run tutor-lead-monitor migrate
# Long polling for owner commands and feedback only:
uv run tutor-lead-monitor telegram-bot
# In a second terminal, explicitly send eligible alerts/digests:
uv run tutor-lead-monitor notify-immediate --limit 100
uv run tutor-lead-monitor send-digest
uv run tutor-lead-monitor send-digest --local-date 2026-09-18
```

Migration `7d8905a1ece6` adds recipient pause state, frozen notification chunks and
callback receipts. Existing evidence and constraints are preserved. Downgrading
removes this new delivery state and is for disposable data or reviewed rollback only.
Migration `b35778628004` adds payload-free notification replay markers without
changing existing membership or chunks. Downgrading it discards expired-notification
replay protection and requires the same rollback review.
Compose passes optional Telegram variables into containers; `serve` remains the
health lifecycle and does not start the bot or a scheduler.

Owner commands: `/start`, `/help`, `/status`, `/digest`, `/pause`, `/resume`.
Commands work only in an authorized user's private chat; callbacks must originate
from the configured recipient chat. Unknown users receive no command response or
administrative details. `/status` reports DB availability, persistent pause state,
recent collection runs, processing/notification counts, and the next configured
09:00 Europe/Rome digest time. That time is informational in Milestone 3.

`/pause` blocks immediate and non-explicit digest delivery across restarts.
Explicit `/digest` and CLI `send-digest` remain allowed while paused. An in-flight
send cannot be recalled; a pause command encountering the recipient lock reports
busy and must be retried. Collection and processing continue.

### Digest and feedback semantics

A digest includes all currently active leads at or above the digest threshold that
have never been reserved in a digest for that recipient. Selection checks prior
`NotificationItem` membership across all periods and statuses, plus replay markers
after notification expiry. Lead creation date does not restrict eligibility.
Immediate-alert history is independent: an already-alerted lead can appear in one
digest. Ordering is score descending, canonical publication or collection freshness
descending, then UUID ascending.

Statistics use the requested local calendar day, from midnight inclusive to the
next midnight exclusive, converted independently to UTC (23/25 hours on Rome DST
days). Collected/rejected counts use raw collection time; new-lead and score-band
counts use lead creation time and current state at the snapshot. These statistics
are frozen with the envelope; they describe that reporting day, while the cards
may include older backlog. Requesting a historical date does not reconstruct
historical eligibility or state.

The first nonempty request freezes membership and chunks. Repeated requests resume
delivery or return the existing result. Later arrivals and leads promoted from
review to digest eligibility enter the next new period's digest, even if originally
created on an earlier date. Membership in an unfinished digest stays reserved for
that envelope's retry, rather than being copied into a later digest. No empty
message or envelope is created with `send_empty_digest: false`, so a later nonempty
request still works. Thresholds are review 45, digest 55, immediate 90.

Messages use escaped HTML, HTTP(S) links, bounded excerpts and conservative UTF-16
limits. Complete cards and links stay intact across chunks. Numbered feedback rows
identify their digest card; callback data contains only an action and UUID.
Interested → interested; Not relevant → rejected; Duplicate → duplicate; Closed →
closed. Changed choices append feedback; the latest accepted choice wins. Identical
consecutive choices are no-ops, and replayed callbacks cannot undo later choices.
Scores/reasons stay unchanged. Authorized callbacks receive an acknowledgement
attempt even for invalid data or missing leads.

### Delivery retries and uncertain outcomes

Envelopes reserve before sending. Chunk attempts commit before network I/O; a
recipient session lock prevents competing senders. Sent chunks are not intentionally
resent. Safe connection failures and rate limits retry the same chunk, at most three
attempts, with persisted exponential backoff, jitter and Telegram retry-after. Rerun
the same command after the due time; no retry scheduler is installed. A failed alert
does not block other leads. Authorization, malformed and permanent failures stop.

Telegram sendMessage has no caller-provided idempotency key. A timeout/lost response
may mean Telegram accepted the message, so exactly-once network delivery cannot be
guaranteed. These chunks and interrupted `sending` attempts become ambiguous and
are never automatically resent or replaced. Review the owner chat and internal
notification/chunk IDs before targeted database repair; do not blindly reset them.
A crash after sending but before recording success has the same ambiguity.
Errors/logs contain safe categories, never updates, tokens, contacts or post bodies.

Frozen chunks contain the authorized excerpt and expire with their envelope after
the configured notification retention period (default 180 days). Retention removes
chunks, text, buttons, membership, envelopes, and delivery metadata even when
feedback protects the lead and raw evidence. Retry unfinished delivery within that
window: expired reservations, including failed or uncertain sends, are consumed
and cannot be reopened or automatically replaced.

Before deleting an expired envelope, the same transaction writes only replay keys
to `notification_markers`: a SHA-256 recipient hash, kind, and either an internal
lead UUID or SHA-256 digest-period hash. There are no post texts, URLs, contacts,
message bodies, provider message IDs, timestamps, or aggregated metrics. Immediate
and digest lead keys remain while their lead remains and are removed when retention
deletes that lead. Digest-period keys remain until explicit manual recipient erasure,
so rerunning an expired period cannot create another envelope even after its leads
are deleted. These keys are replay protection, not retained notification history.
Manual erasure must remove the corresponding keys as well; erasing keys loses that
replay protection. Callback receipts remain with feedback and must be explicitly
deleted before manually deleting the corresponding feedback.

Tests block real Telegram transport calls. The full suite covers fake transport
retries, concurrent sends, DST, callbacks, persistent pause and migration drift.

## Before Milestone 4

Expand the anonymized corpus and review false positives/false merges before real
source enablement. Rules are heuristic; evidence spans refer to normalized text,
location extraction requires explicit labels, and similarity is lexical.
Feedback-protected evidence and unresolved failed/pending work need manual review.
Review source permissions and credential handling before implementing any real
collector. All delivery verification uses fake clients; implementation tests never
call Telegram. Perform an owner-controlled live bot smoke test after configuring
local credentials. No production scheduler or deployment is included.

# Tutor Lead Monitor — Technical Architecture

Status: implementation-ready draft  
Version: 0.2  
Last updated: 2026-09-17

## 1. Architectural principles

1. **Collectors only collect.** They do not decide whether an item is a lead and do not send Telegram messages.
2. **Preserve raw evidence.** A normalized lead always points back to one or more immutable raw occurrences.
3. **Idempotency everywhere.** Re-running a collector or notification job must be safe.
4. **Rules before models.** The MVP uses deterministic, explainable rules; optional AI is an adapter, not a foundation.
5. **One deployable service first.** Keep clear module boundaries inside one Python application and one PostgreSQL database.
6. **Compliance is configuration.** A source cannot run unless its registry entry is enabled and describes an approved access method.
7. **UTC internally.** Store UTC timestamps; convert only at the delivery edge.

## 2. Context and data flow

```text
Permitted sources
      │
      ▼
  Collectors ──► raw_items ──► normalization/classification/extraction
                                     │
                                     ▼
                              scoring + deduplication
                                     │
                                     ▼
                                  leads
                                     │
                         ┌───────────┴───────────┐
                         ▼                       ▼
                  immediate alerts         daily digest
                         │                       │
                         └──────► Telegram ◄────┘
                                      │
                                      ▼
                                  feedback
```

The scheduler invokes application use cases. It must not contain business logic.

## 3. Deployment model

Local development and the first production deployment use the same container images and database migrations. Environment-specific configuration must remain outside the images.

Core Docker Compose services:

- `app`: Python application running scheduled collection/processing/delivery jobs and the Telegram bot;
- `postgres`: PostgreSQL database;
- optional `migrate` one-shot service or startup command for Alembic migrations.

Do not introduce a broker in the MVP. Use explicit database transactions and job locks. If volume later requires multiple workers, the use-case boundaries should be transferable to a queue without rewriting collectors or domain logic.

### 3.1 Local development

- expose PostgreSQL only when needed for local debugging;
- use bind mounts or rebuildable development images;
- allow manual `docker compose up` and fixture collectors;
- do not use production credentials or production data.

### 3.2 Production deployment

Production must run on an always-on host and require no daily manual command.

- Long-running services use `restart: unless-stopped` or an equivalent host-level restart policy.
- PostgreSQL uses a named persistent volume and is not exposed publicly.
- The application waits for a healthy database before starting normal jobs.
- Health checks distinguish process liveness from readiness to collect and deliver notifications.
- Only one scheduler instance may own each scheduled job; PostgreSQL advisory locks or a lock table prevent duplicate runs after restarts or overlapping schedules.
- Deployment secrets come from protected environment variables or a platform secret store, never Git or container images.
- Host firewall and remote access expose only the minimum required ports. Telegram long polling requires no public application port; webhooks, if later selected, require TLS and a separately reviewed ingress configuration.
- The host must use reliable time synchronization. All application timestamps remain UTC, with digest scheduling converted from the configured user timezone.
- Database backups must leave the application volume/host or use provider-managed snapshots; a backup stored only beside the database is insufficient.
- Production deploy, update, rollback, restart, backup, and restore commands belong in a concise runbook.

The initial production topology may remain a single host. High availability is not an MVP requirement; automatic restart, recoverable data, and visible failure are required.

## 4. Suggested repository layout

```text
tutor-lead-monitor/
├── AGENTS.md
├── README.md
├── .env.example
├── .gitignore
├── docker-compose.yml
├── pyproject.toml
├── alembic.ini
├── docs/
│   ├── PROJECT_SPEC.md
│   ├── ARCHITECTURE.md
│   └── SOURCES.md
├── config/
│   ├── sources.example.yml
│   ├── queries.yml
│   └── scoring.yml
├── migrations/
├── src/tutor_lead_monitor/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── logging.py
│   ├── db/
│   │   ├── base.py
│   │   ├── models.py
│   │   ├── repositories.py
│   │   └── session.py
│   ├── domain/
│   │   ├── enums.py
│   │   ├── models.py
│   │   └── policies.py
│   ├── collectors/
│   │   ├── base.py
│   │   ├── fixture.py
│   │   ├── manual_import.py
│   │   ├── web_search.py
│   │   ├── telegram_updates.py
│   │   └── avito_notifications.py
│   ├── processing/
│   │   ├── normalize.py
│   │   ├── classify.py
│   │   ├── extract.py
│   │   ├── score.py
│   │   └── deduplicate.py
│   ├── notifications/
│   │   ├── base.py
│   │   ├── telegram.py
│   │   ├── formatting.py
│   │   └── digest.py
│   ├── application/
│   │   ├── collect.py
│   │   ├── process.py
│   │   ├── notify.py
│   │   └── retention.py
│   └── scheduling/
│       ├── jobs.py
│       └── locks.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
└── scripts/
```

This is a target layout, not a reason to create empty modules prematurely. Milestone 1 should create only files needed for working code and tests.

## 5. Core domain contracts

Use typed domain models that do not depend on SQLAlchemy or Telegram classes.

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Mapping, Protocol


@dataclass(frozen=True)
class CollectedItem:
    source_key: str
    external_id: str
    url: str | None
    published_at: datetime | None
    collected_at: datetime
    text: str
    author_label: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CollectionContext:
    since: datetime | None
    cursor: Mapping[str, Any] | None
    run_id: str


@dataclass(frozen=True)
class CollectionPage:
    items: tuple[CollectedItem, ...]
    next_cursor: Mapping[str, Any] | None
    has_more: bool


class Collector(Protocol):
    key: str

    async def collect(self, context: CollectionContext) -> AsyncIterator[CollectionPage]: ...
```

Requirements:

- `external_id` must be stable within a source when the source provides one.
- If a source lacks stable IDs, the collector must create one from stable source fields, not collection time.
- Collector metadata must be JSON-serializable and must not contain credentials.
- A page cursor is committed only after its items are persisted successfully.
- Returning no items is a successful run, not an error.

## 6. Database model

Use UUID primary keys unless there is a clear reason otherwise. Use timezone-aware timestamps. Add `created_at` and `updated_at` where operationally useful.

### 6.1 `sources`

| Column | Notes |
|---|---|
| `id` | UUID PK |
| `key` | unique stable machine name |
| `kind` | enum/string: fixture, web_search, telegram, vk, avito, rss, manual |
| `display_name` | human-readable |
| `enabled` | false by default for real sources |
| `config` | JSONB non-secret configuration |
| `access_method` | official_api, authorized_bot, saved_notification, public_web, manual |
| `policy_status` | pending, approved, paused, blocked |
| `last_reviewed_at` | nullable |

### 6.2 `collector_states`

| Column | Notes |
|---|---|
| `source_id` | PK/FK to sources |
| `cursor` | JSONB |
| `high_water_mark` | nullable timestamp |
| `last_success_at` | nullable |
| `consecutive_failures` | integer |
| `paused_until` | nullable timestamp |

### 6.3 `collection_runs`

| Column | Notes |
|---|---|
| `id` | UUID PK/run ID |
| `source_id` | FK |
| `started_at`, `finished_at` | timestamps |
| `status` | running, succeeded, partial, failed |
| `items_seen`, `items_inserted`, `items_failed` | counters |
| `error_category`, `error_message` | sanitized; nullable |

### 6.4 `raw_items`

| Column | Notes |
|---|---|
| `id` | UUID PK |
| `source_id` | FK |
| `external_id` | stable source ID |
| `url`, `canonical_url` | nullable |
| `published_at`, `collected_at` | timestamps |
| `text` | original collected text |
| `normalized_text` | nullable until processed |
| `exact_fingerprint` | SHA-256 |
| `metadata` | JSONB, non-secret |
| `processing_status` | pending, processed, rejected, failed |
| `processing_error` | sanitized nullable text |

Constraints/indexes:

- unique `(source_id, external_id)`;
- index `published_at`, `processing_status`, `exact_fingerprint`;
- optional partial unique index on canonical URL only if source behavior makes it safe.

### 6.5 `leads`

| Column | Notes |
|---|---|
| `id` | UUID PK |
| `canonical_raw_item_id` | FK |
| `intent`, `subject` | enums/strings |
| `grade` | nullable small integer with check `1..11` |
| `goals` | array or JSONB set |
| `format` | online, offline, either, unknown |
| `location_text`, `budget_text` | nullable |
| `urgency` | low, normal, high, unknown |
| `score` | integer with check `0..100` |
| `score_reasons` | JSONB ordered feature list |
| `classification_version`, `scoring_version` | version strings |
| `status` | new, notified, interested, rejected, duplicate, closed, expired |
| `first_published_at`, `last_seen_at` | timestamps |

### 6.6 `lead_occurrences`

| Column | Notes |
|---|---|
| `lead_id` | FK |
| `raw_item_id` | unique FK |
| `similarity` | nullable numeric |
| `match_method` | exact_id, exact_url, exact_text, fuzzy_text, manual |

Composite primary key `(lead_id, raw_item_id)` is acceptable.

### 6.7 `notifications`

| Column | Notes |
|---|---|
| `id` | UUID PK |
| `lead_id` | nullable for digest summary records if desired |
| `recipient_key` | e.g. Telegram user ID as string |
| `kind` | immediate, digest |
| `period_key` | nullable; e.g. local date for digest |
| `status` | pending, sending, sent, failed, skipped |
| `attempt_count` | integer |
| `provider_message_id` | nullable |
| `last_error` | sanitized nullable |
| `sent_at` | nullable timestamp |

Idempotency constraints:

- unique `(lead_id, recipient_key, kind)` for immediate alerts;
- for digest membership, prefer a separate `notification_items` table with unique `(notification_id, lead_id)`;
- unique `(recipient_key, kind, period_key)` for one digest envelope per local day.

### 6.8 `feedback`

| Column | Notes |
|---|---|
| `id` | UUID PK |
| `lead_id` | FK |
| `recipient_key` | actor |
| `value` | interested, not_relevant, duplicate, closed |
| `created_at` | timestamp |
| `metadata` | JSONB without message text or secrets |

### 6.9 Milestone 3 delivery state

- `recipient_states`: recipient key, persistent delivery pause flag, timestamps.
- `notification_chunks`: envelope/position composite key, frozen escaped text and
  buttons, status, attempts, next retry time, safe error category, provider message
  ID and sent timestamp. `ambiguous` identifies uncertain sends.
- `callback_receipts`: callback ID primary key and feedback foreign key; replaying
  an old callback cannot undo newer feedback.

Recipient session advisory locks serialize reservation and delivery across workers.
Transactions finish before network calls. Successfully sent chunks are never resent.
Safe connection failures and rate limits retry the same chunk, at most three times,
with persisted exponential backoff, jitter and retry-after. Timeouts and interrupted
sends require manual review: Telegram sendMessage has no caller idempotency key.

Digest membership freezes on the first nonempty request for a recipient/local date,
using leads discovered (`created_at`) that day. Rome midnight boundaries convert
independently to UTC for DST. Immediate-alert leads remain eligible. Explicit
/digest and CLI send-digest are allowed while paused; immediate delivery is not.
Empty digests create no envelope when send_empty_digest is false.

Feedback retains changed choices; consecutive identical choices are no-ops and
new changed choices set lead status. Receipts deduplicate old callbacks. Scores and
reasons never change through feedback. Retention explicitly deletes chunks before
envelopes and retains idempotency envelopes for retained leads.

## 7. Transactions and idempotency

### Collection

For each page:

1. insert raw items using the `(source_id, external_id)` unique constraint;
2. treat conflicts as already seen;
3. commit the page;
4. then update the collector cursor in the same transaction, or in a transaction whose retry cannot skip unpersisted items.

The simplest safe design is to persist page items and cursor atomically.

### Processing

- Claim pending items using row locking (`FOR UPDATE SKIP LOCKED`) if concurrent workers are ever enabled.
- Update the raw item and lead/occurrence records in one transaction.
- A processing exception marks the item failed with a sanitized reason; a retry policy may reset retryable failures.

### Notifications

- Create a pending notification record first.
- Enforce the database idempotency key before calling Telegram.
- Mark `sending` with an attempt number.
- On ambiguous network outcomes, do not blindly create a new notification record; retry the same record according to provider semantics.

## 8. Processing pipeline

### 8.1 Normalization

Produce two forms:

- `normalized_text`: readable text for classification;
- `fingerprint_text`: stronger normalization for duplicate detection.

Normalization may:

- apply Unicode normalization;
- lowercase for matching while retaining original text;
- normalize `ё/е` in fingerprint-only comparisons;
- collapse whitespace;
- strip tracking parameters from canonical URLs;
- replace URLs and phone-like strings with placeholders in fingerprint text;
- remove configured repost boilerplate and repeated emoji.

Do not transliterate or stem aggressively in the first version; it can create false merges.

### 8.2 Rules classification

Keep rule sets in code/config with stable IDs and versions. Features should include:

- seek verbs: ищу, ищем, нужен, нужна, требуется, посоветуйте, порекомендуйте;
- indirect need: подтянуть, помочь, подготовить, плохо с литературой;
- offering patterns: я репетитор, набираю учеников, провожу занятия, мои услуги;
- organization patterns: школа, образовательный центр, курс, набор на курс;
- job patterns: вакансия, в штат, зарплата, трудоустройство;
- literature terms and exam/olympiad terms;
- grammatical and proximity rules where useful.

Rules return labels, confidence, matched rule IDs, and evidence spans. Never return only a boolean.

### 8.3 Extraction

Use conservative regex/dictionaries for grade, goal, format, location, urgency, and budget. Unknown is preferable to a confident wrong value. Store extraction evidence in processing metadata or score reasons.

### 8.4 Scoring

Use a pure function:

```python
ScoreResult = score(classification, extracted_fields, freshness, source_quality, config)
```

It returns:

- clamped integer score;
- ordered reasons with rule ID, delta, and short explanation;
- scoring configuration version.

### 8.5 Deduplication

Candidate generation should query recent leads using exact fingerprints and then a narrowed token/key search. Do not compare every record with every historical record.

Initial fuzzy features:

- token-set similarity of fingerprint text;
- identical normalized contact placeholder structure;
- same grade/goal/format when known;
- publication-time distance;
- common uncommon n-grams.

Use a high threshold first (for example 90/100) and tune from labeled feedback. Borderline matches remain separate.

Canonical occurrence selection is deterministic: highest eligible score, then most
complete extraction, then lowest internal raw UUID. Replace the canonical raw
pointer, classification, extraction, score reasons, and configuration versions
together. Never lower the lead score when a poorer duplicate arrives; keep every
occurrence and its historical processing evidence. Preserve lead identity and user
state. Serialize the match/promotion decision with the deduplication transaction lock.

Earlier rejected evidence may be attached through a canonical URL or a compatible
exact fingerprint within the configured time window. Preserve its original
processing explanation; do not recover historical fuzzy matches automatically.

## 9. Application use cases

### `run_collector(source_key)`

- validates source is enabled and approved;
- obtains a per-source advisory lock;
- creates a collection run;
- invokes the collector with current state;
- persists pages and cursor safely;
- records metrics and sanitized errors.

### `process_pending(limit)`

- claims pending raw items;
- normalizes, classifies, extracts, scores, deduplicates;
- creates or attaches to a lead;
- evaluates notification eligibility.

### Manual failed-processing maintenance

- inspect total failed count and a bounded list of internal UUIDs only;
- reset only failed rows selected by one explicit UUID or a limit of 1–1,000;
- claim reset candidates with row locks and skip busy records;
- clear the failure category, preserve evidence, and process only on a separate invocation;
- never print post bodies, external identifiers, contact details, or arbitrary errors.

### `send_immediate_alerts(limit)`

- claims eligible unsent leads;
- reserves idempotent notification records;
- formats and sends messages;
- records provider message IDs and outcomes.

### `send_daily_digest(local_date)`

- computes the UTC boundaries for the configured timezone;
- selects eligible new leads not already included in that period’s digest;
- creates an idempotent digest envelope and membership records;
- sends chunks in deterministic order.

### `apply_feedback(lead_id, actor, value)`

- authorizes the actor;
- stores feedback;
- updates lead status when appropriate;
- never mutates historical score reasons.

## 10. Scheduling

Suggested defaults:

- high-value authorized feeds: every 5–10 minutes;
- web search: every 30–60 minutes, subject to provider quota;
- processing and immediate delivery: after each collection run and every 5 minutes as a safety net;
- daily digest: configurable, default 09:00 Europe/Rome (explicit execution in Milestone 3);
- retention cleanup: once daily;
- source health summary: once daily or via `/status`.

Use PostgreSQL advisory locks or a lock table so duplicate application processes cannot run the same scheduled job simultaneously.

Scheduler recovery rules:

- startup reconciles unfinished collection runs and notifications left in `running` or `sending` states;
- every job defines a misfire policy instead of assuming the process was continuously available;
- a missed collector run should normally execute once after recovery, not replay every missed interval;
- a missed daily digest may run within a configurable grace window and must not send twice for the same local date;
- backoff state and collector cursors live in PostgreSQL, not only in process memory;
- graceful shutdown stops accepting new jobs and gives in-flight database transactions a bounded time to finish.

## 11. Telegram boundary

Define an internal notifier interface:

```python
class LeadNotifier(Protocol):
    async def send_alert(self, recipient: str, lead: LeadView) -> SendResult: ...
    async def send_digest(self, recipient: str, digest: DigestView) -> SendResult: ...
```

Formatting receives immutable view models, not ORM entities. Callback data should carry a compact action and opaque lead identifier, then verify the callback actor against the allowlist.

Tests must use a fake notifier; unit tests must never call Telegram.

## 12. Configuration and secrets

Suggested precedence:

1. safe defaults in code;
2. version-controlled YAML for business rules;
3. environment variables for deployment-specific values and secrets;
4. CLI flags for explicit one-off commands.

Validate:

- no real collector enabled with missing credentials;
- allowed Telegram user list is non-empty outside tests;
- timezone exists;
- score bands do not overlap;
- retention values are positive;
- source key and collector key agree.

Use secret-aware settings representations so logs and exception messages redact sensitive fields.

## 13. Observability

Structured log events should include:

- event name;
- timestamp;
- run/job ID;
- source key;
- record/lead internal ID where relevant;
- status, duration, and counters;
- retryable/non-retryable category.

Never log full tokens, cookies, raw API responses, phone numbers, email addresses, or entire post bodies.

Minimum `/status` output:

- application version;
- database reachable/not reachable;
- last successful run and failure count per enabled source;
- pending/failed processing counts;
- pending/failed notification counts;
- paused state and next digest time.

Production must also make the following conditions visible through logs, `/status`, or host-level alerts:

- an enabled source has not succeeded within its configured freshness window;
- repeated authorization, quota, or policy failures automatically paused a source;
- pending/failed processing or notification counts exceed configured limits;
- the daily digest did not complete within its grace window;
- the most recent database backup is missing or too old;
- disk space or database volume usage crosses warning thresholds;
- the application or database health check remains unhealthy after restart attempts.

## 14. Testing strategy

### Unit tests

- text normalization and fingerprints;
- positive/negative/indirect intent classification;
- extracted grade, format, goals, and budget;
- every scoring feature and score clamping;
- duplicate and non-duplicate pairs;
- Telegram escaping and chunking;
- configuration validation.

### Integration tests

- migrations on an empty PostgreSQL database;
- collector page + cursor atomicity;
- idempotent raw insertion;
- end-to-end fixture → lead → fake notification;
- notification uniqueness under repeated/concurrent runs;
- daily digest timezone boundaries;
- feedback authorization and persistence;
- retention cleanup.

### Production verification

- deploy from a clean checkout using production documentation;
- reboot the host and verify automatic service startup;
- verify scheduler ownership prevents duplicate work after restart;
- inject a controlled high-scoring lead and receive exactly one immediate alert;
- generate one controlled daily digest at the configured local time;
- simulate a temporary source failure and verify later recovery without blocking other sources;
- create a PostgreSQL backup and restore it into an isolated database;
- complete a minimum 24-hour unattended smoke test.

### Contract tests

Each real collector should convert recorded, redacted provider fixtures into `CollectedItem` objects. Do not make live third-party requests in the default test suite.

## 15. Security checklist

- `.env` ignored; `.env.example` contains placeholders only.
- Telegram commands and callbacks use an explicit user-ID allowlist.
- HTTP clients set timeouts, redirect limits, user agent, and response-size caps.
- Imported files have size and format limits.
- HTML is parsed as data; no scripts are executed.
- URLs sent to Telegram use allowed schemes (`https`, optionally `http`).
- Database user is not a PostgreSQL superuser.
- Containers run as a non-root user when practical.
- Dependency versions are constrained and checked.
- No headless browser, account cookies, or personal Telegram session is introduced without a separate reviewed design.

## 16. Backup, restore, and operational runbook

The production design must document and test:

- backup mechanism, schedule, encryption, destination, retention, and ownership;
- a default backup frequency of at least once per day, refined after the actual acceptable data-loss window is chosen;
- periodic cleanup that cannot delete the newest valid backup;
- restoration into an isolated database before any production replacement;
- migration compatibility between application and database versions;
- pre-update backup and a rollback path for both application image and schema;
- emergency source disablement without redeploying the entire application;
- recovery steps for expired credentials, exhausted quotas, corrupted configuration, unavailable PostgreSQL, and a full disk;
- the expected recovery point objective (RPO) and recovery time objective (RTO) once a host/provider is selected.

A backup job is not considered complete until at least one restore test has succeeded. Restore tests should be repeated after material database or deployment changes.

## 17. Evolution path

Only after the MVP has real volume and measurements, consider:

- adding an optional LLM classifier for low-confidence items;
- per-source precision/recall metrics;
- a review queue or small web UI;
- multiple tutors and per-recipient matching profiles;
- Redis/queue workers;
- vector similarity for deduplication;
- automatic source discovery;
- suggested response drafts, still requiring explicit human sending.

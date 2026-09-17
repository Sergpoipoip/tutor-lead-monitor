# Tutor Lead Monitor — Product Specification

Status: implementation-ready draft  
Version: 0.2  
Last updated: 2026-09-17

## 1. Product summary

Tutor Lead Monitor discovers public or explicitly authorized posts in which a person is looking for a literature tutor, ranks the opportunities, removes duplicates, and delivers useful leads to one Telegram user.

The product is a personal lead radar, not a crawler of the entire internet. It must prefer official APIs, user-authorized feeds, saved-search notifications, and public web search. It must not bypass authentication, CAPTCHA, robots restrictions, rate limits, platform permissions, or other technical access controls.

## 2. Primary user

The initial user is a private literature tutor who:

- teaches literature in Russian;
- works online and may also accept location-specific requests;
- prepares school students for regular coursework, essays, OGE/EGE, olympiads, and VSOSH;
- wants high-quality leads quickly, without reading hundreds of advertisements from other tutors;
- needs a daily Telegram digest and immediate alerts for exceptional leads.

The architecture may later support other subjects and multiple recipients, but those are not MVP requirements.

## 3. Product goals

1. Collect new candidate posts from a curated set of legal and technically supported sources.
2. Distinguish people seeking a tutor from tutors advertising themselves.
3. Recognize literature-related intent even when the phrase “looking for a tutor” is absent.
4. Normalize heterogeneous source records into one lead model.
5. Merge cross-posted or repeated requests into one lead.
6. Rank leads from 0 to 100 with an explainable score.
7. Deliver high-priority leads quickly and all useful new leads in a daily digest.
8. Learn from simple Telegram feedback without requiring model training in the MVP.

## 4. Non-goals for the MVP

- Monitoring “all of Telegram” or “the whole internet”.
- Reading private chats, closed communities, or accounts without explicit access.
- Automating outreach or sending unsolicited messages to prospective clients.
- Circumventing platform access controls or maintaining anti-bot evasion code.
- Replacing a human decision about whether and how to contact a lead.
- Building a web dashboard, mobile application, multi-tenant SaaS, or billing system.
- Training a custom machine-learning model.
- Guaranteeing complete or real-time coverage of any third-party platform.

## 5. MVP scope

### 5.1 Required source types

The first release must support the following collector categories through a common interface:

1. `fixture` — deterministic local records for tests and demos.
2. `web_search` — results from a configured, permitted search provider.
3. `telegram_bot_updates` — messages or channel posts that the bot is authorized to receive.
4. `manual_import` — JSON/CSV or forwarded items, used both as a fallback and for controlled testing.
5. `avito_notifications` — saved-search notifications or another user-authorized notification bridge.

The database and interface must allow later collectors for VK, Avito Messenger, RSS/Atom, email, and selected websites without changing the downstream pipeline.

### 5.2 Required processing pipeline

Every candidate item passes through these stages:

1. ingestion;
2. raw persistence and idempotency check;
3. text cleanup and normalization;
4. intent and subject classification;
5. structured field extraction;
6. relevance scoring;
7. exact and fuzzy deduplication;
8. persistence of the normalized lead and score explanation;
9. notification decision;
10. Telegram delivery and feedback capture.

Failure in one item must not abort the entire collection run.

### 5.3 Classification labels

Required intent labels:

- `seeking_tutor`
- `offering_tutoring`
- `school_or_agency_ad`
- `teaching_job`
- `informational`
- `uncertain`

Required subject labels:

- `literature`
- `russian_and_literature`
- `russian_language`
- `other`
- `unknown`

The MVP must use deterministic rules and weighted features. The classifier API must allow an optional LLM or ML implementation later, but the product must run fully without one.

### 5.4 Extracted lead fields

Each normalized lead should contain, when available:

- source and original URL;
- source publication identifier;
- publication timestamp and collection timestamp;
- original text and normalized text;
- intent and subject;
- grade (`1`–`11`, if known);
- goals: school support, essay, OGE, EGE, olympiad, VSOSH, admissions, adult study, other;
- preferred format: online, offline, either, unknown;
- location text;
- urgency;
- budget text and optional normalized amount/range;
- contact availability flag, without unnecessary extraction of personal details;
- relevance score;
- score reasons;
- duplicate group identifier;
- review state and user feedback.

### 5.5 Initial scoring model

The score must be explainable and configurable. The following weights are initial defaults, not hard-coded business logic:

| Feature | Weight |
|---|---:|
| Explicitly seeking a tutor or teacher | +25 |
| Literature is the primary subject | +30 |
| Russian language + literature request | +15 |
| EGE or OGE preparation | +10 |
| Olympiad or VSOSH preparation | +12 |
| Online format is accepted | +8 |
| Grade is stated | +4 |
| Request is less than 12 hours old | +6 |
| Budget is stated | +3 |
| Direct response path exists in the source | +2 |
| Tutor advertising their own services | −70 |
| School, course, or agency advertisement | −55 |
| Teaching vacancy rather than private tutoring request | −35 |
| Request clearly closed or stale | −50 |

Clamp the final score to `0..100`.

Initial notification thresholds:

- `90..100`: immediate alert and include in digest;
- `55..89`: daily digest;
- `45..54`: persist for review, omit from normal digest by default;
- `0..44`: rejected/no notification.

All thresholds and weights must be configurable through version-controlled YAML or typed settings, not only environment variables.

### 5.6 Deduplication

Deduplication must occur at two levels:

1. **Exact/idempotent:** same source + external ID, canonical URL, or stable source fingerprint.
2. **Near duplicate:** normalized text similarity, overlapping meaningful tokens, compatible publication times, and matching extracted attributes.

The MVP must preserve every occurrence and its source URL while selecting one canonical lead. It must never silently delete the evidence that two sources carried the same request.

Recommended initial rules:

- normalize case, whitespace, URLs, punctuation, and common boilerplate;
- build a SHA-256 exact fingerprint;
- compare recent candidates only, initially within 30 days;
- use a token-based similarity implementation with configurable threshold;
- do not merge records solely because the contact name or grade matches.

### 5.7 Telegram delivery

The Telegram bot is the delivery and feedback interface.

Required behavior:

- send immediate alerts for newly discovered leads above the urgent threshold;
- send one daily digest at a configurable local time and timezone;
- avoid notifying the same recipient twice for the same notification type and lead;
- split messages safely when Telegram limits are reached;
- escape user-generated text correctly;
- include the score, short reason, extracted fields, source, age, excerpt, and original link;
- include inline feedback buttons: `Interested`, `Not relevant`, `Duplicate`, and `Closed`;
- persist feedback and acknowledge the button action;
- support `/start`, `/help`, `/status`, `/digest`, `/pause`, and `/resume` for the authorized user.

The bot must reject administrative commands from unknown Telegram user IDs.

Example alert:

```text
🔥 94/100 — new literature tutor request

Literature · Grade 10 · Online
Goal: EGE preparation

“Looking for a literature tutor for my daughter...”

Source: VK
Published: 14 minutes ago
Why: explicit request + literature + EGE + online

[Open original]
[Interested] [Not relevant] [Duplicate] [Closed]
```

### 5.8 Daily digest

The digest must include:

- date and timezone;
- number of raw items collected;
- number filtered as advertisements/non-leads;
- number of new canonical leads;
- count by relevance band;
- compact list of all newly eligible leads sorted by score, then freshness;
- a deep link or original URL for each lead.

If there are no eligible leads, send at most one concise “no new leads” message for that day, controlled by configuration.

## 6. Source and compliance policy

Every source must have an explicit registry entry containing:

- owner/platform;
- access method;
- authorization basis;
- terms/robots review status;
- rate-limit policy;
- data fields retained;
- retention period;
- enabled/disabled state;
- operational notes.

Collectors must follow these rules:

1. Prefer official APIs and exports.
2. Collect only public content or content the user/bot is authorized to receive.
3. Do not bypass login, CAPTCHA, paywalls, rate limits, or bot protections.
4. Do not use residential proxies, fingerprint spoofing, or account farming.
5. Respect source deletion when it is observed and support local deletion.
6. Store the minimum data needed for evaluation and follow-up.
7. Never expose API tokens, session cookies, or credentials in logs.
8. Disable a collector after repeated authorization or policy errors until reviewed.

Platform rules can change. `docs/SOURCES.md` is operational guidance, not permanent legal approval. Revalidate a source before enabling it in production.

## 7. Data retention and privacy

Initial defaults:

- raw rejected items: 30 days;
- canonical leads and occurrences: 90 days;
- notification records and aggregated metrics: 180 days;
- user feedback: retained until manually deleted;
- secrets: never stored in the database or repository.

Retention values must be configurable. A maintenance job must delete expired records. Logs should identify records by internal IDs and source names; they should not repeat full post text or contact information.

## 8. Reliability and operational requirements

- Collection is idempotent.
- All times are stored in UTC and rendered in the configured user timezone.
- Each collector maintains its own cursor or high-water mark.
- Network calls use explicit timeouts, bounded retries, exponential backoff, and jitter.
- A failed collector does not block other collectors or the daily digest.
- Database migrations are handled by Alembic.
- Application startup validates configuration without printing secrets.
- Structured logs include run ID, collector, duration, counts, and error category.
- Health/status reporting exposes database connectivity and the most recent run per collector.
- The application runs locally with Docker Compose and can be deployed as an unattended, continuously running service on an always-on host.

## 9. Configuration

Secrets belong in environment variables or an external secret store:

- `DATABASE_URL`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_USER_IDS`
- provider-specific API credentials

Non-secret business configuration belongs in typed settings/YAML:

- timezone and digest time;
- collector intervals;
- source registry;
- query groups;
- scoring weights and thresholds;
- deduplication window and similarity threshold;
- retention durations;
- immediate-alert and digest rules.

The repository must contain `.env.example` with placeholders only.

## 10. Suggested implementation stack

- Python 3.12+
- PostgreSQL 16+
- SQLAlchemy 2.x and Alembic
- Pydantic Settings
- HTTPX
- aiogram or another actively maintained Telegram Bot API client
- APScheduler or a small database-backed scheduler for the MVP
- RapidFuzz or an equivalent library for text similarity
- pytest, pytest-asyncio, Ruff, and mypy/pyright
- Docker and Docker Compose

Avoid adding Redis, Celery, Kafka, Elasticsearch, or an LLM dependency until a measured need exists.

## 11. Milestones

### Milestone 1 — Foundation

- package, configuration, logging, database, migrations;
- domain models and collector interface;
- fixture collector;
- Docker Compose;
- unit tests and local run instructions.

### Milestone 2 — Pipeline

- normalization, rules classifier, extraction, scoring;
- exact and fuzzy deduplication;
- end-to-end fixture-to-database test;
- retention task.

### Milestone 3 — Telegram delivery

- authorized bot commands;
- immediate alert and daily digest;
- feedback buttons and notification idempotency;
- retry/error handling.

### Milestone 4 — First real sources

- one permitted web-search provider;
- authorized Telegram updates;
- manual import and/or notification import;
- documented source enablement process.

### Milestone 5 — VK and Avito

- VK collector only through currently supported official access;
- Avito saved-search notification bridge;
- optional Avito Messenger integration for the user’s own listing/account if officially available and authorized;
- monitoring and per-source quality metrics.

### Milestone 6 — Production deployment and autonomous operation

- deploy the application and PostgreSQL on an always-on host;
- configure production container restart policies so required services start automatically after a host reboot;
- run collectors, processing, immediate alerts, daily digests, and retention jobs without manual commands;
- store production secrets outside the repository and restrict access to them;
- configure service and database health checks, structured logs, log rotation, and disk-space monitoring;
- configure automatic PostgreSQL backups and document a tested restoration procedure;
- document deployment, application and database updates, rollback, emergency restart, and source-disable procedures;
- perform a host reboot test and a minimum 24-hour unattended-operation test before declaring production ready.

## 12. MVP acceptance criteria

The MVP is complete when all of the following are true:

1. `docker compose up` starts the application and PostgreSQL from a clean checkout after the user provides required secrets.
2. Database migrations run deterministically.
3. Reprocessing the same fixture creates no duplicate raw item, lead, or notification.
4. Test cases distinguish at minimum:
   - “Looking for a literature tutor, grade 10”;
   - “I am a literature tutor accepting students”;
   - “School seeks a literature teacher”;
   - “Can anyone recommend someone to help my daughter with literature?”
5. Scoring returns both a number and human-readable reasons.
6. Near-identical posts from two sources become one canonical lead with two occurrences.
7. A high-scoring fixture produces one immediate Telegram notification in an integration test using a fake Telegram client.
8. A second processing run produces no second immediate notification.
9. A daily digest contains all eligible unsent digest items in score order.
10. Unknown Telegram users cannot run administrative commands.
11. No real collector is enabled without an explicit source-registry entry and credentials/authorization checks.
12. Unit and integration tests pass locally; linting and type checking are documented.

## 13. Production acceptance criteria

Production deployment is complete when all of the following are true:

1. The application and PostgreSQL are deployed on an always-on host.
2. After a host reboot, all required services start automatically without a manual command.
3. Scheduled collection and processing resume automatically and do not require a daily application launch.
4. A controlled high-scoring test lead produces exactly one immediate Telegram alert.
5. A controlled set of eligible leads produces one daily digest at the configured local time.
6. Temporary source and network failures are retried according to policy and do not stop unrelated collectors or later scheduled runs.
7. `/status` reports database connectivity, scheduler state, the most recent collector runs, failures, and the next digest time.
8. Automatic PostgreSQL backups are created and at least one restoration test has succeeded.
9. Logs are retained and rotated without exposing secrets or unnecessary personal data, and low disk space is detectable.
10. Deployment, update, rollback, backup restoration, emergency restart, and source-disable instructions are documented.
11. The system completes at least 24 consecutive hours of unattended operation, including collection, processing, notifications, digest delivery, and retention jobs.

## 14. Open decisions

Resolve these before production, not before Milestone 1:

- deployment host and backup strategy;
- final web-search provider and its current pricing/terms;
- Telegram communities that will explicitly allow bot access;
- whether Avito notifications will arrive by email, manual forwarding, or an official account integration;
- target offline locations and travel radius;
- daily digest time and the score threshold for immediate alerts;
- whether optional LLM classification provides enough measured improvement to justify cost and privacy impact.

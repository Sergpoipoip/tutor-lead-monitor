# Tutor Lead Monitor — handoff

## Current update — 2026-09-19, Milestone 5A

This section supersedes the **historical 2026-09-18 snapshot below** wherever status,
next actions or implementation scope differ. AGENTS.md, PROJECT_SPEC.md,
ARCHITECTURE.md and SOURCES.md remain authoritative; milestone definitions are unchanged.

The owner explicitly prioritized a bounded **Milestone 5A** VK official wall collector
before 4B/4C. Reasons: poor/stale Yandex search yield; seven curated active VK communities;
a successful official API audit; privately retained written VK Support clarification
permitting the described classification, scoring, minimal storage and private Telegram
delivery. The owner confirmed the technical smoke-test/audit date as **2026-09-19**:
API 5.199, service token, 12 calls with no errors, 133 non-pinned posts sampled,
6 likely literature leads, all communities active within the preceding day. Platform
Rules revision 2026-08-19 was reviewed by the owner. These are owner-reported findings;
implementation and tests made no live VK calls. Support correspondence is not published.

The working tree implements seven independent `vk_api` sources, all disabled/pending
with null policy reviewer/date. All use VK_ACCESS_TOKEN, validated only at collector
construction, excluded from settings repr/serialization and never logged. The token
was reported by the owner to be in ignored .env; its value was not displayed. Yandex
remains disabled/pending and unchanged. Neither approval nor live collection is implied
by the earlier audit or this implementation.

Implementation: vk_api.py owns one bounded official POST/form wall.get request;
collectors/vk.py owns one community's versioned post-ID cursor. First runs request
count=initial_posts (default 20, range 1–20), including pins within that budget. Later
runs request incremental_posts (default 100, range 1–100) and emit newer IDs only. Empty initial
walls save zero. Full windows without an ordinary ID at/below the old mark stop with
cursor_overflow before persistence/checkpoint advancement. No offset cursor, automatic
retry, catch-up pagination, destination/profile/comment/attachment request, scheduling
or automatic execution is added. IDs and owner checks preserve source identity; minimal
repost provenance/text reaches the existing processing pipeline without rewriting it.
Existing database uniqueness, per-source state and atomic checkpoints are reused;
no database migration was added. Synthetic contracts and PostgreSQL tests cover the
new boundary, errors, replay, overflow, failed persistence, isolation and CLI gates.

**Still unimplemented:** 4B authorized Telegram source collection; 4C manual/email/
notification import; Avito; automatic scheduling; deployment/backups/unattended operation.
5A is a local increment name, not completion of all of Milestone 5 or a renumbering of
PROJECT_SPEC. The existing Telegram bot still handles delivery/owner commands/feedback.

**Next owner action:** review the new disabled configuration and verification results,
then explicitly authorize only vk_ishchu_repetitora (218494134) for the first application
smoke test with initial_posts: 5. Follow README's exact procedure and SOURCES §18.
Complete policy reviewer/aware timestamp/notes/expiry, keep the other six VK sources
and Yandex disabled, validate configuration, prepare local PostgreSQL/migrations and
sync sources. Only the explicit collect command contacts VK. With no cursor it
requests count=5, retrieves at most five posts including any pin, and retains at most
five. A separately authorized subsequent run uses incremental_posts (default 100). Review evidence
privately before processing/delivery. Do not reset any existing cursor to create a
"first" run. Restoring initial_posts to 20 does not backfill older excluded posts.
Overflow recovery requires separate review; never advance a cursor to hide a gap.
Failed first-run persistence with observed items and no cursor also blocks retries
before HTTP, preventing failed evidence from being displaced by newer arrivals.
Preserve evidence and audit history for recovery. Repost provenance is retained;
current deduplication still matches occurrence URLs/text, not original IDs in metadata.

Git baseline for this work: main at 4cb4cc14e0b9763e4b9a3f6c5030d88a33af21b8
(docs: add project handoff). This task does not commit, push or merge; changes remain
in the working tree for owner review. Do not infer a current HEAD from this snapshot.

The earlier implementation reported 608 passing tests; that is historical evidence,
not verification of this hardening pass. Independent review reproduced first-run
oversampling, a combined-text separator bound defect, and late parser success past
the operation deadline. Those defects are fixed; failed-bootstrap retries now fail
closed. Fresh verification results follow.
No live application VK compatibility has been established: the owner-run API audit
is distinct from the still-pending first collector smoke test. During this review
.env was not read, including by Settings, tests, Alembic or Compose.

**Fresh independent verification, 2026-09-19:** 647 passed (528 unit, 119 PostgreSQL
integration), no skips. The first targeted regressions reproduced three failures
before the fixes (request count, combined text bound, parser deadline). The full
suite then passed against a newly created disposable PostgreSQL 17 container using
a cached image with --pull=never and a loopback-only published port. It was removed
after verification; no owner database was used. Ruff lint and formatting (76 files),
strict mypy (70 files), offline lock/sync/build, Compose, actionlint and configuration
validation passed. Alembic current was b35778628004 (head); drift, downgrade base /
upgrade head / drift, and offline SQL generation passed. Fixture preview, healthcheck,
failed-record inspection and retention dry run also passed on an empty disposable DB.
The temporary CLI verification runner initially used a nonexistent package entrypoint;
correcting it to tutor_lead_monitor.cli made those checks pass. This was a verification
harness error, not an application defect. CI and live provider APIs were not queried.

Exact commands used (from the repository root):

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
uv --offline lock --check
uv --offline sync --locked
uv --offline build
.venv/bin/python /private/tmp/tlm-vk-review-safe.py pytest tests/unit tests/integration -q
.venv/bin/python /private/tmp/tlm-vk-review-safe.py alembic current
.venv/bin/python /private/tmp/tlm-vk-review-safe.py alembic check
.venv/bin/python /private/tmp/tlm-vk-review-safe.py alembic downgrade base
.venv/bin/python /private/tmp/tlm-vk-review-safe.py alembic upgrade head
.venv/bin/python /private/tmp/tlm-vk-review-safe.py alembic check
.venv/bin/python /private/tmp/tlm-vk-review-safe.py alembic upgrade head --sql > /private/tmp/tlm-vk-review-migrations.sql
.venv/bin/python /private/tmp/tlm-vk-review-safe.py tutor_lead_monitor.cli check-config
.venv/bin/python /private/tmp/tlm-vk-review-safe.py tutor_lead_monitor.cli healthcheck
.venv/bin/python /private/tmp/tlm-vk-review-safe.py tutor_lead_monitor.cli fixture
.venv/bin/python /private/tmp/tlm-vk-review-safe.py tutor_lead_monitor.cli failed --limit 20
.venv/bin/python /private/tmp/tlm-vk-review-safe.py tutor_lead_monitor.cli retention --dry-run
env -i PATH="$PATH" HOME="$HOME" POSTGRES_ADMIN_PASSWORD=synthetic-review-only POSTGRES_APP_PASSWORD=synthetic-review-only docker compose --env-file /dev/null -f docker-compose.yml config --quiet
docker run --pull=never --network=none --rm -i rhysd/actionlint:latest - < .github/workflows/ci.yml
git diff --check
```

The temporary safe wrapper disables Settings' default dotenv, removes provider secret
environment variables and rejects any attempt to open the repository .env via a Python
audit hook before running the named module. The temporary container runner supplies
DATABASE_URL/TEST_DATABASE_URL for its newly created disposable `_test` database and
REQUIRE_INTEGRATION_TESTS=1. These temporary scripts are review artifacts, not project
entrypoints; do not run database checks on an owner database or assume these artifacts
exist in another environment. Tests now independently disable default dotenv loading
and clear provider credentials, while explicit synthetic dotenv fixtures still work.

All seven key/ID/screen-name/display-name tuples were checked against the approved list;
all seven VK sources and Yandex remain disabled/pending with null reviewer/date. A
signature scan of tracked and nonignored untracked files found no credential candidates;
.env was excluded without opening it. Build archive filename checks found no .env.
No files were committed, no real VK post text was printed, and no live provider call
was made. Signature scanning does not establish whether arbitrary strings are valid
credentials; no comparison with the private .env was performed.

---

## 1. Date, purpose and authority

Snapshot: **2026-09-18**, for a fresh Codex chat resuming this repository.
`docs/HANDOFF.md` is an operational snapshot, not an authoritative product
specification. [AGENTS.md](../AGENTS.md), [PROJECT_SPEC.md](PROJECT_SPEC.md),
[ARCHITECTURE.md](ARCHITECTURE.md), and [SOURCES.md](SOURCES.md) take precedence.
The three authoritative documents declare version **0.2**: product scope and
milestones, technical contracts, and source access/enablement policy respectively.
[README.md](../README.md) supplies local operating instructions. Do not infer
implemented functionality from the architecture's future target layout.

**Secrets, tokens, passwords and complete environment-variable values must never
appear in this file.** Variable names alone are sufficient. Do not read or copy
`.env` into a chat. The next agent must verify Git state instead of blindly
trusting this snapshot; resolve discrepancies against the authoritative documents
and actual implementation before making changes.

## 2. Repository and working tree

- Repository: <https://github.com/Sergpoipoip/tutor-lead-monitor>.
- Branch: `main`; local `origin/main` also points to this HEAD (no fetch performed).
- HEAD: `c5cc93e189ce1341ed39bf9d681419806736c1fe`.
- Subject: `fix: harden Yandex source enablement`.
- Initial `git status --porcelain=v1 --untracked-files=all` was empty: no
  uncommitted or untracked files and no unrelated changes.
- This task creates only `docs/HANDOFF.md`; it is untracked until committed.
  Ignored local files are not represented by that status; their contents were
  not inspected. `git check-ignore .env` confirms the ignore rule.

## 3. Product and current architecture

Discover public or explicitly authorized requests for literature tutoring,
distinguish them from advertisements/vacancies, rank and deduplicate them, and
deliver useful leads to one authorized Telegram owner. No automated outreach.

Python 3.12+, PostgreSQL (Compose/CI use 17), SQLAlchemy 2, Alembic, Pydantic
Settings/YAML, HTTPX and python-telegram-bot; dependencies are locked in `uv.lock`.
Code is under `src/tutor_lead_monitor/`: independent `collectors/` and `search/`
feed immutable raw evidence; `processing/` contains deterministic rules;
`application/` orchestrates `db/` transactions and `notifications/` delivery.
The collector factory currently supports only fixtures and Yandex web search.
Compose has PostgreSQL, a one-shot migration service and the application.
`serve` checks health only; the owner bot and business commands run explicitly.

Current settings in `config/`:

| Setting | Value |
|---|---|
| Local digest time / display | 09:00 Europe/Rome / `время Рима`; no automatic execution |
| Empty digest | Disabled |
| Score thresholds | Review 45, digest 55, immediate 90; immediate leads also enter a digest |
| Rules / scoring | `rules-v2`; fresh window 12 hours, stale after 30 days |
| Text deduplication | 30-day window, similarity threshold 90 |
| Retention | Rejected raw 30 days; leads/occurrences 90; notifications/run metrics 180 |
| Yandex budget | Default 3 queries × 10 results; maximum 10 queries × 20 results |

Times are UTC internally. YAML holds non-secret business configuration; protected
settings are lazy and redacted. Current migration chain:
`0001` → `7d8905a1ece6` → `b35778628004` (head). Milestone 4A added no migration.

## 4. Completed milestones and evidence

| Milestone | Repository evidence and delivered behavior |
|---|---|
| 1 — Foundation | `b6f1e0a`, `10544c6`: package, typed settings/operations, logging, PostgreSQL models/migrations, fixtures, Compose and tests; alignment with v0.2. |
| 2 — Processing and hardening | `84be331`, `0e22232`: normalization, classification/extraction/scoring, exact/fuzzy deduplication, retention, richer duplicate promotion, earlier rejected evidence recovery, bounded failed-record inspection/reset. Russian classification corpus covers ambiguous and negated requests. |
| 3 — Telegram and reliability | `6e1b636`, `ba0f1c7`: owner commands, immediate alerts, digest, feedback, persistent pauses/retry state; backlog digest eligibility and notification-retention fixes. |
| 3 — Complete Russian localization | `2d15034`: all newly generated Telegram UI text, reasons, dates, status and buttons are Russian; source excerpts and frozen historical snapshots stay unchanged. |
| 4A — Search and enablement hardening | `c4b596c`, `c5cc93e`: provider-neutral search boundary, Yandex REST/XML adapter, bounded requests/parsing, mock contracts, CLI integration; mandatory real-source review metadata, safe credential boundaries, operational ownership and trailing-dot URL identity. |

These commits are in `main` history. Implemented behavior is backed by code and
tests, not a claim of unattended production readiness.

## 5. Yandex state and boundaries

Milestone 4A and subsequent hardening are **merged into `main`**. Both committed
source registries retain `enabled: false`, `policy_status: pending`, and null
`policy.reviewer` / `policy.reviewed_at`. Owner is `Project owner`, meaning the
operational person/project responsible for review and disablement; provider is
identified separately as `yandex` and in the Russian display name.

The repository's recorded state is **no real Yandex request has been made**:
README explicitly records no live API call, and SOURCES §17 records a technical
review only. This handoff made no Yandex request. Git cannot independently prove
external account activity; confirm with the owner before treating a run as first.

Enabled real sources require approved policy, a nonblank reviewer (normalized
whitespace, at most 160 printable characters), an aware review timestamp and
unexpired authorization when expiry is specified. Validation, collector factory
and execution enforce the gates; fixtures need no review metadata. Stored disabled
state and active pauses also block collection.

Credentials remain lazy `SecretStr` values excluded from settings serialization.
Boundary checks reject missing/empty values, known placeholders, whitespace and
controls. API keys permit printable ASCII up to a local 4,096-character header
cap; folder IDs permit printable JSON strings up to 50 characters. Do not restore
undocumented exact-format regexes or echo values in errors.

Search uses the official synchronous REST endpoint, XML via bounded Base64,
Russian search/localization, region 225, strict family mode, time-descending flat
groups, page zero and `PERIOD_2_WEEKS`. Every request disables provider
service-improvement logging. Limits: 45-second total timeout, 1 MiB response,
512 KiB XML, 10,000 elements; no DTD/entities, redirects or environment proxies.
No automatic retries: a valid Retry-After can persist a pause up to 300 seconds.
Only snippets/URLs are evidence; destinations are never fetched. Unknown
publication dates stay unknown. SHA-256 canonical-URL IDs make replay idempotent;
meaningful URL parameters/encoding survive and one trailing DNS dot is removed.

## 6. Invariants to preserve

- Collection, classification, deduplication and Telegram delivery remain separate.
  Page persistence/cursor advancement is atomic; database uniqueness and advisory
  locks protect repeat/concurrent work. Logs contain safe categories/IDs, not posts.
- Preserve raw evidence and occurrences. Canonical promotion ranks score, extracted
  completeness, then UUID; update classification/extraction/reasons/versions
  together, never lowering score because a poorer duplicate arrived.
- Digest eligibility uses recipient-specific prior digest membership and expiry
  markers, not lead creation day. Late arrivals/promotions enter a later digest;
  an immediate alert does not consume digest eligibility. Order: score, freshness,
  UUID. Statistics cover the requested local calendar day with DST-aware bounds.
- Frozen message chunks/membership resume unchanged. Ambiguous Telegram sends
  require manual review; never blindly resend. New owner-facing text is Russian;
  preserve original excerpts, stable internal IDs and historical snapshots.
- Feedback and required lead/evidence remain until manual deletion, but never
  extend notification payload retention. Payload-free lead markers last while
  their lead remains; digest-period markers last until explicit recipient erasure.
- Official/permitted access only: no scraping fallback, personal-session collection,
  CAPTCHA bypass, browser automation, Redis/Celery/Kafka/Elasticsearch or LLM
  dependency. Tests guard against real HTTP/Telegram calls.

## 7. Verification baseline

Fresh collection at this HEAD found **498 tests: 394 unit, 104 PostgreSQL
integration**. These are collected counts, not a fresh passing-suite claim.
Commands used `.venv/bin/pytest --collect-only -q -p no:cacheprovider` separately
on both directories with bytecode writes disabled. No full suite was rerun for
this documentation-only task.

CI status is **unverified**, not asserted green: the read-only GitHub Actions
lookup for this exact HEAD failed with HTTP 401. Check the
[repository Actions page](https://github.com/Sergpoipoip/tutor-lead-monitor/actions)
after restoring GitHub access. The checked-in workflow runs locked installation,
Ruff lint/format, strict mypy, unit/PostgreSQL integration tests, migration round
trip and Alembic drift checks. Workflow presence alone is not a successful run.
No committed full verification report was found; do not import prior chat totals
or pass claims as evidence. This task checks documentation whitespace only.

## 8. Deliberately absent and next development target

Not implemented: automatic scheduling, production deployment/backups/unattended
operation, real Telegram source collectors, manual/email/notification imports,
Avito/VK integrations and other later Milestone 4 sources. The existing Telegram
bot handles delivery/owner commands/feedback, not source ingestion. Source interval,
freshness and failure-threshold configuration does not implement an autonomous job.

After a successful owner-reviewed Yandex smoke test, the next development target
is **Milestone 4B — authorized Telegram Bot API source collection**, through the
existing independent collector boundary and explicitly permitted chats/channels.
This is the next item in PROJECT_SPEC §11's remaining Milestone 4 sequence;
the authoritative documents group remaining sources as 4B/4C/5 rather than define
a detailed 4B acceptance contract. Agree that bounded contract before coding.
Manual/notification imports follow; scheduling/deployment remain Milestone 6.
This handoff does not authorize implementation or source enablement.

## 9. Exact next owner action: bounded live smoke test

Follow [SOURCES §17](SOURCES.md#17-milestone-4a--yandex-search-api-review-and-owner-enablement):

1. Review current Yandex Search API access, account availability, pricing, quotas,
   applicable terms and permission to retain snippets. The dated review is not approval.
2. Configure billing and a spending limit/budget in the provider console.
3. Create credentials with the documented `search-api.webSearch.user` folder role.
   Store credentials **only in ignored `.env`** for this local workflow, under
   `YANDEX_SEARCH_API_KEY` and `YANDEX_SEARCH_FOLDER_ID`. Never paste values here,
   into commands, YAML, Git or chat. Check for stale overriding shell variables
   without printing their values.
4. Complete policy reviewer, timezone-aware review date, authorization/terms notes
   and authorization expiry when applicable. Only after review set approved/enabled.
5. In that source's options select `query_ids: [seek_tutor]`,
   `max_requests_per_run: 1`, `results_per_query: 5`.
6. With local PostgreSQL available, run the following explicitly. Only the last
   command calls Yandex and may incur cost; run it once for this smoke test.

```sh
uv run tutor-lead-monitor check-config
uv run tutor-lead-monitor migrate
uv run tutor-lead-monitor sync-sources
uv run tutor-lead-monitor collect --source yandex_web_search
```

7. Inspect sanitized run counts and stored snippets/URLs privately before proceeding
   to processing or delivery. Confirm the account accepts the date-filter contract.
   Do not use `pipeline` as the smoke test: it includes all enabled collectors and
   processing. Any replay is another potentially paid request.

Emergency disable: set the source disabled, synchronize it, stop in-flight
collection and revoke a compromised key. Preserve evidence/history. No database
rollback is needed for 4A.

## 10. Resume commands and limitations

Start at repository root; these examples contain no environment-variable values:

```sh
git status --short --branch
git log -10 --oneline
git show -s --format='%H %s' HEAD
uv lock --check
uv sync --locked
uv run tutor-lead-monitor check-config
uv run tutor-lead-monitor fixture
uv run tutor-lead-monitor failed --limit 20
uv run tutor-lead-monitor retention --dry-run
uv run pytest tests/unit
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
git diff --check
```

Database commands require a configured/migrated local database; follow README.
For full verification, point `TEST_DATABASE_URL` at a disposable database ending
in `_test`, then run `uv run pytest tests/unit tests/integration`. Against that
disposable database only, with `DATABASE_URL` selected securely, run Alembic
`upgrade head`, `downgrade base`, `upgrade head`, then `check`. Never downgrade
owner data. Also validate Compose and actionlint as documented in README.

Known limits: heuristic classification/lexical matching need real yield review;
snippets can be incomplete/stale and lack publication dates. Repeated search URLs
preserve first raw evidence rather than update it. The live date-filter contract
(`period` versus the conceptual guide's `resultsWithin`) remains untested. No
automated freshness/quota monitoring or failure-threshold pausing exists yet.
Telegram cannot guarantee exactly-once delivery after an ambiguous network result.
Outstanding decisions include authorized communities, import method, offline
locations, production host/backup/restore strategy and later autonomous operations.

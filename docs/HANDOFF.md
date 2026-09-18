# Tutor Lead Monitor — handoff

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

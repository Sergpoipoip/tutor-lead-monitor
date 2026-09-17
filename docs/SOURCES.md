# Tutor Lead Monitor — Source Strategy and Registry Guide

Status: implementation-ready draft  
Version: 0.2  
Last updated: 2026-09-17

## 1. Purpose

This document defines which source types Tutor Lead Monitor may use, how each should be integrated, and what must be checked before production enablement.

The source landscape and platform rules change. Treat every entry as a proposed integration strategy, not permanent permission. Re-check the official documentation and applicable terms immediately before implementation and periodically afterward.

## 2. Source tiers

| Tier | Meaning | Examples | MVP stance |
|---|---|---|---|
| A | Official API/export or content explicitly delivered to the user | Telegram Bot API updates, Avito messages for own account, RSS, email notifications | Preferred |
| B | Public web search/index result with provider authorization | Search API results linking to public posts | Allowed after provider review |
| C | Public HTML pages with clear permission and respectful rate limits | selected forums or public boards | Per-source review required |
| D | Authenticated user-session automation or fragile scraping | user-account Telegram scraping, social network feeds behind login | Out of MVP |
| E | Access-control evasion | CAPTCHA bypass, proxy rotation, stolen/shared cookies | Prohibited |

## 3. Initial implementation order

1. Fixture and manual-import collectors.
2. Telegram delivery bot and authorized Telegram updates.
3. One currently supported web-search provider.
4. Email/notification import for saved searches, including Avito notifications.
5. VK official API integration if current access and permissions support the chosen communities.
6. Avito Messenger/API integration for the user’s own account/listing if access is granted.
7. Selected RSS feeds and public websites after individual review.

This order validates the complete product pipeline before spending time on difficult platform integrations.

## 4. Source registry schema

Use `config/sources.yml` for non-secret source configuration. Secrets are referenced by environment-variable name, never stored in YAML.

Example:

```yaml
sources:
  - key: telegram_authorized_channel_example
    kind: telegram_bot_updates
    display_name: Authorized Telegram channel
    enabled: false
    policy_status: pending
    access_method: authorized_bot
    collector_interval_seconds: 300
    credential_env: TELEGRAM_BOT_TOKEN
    operations:
      freshness_sla_seconds: 900
      pause_after_consecutive_failures: 5
      quota_policy: "provider-specific"
      authorization_expires_at: null
    config:
      allowed_chat_ids: []
    policy:
      authorization_basis: "Bot must be added to the chat/channel with sufficient rights"
      reviewed_at: null
      reviewer: null
      notes: "Do not assume access to unrelated public chats"
    retention_days: 90
```

Required fields:

- `key`
- `kind`
- `display_name`
- `enabled`
- `policy_status`
- `access_method`
- schedule or collection interval
- freshness service-level target for successful collection
- automatic pause threshold and quota policy
- non-secret collector configuration
- authorization/policy notes
- authorization expiry or review date when applicable
- retention period

An enabled source with `policy_status != approved` must fail configuration validation.

## 5. Query strategy

Use query groups rather than one enormous Boolean query. Search-provider syntax differs, so collectors should render provider-specific syntax from provider-neutral concepts.

### 5.1 High-intent seeking phrases

```text
ищу репетитора
ищем репетитора
нужен репетитор
нужна репетитор
нужен преподаватель
нужна преподавательница
посоветуйте репетитора
порекомендуйте репетитора
подскажите репетитора
кто может позаниматься
кто готовит к ЕГЭ
кто готовит к олимпиаде
```

### 5.2 Indirect need phrases

```text
нужно подтянуть литературу
помочь с литературой
проблемы с литературой
просела литература
помочь с сочинением
научить писать сочинение
разбирать произведения
подготовить к экзамену по литературе
подготовить к олимпиаде по литературе
```

### 5.3 Subject and goal terms

```text
литература
русский и литература
ЕГЭ по литературе
ОГЭ по литературе
олимпиада по литературе
ВСОШ по литературе
сочинение
итоговое сочинение
анализ произведения
литературный анализ
```

### 5.4 Negative/offering phrases

These are classification features, not always search exclusions; excluding them at retrieval time can hide replies or quoted context.

```text
я репетитор
набираю учеников
набор учеников
провожу занятия
мои услуги
авторский курс
образовательный центр
вакансия преподавателя
требуется учитель
```

### 5.5 Example provider-neutral query groups

```yaml
query_groups:
  explicit_literature:
    seek_any:
      - "ищу репетитора"
      - "нужен репетитор"
      - "посоветуйте репетитора"
    subject_any:
      - "литература"
      - "русский и литература"

  exams:
    seek_any:
      - "ищу"
      - "нужен"
      - "подготовить"
    subject_any:
      - "ЕГЭ по литературе"
      - "ОГЭ по литературе"
      - "итоговое сочинение"

  olympiad:
    seek_any:
      - "ищу"
      - "нужен"
      - "подготовить"
    subject_any:
      - "олимпиада по литературе"
      - "ВСОШ по литературе"

  indirect_help:
    phrases_any:
      - "подтянуть литературу"
      - "помочь с литературой"
      - "помочь с сочинением"
      - "разбирать произведения"
```

## 6. Telegram

### Recommended access methods

1. **Bot updates from authorized chats/channels.** Add the bot only where the owner/admin permits it and configure exact allowed chat IDs.
2. **Messages deliberately forwarded to the bot.** Treat forwarded messages as a manual/authorized import and preserve the original link when available.
3. **Delivery to the private owner chat.** The bot sends alerts and digests only to allowlisted user IDs.

The Bot API delivers updates to the bot; it is not a global Telegram search API. Telegram’s official API documentation describes incoming `Update` objects, including messages and channel posts known to the bot, and the authorization token model: <https://core.telegram.org/bots/api>.

### Candidate communities

Public aggregators previously identified as discovery seeds include:

- `@poisk_uchenikov` (“Ищу репетитора / Заявки для репетитора”);
- large chats or channels focused on “ищу репетитора” requests;
- local parent and school communities that explicitly permit a bot.

These names are leads for manual review, not pre-approved sources. Before adding one:

- confirm it still exists and publishes original or linked requests;
- contact an administrator when bot membership or automated processing is required;
- confirm whether reposting/aggregation is permitted;
- prefer an original-post link over copying personal/contact information;
- add the exact chat ID and authorization basis to the source registry.

### Prohibited in the MVP

- logging into a personal Telegram account with MTProto to scrape unrelated chats;
- discovering or joining groups automatically;
- collecting member lists;
- attempting global message search;
- copying entire chat histories.

## 7. VK

### Proposed access method

Use the official VK API with an application/token and only methods and communities permitted by the current API rules. A collector should target a curated list of public communities rather than attempt indiscriminate global scraping.

Possible source families:

- parent communities by city/district;
- school and class communities where public requests are allowed;
- EGE/OGE discussion communities;
- olympiad/VSOSH communities;
- local recommendation groups;
- tutor-request aggregator communities.

### Enablement checklist

- verify current official method availability and required token type;
- verify the application and account have the required permissions;
- store community IDs, not only mutable screen names;
- document per-method rate limits and pagination behavior;
- test edited/deleted posts and pinned/reposted content;
- keep API version explicit;
- retain original VK URL and publication ID;
- disable the source on repeated authorization errors.

### Collection behavior

- Fetch new posts from a curated allowlist using per-community cursors.
- Apply broad candidate terms, then perform full local classification.
- Preserve repost metadata so the original and repost can deduplicate.
- Never use cookies or browser automation as a silent fallback when the API denies access.

Exact VK methods must be confirmed against the current official developer documentation at implementation time: <https://dev.vk.com/>.

## 8. Avito

Avito is valuable but should not be implemented as an anonymous high-frequency page scraper.

### Supported strategy A — saved-search notifications

1. The user creates saved searches in the Avito interface.
2. Avito sends notifications through a user-enabled channel such as email or app notifications.
3. A permitted bridge passes notification content or links into Tutor Lead Monitor.
4. The collector extracts only the data present in the notification and follows links only when access is permitted.

Suggested saved searches:

```text
ищу репетитора литература
нужен репетитор литература
ищу преподавателя литературы
ЕГЭ литература репетитор
ОГЭ литература репетитор
олимпиада литература репетитор
ВСОШ литература
помощь с сочинением
подготовка по литературе
```

Important product assumption: Avito mainly contains tutor service offers, so many results will be competitors rather than parents seeking a tutor. Measure lead yield before investing heavily.

### Supported strategy B — messages for the user’s own listing

If the user has an Avito tutor listing and Avito grants official API access, integrate messages addressed to that account/listing. This is a high-value source because it captures direct inbound inquiries.

Requirements:

- use only official credentials and scopes granted to the user’s account;
- process only conversations the account is authorized to read;
- never automate replies in the MVP;
- store minimal excerpts and a conversation deep link when available;
- treat API access loss as a disabled source, not a reason to scrape the web UI.

Confirm current products, scopes, and access conditions in Avito’s official developer portal before implementation: <https://developers.avito.ru/>.

### Optional strategy C — web-search discovery

A configured web-search provider may return public Avito URLs for narrowly targeted queries such as `site:avito.ru "ищу репетитора" литература`. These results may be stale or dominated by service offers. They must pass normal freshness, classification, and deduplication rules.

### Prohibited

- CAPTCHA-solving services;
- rotating proxies or browser fingerprint evasion;
- automated use of a personal logged-in browser session;
- reverse-engineered private endpoints;
- scraping search pages after the site signals blocking or disallowance.

## 9. Web search

Use a provider abstraction:

```python
class SearchProvider(Protocol):
    async def search(self, query: str, *, cursor: str | None) -> SearchPage: ...
```

Select one provider immediately before Milestone 4 based on:

- current official availability in the target region;
- API terms that permit the intended use;
- Russian-language result quality;
- freshness/date filtering;
- quotas and predictable cost;
- stable result URLs and publication metadata;
- prohibition or allowance of result storage.

Yandex is a natural candidate for Russian-language discovery, but its current product name, endpoint, pricing, and storage conditions must be revalidated before implementation. Do not hard-code a historical Yandex Search API contract into the domain layer.

Example searches:

```text
"ищу репетитора" "литература"
"нужен репетитор" "литература"
"посоветуйте репетитора" "литература"
"помочь с литературой" репетитор
"подготовка к ЕГЭ по литературе" "ищу"
"ВСОШ по литературе" репетитор
site:vk.com "ищу репетитора" "литература"
site:t.me "ищу репетитора" "литература"
site:avito.ru "ищу репетитора" "литература"
```

Search results are candidates, not necessarily source content. Prefer sending the original public URL. Do not automatically crawl a returned URL unless that destination has its own approved source policy.

## 10. RSS, forums, and public boards

RSS/Atom is preferred when offered. For a website without a feed, approve an HTML collector only after checking:

- robots instructions and terms;
- stable public access without login;
- request frequency and caching;
- pagination and stable item IDs;
- whether personal/contact details are necessary to retain;
- deletion/edit behavior;
- a named owner in the source registry.

HTML collectors must use conditional requests (`ETag`, `Last-Modified`) when supported, conservative intervals, bounded response sizes, and parser contract tests based on saved/redacted HTML fixtures.

Potential categories to research manually:

- parent recommendation forums;
- city/district discussion boards;
- public school-parent boards;
- education and exam forums;
- marketplaces explicitly offering a public feed/API for tutoring requests.

Do not add `Profi.ru`, `Repetit.ru`, or similar marketplaces by assumption. First verify whether they expose tutor requests publicly and whether automated monitoring is allowed. Many marketplaces intentionally reveal requests only inside authenticated tutor workflows.

## 11. Facebook, Instagram, Odnoklassniki, and WhatsApp

These are not MVP sources.

### Facebook / Instagram

Only reconsider if a current official Meta API exposes the specific content with the necessary permissions, or if public pages are surfaced by an approved web-search provider. Do not build a global-post-search assumption into the architecture.

### Odnoklassniki

Only reconsider through documented official APIs and specific authorized groups. Do not assume global search/read access.

### WhatsApp

Do not attempt to monitor arbitrary groups. A future integration may process inbound messages to a business number explicitly controlled by the user, subject to the current official platform rules.

## 12. Source quality metrics

Track per source and query group:

- items collected;
- unique canonical leads;
- percentage classified as seeking a tutor;
- percentage relevant to literature;
- notified leads;
- `Interested` rate;
- `Not relevant` rate;
- duplicates;
- median age at discovery;
- API/network failures;
- estimated monetary cost.

After at least 30–50 reviewed candidates per source, use these metrics to increase/decrease polling frequency or disable low-value queries. The goal is useful leads, not maximum collection volume.

## 13. Autonomous production behavior

Every enabled real source must remain safe and diagnosable when the application runs unattended.

### 13.1 Persistent state

- Store cursors, high-water marks, backoff state, consecutive failure counts, and last successful run in PostgreSQL.
- Advance a cursor only after the corresponding items are persisted successfully.
- A restart must not force a full historical recrawl or skip items collected before the cursor commit.
- Source configuration and query versions should be visible in collection-run metadata.

### 13.2 Failure and pause policy

- Classify failures as temporary network, rate limit, quota exhausted, authorization, policy/access, parsing/schema change, or permanent configuration errors.
- Retry temporary failures with bounded exponential backoff and jitter.
- Honor provider retry headers and explicit rate limits.
- Automatically pause a source after the configured number of consecutive authorization, policy, or parsing failures.
- Do not silently fall back from an official API to browser automation or scraping.
- One paused or failing source must not stop other collectors, processing, immediate alerts, or the daily digest.
- `/status` must show why a source is paused and the last successful run.

### 13.3 Freshness, quota, and credential monitoring

- Define a freshness window for every enabled source based on its schedule.
- Mark a source stale when no successful run occurs within that window, even if the process itself is healthy.
- Track requests and known quota consumption where the provider exposes it.
- Slow or pause collection before exceeding a hard quota; never create an uncontrolled retry loop.
- Record credential or authorization expiration dates when known and surface them before expiry.
- Treat zero results as a successful run only when the provider request and parsing completed normally.

### 13.4 Safe re-enablement

Re-enabling a paused source requires a controlled dry run, review of sample results, confirmation that cursors and rate limits are correct, and an explicit configuration change. Automatic recovery may clear short-lived network/rate-limit pauses, but it must not clear policy or authorization pauses without review.

## 14. Source onboarding checklist

Before enabling any real source:

1. Identify the source owner/platform and exact URL/community/account.
2. Choose Tier A, B, or reviewed C access; reject D/E for the MVP.
3. Record why access is authorized.
4. Read current official documentation and applicable terms.
5. Record credentials/scopes required without storing the secret itself.
6. Define rate limits, timeout, retry, and backoff behavior.
7. Define stable external ID and cursor strategy.
8. Define which fields are retained and for how long.
9. Add redacted fixtures and contract tests.
10. Test duplicate, edited, deleted, empty, and rate-limited responses.
11. Test restart behavior, cursor recovery, bounded retries, and automatic pause behavior.
12. Define freshness, quota, authorization-expiry, and consecutive-failure thresholds.
13. Confirm `/status` and logs make stale or paused state understandable without exposing credentials or unnecessary personal data.
14. Start disabled, run a controlled dry run, inspect results, then explicitly enable.
15. Set a review date and owner.

## 15. First manual setup tasks for the project owner

These tasks can proceed while Milestones 1–3 are being coded:

1. Create the Telegram bot with BotFather and record the token outside Git.
2. Record the owner’s numeric Telegram user ID for the allowlist.
3. Decide the digest timezone/time and immediate-alert threshold.
4. Create an Avito tutor listing if desired and create the saved searches above.
5. Collect 30–50 anonymized example posts: true leads, tutor ads, school vacancies, and ambiguous cases.
6. Build a spreadsheet/list of candidate Telegram and VK communities with columns for URL, owner/admin, access method, permission status, and expected lead quality.
7. Choose one search provider only after confirming current access, quota, and pricing.
8. Do not place any production token, cookie, or personal-session file in the repository.

## 16. Official documentation starting points

- Telegram Bot API: <https://core.telegram.org/bots/api>
- Telegram Bot FAQ: <https://core.telegram.org/bots/faq>
- VK developer documentation: <https://dev.vk.com/>
- Avito developer portal: <https://developers.avito.ru/>
- Yandex developer/cloud documentation: <https://yandex.cloud/en/docs/>

These links are starting points. Verify the exact API product and method documentation during the corresponding milestone.

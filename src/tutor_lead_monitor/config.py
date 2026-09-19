import re
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from tutor_lead_monitor.domain.enums import Intent, Subject
from tutor_lead_monitor.domain.models import JSONValue, utc

PositiveInt = Annotated[int, Field(gt=0)]
Score = Annotated[int, Field(ge=0, le=100)]


def _valid_provider_secret(value: SecretStr | None, *, maximum: int, header: bool) -> bool:
    if value is None:
        return False
    text = value.get_secret_value()
    if not 1 <= len(text) <= maximum or not text.isprintable() or any(c.isspace() for c in text):
        return False
    if header and not text.isascii():
        return False
    placeholder = text.casefold().replace("-", "_")
    return not (
        placeholder.startswith(("replace_", "your_", "${", "<"))
        or placeholder in {"changeme", "change_me", "placeholder", "replace", "replace_me"}
    )


class ConfigError(ValueError):
    """Safe configuration error suitable for display without input values."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    database_url: SecretStr
    config_dir: Path = Path("config")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    health_interval_seconds: PositiveInt = 30
    telegram_bot_token: SecretStr | None = None
    telegram_allowed_user_ids: SecretStr | None = None
    telegram_recipient_chat_id: SecretStr | None = None
    yandex_search_api_key: SecretStr | None = Field(default=None, exclude=True)
    # Treat the deployment-specific folder ID as sensitive too.
    yandex_search_folder_id: SecretStr | None = Field(default=None, exclude=True)
    vk_access_token: SecretStr | None = Field(default=None, exclude=True, repr=False)

    def vk_access(self) -> SecretStr:
        # A form-body secret, not an undocumented provider-specific token pattern.
        token = self.vk_access_token
        if not _valid_provider_secret(token, maximum=4096, header=False) or token is None:
            raise ConfigError("Configure valid VK credentials")
        return token

    def yandex_access(self) -> tuple[SecretStr, SecretStr]:
        key, folder = self.yandex_search_api_key, self.yandex_search_folder_id
        if (
            not _valid_provider_secret(key, maximum=4096, header=True)
            or not _valid_provider_secret(folder, maximum=50, header=False)
            or key is None
            or folder is None
        ):
            raise ConfigError("Configure valid Yandex Search credentials")
        return key, folder

    def telegram_access(self) -> tuple[str, frozenset[int], int]:
        """Validate only when Telegram is explicitly started; errors contain no inputs."""
        try:
            token = self.telegram_bot_token.get_secret_value() if self.telegram_bot_token else ""
            users = self.telegram_allowed_user_ids
            allowed = (
                frozenset(int(s.strip()) for s in users.get_secret_value().split(","))
                if users
                else frozenset()
            )
            chat = (
                int(self.telegram_recipient_chat_id.get_secret_value())
                if self.telegram_recipient_chat_id
                else 0
            )
            if (
                not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token)
                or not allowed
                or min(allowed) <= 0
            ):
                raise ValueError
            # MVP delivery is a private owner chat, never a group or public channel.
            if chat not in allowed:
                raise ValueError
            return token, allowed, chat
        except ValueError:
            raise ConfigError(
                "Configure Telegram token, positive owner IDs, and an allowlisted private recipient"
            ) from None

    @field_validator("database_url")
    @classmethod
    def postgres_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError:
            raise ValueError("DATABASE_URL must be a valid PostgreSQL URL") from None
        if url.drivername != "postgresql+psycopg" or not url.database:
            raise ValueError("DATABASE_URL must use postgresql+psycopg and name a database")
        return value


class RetentionConfig(StrictModel):
    raw_rejected_days: PositiveInt = 30
    leads_days: PositiveInt = 90
    notifications_days: PositiveInt = 180


class DeduplicationConfig(StrictModel):
    window_days: PositiveInt = 30
    similarity_threshold: Score = 90


class BusinessConfig(StrictModel):
    timezone: str = "Europe/Rome"
    timezone_display_name: str = Field(default="время Рима", min_length=1, max_length=80)
    digest_time: time = time(9)
    send_empty_digest: bool = False
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    deduplication: DeduplicationConfig = Field(default_factory=DeduplicationConfig)

    @field_validator("timezone_display_name")
    @classmethod
    def valid_timezone_display_name(cls, value: str) -> str:
        if not value.strip() or not re.search("[А-Яа-яЁё]", value):
            raise ValueError("Timezone display name must contain a Russian label")
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown timezone") from None
        return value

    @field_validator("digest_time")
    @classmethod
    def local_digest_time(cls, value: time) -> time:
        if value.tzinfo is not None:
            raise ValueError("Digest time must be local, without an offset")
        return value


class Thresholds(StrictModel):
    review: Score = 45
    digest: Score = 55
    immediate: Score = 90

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if not 0 < self.review < self.digest < self.immediate <= 100:
            raise ValueError("Thresholds must satisfy 0 < review < digest < immediate <= 100")
        return self


class ScoringWeights(StrictModel):
    seeking_tutor: int = 25
    literature: int = 30
    russian_and_literature: int = 15
    exam: int = 10
    olympiad: int = 12
    online: int = 8
    grade_stated: int = 4
    fresh: int = 6
    budget_stated: int = 3
    contact_available: int = 2
    offering_tutoring: int = -70
    school_or_agency_ad: int = -55
    teaching_job: int = -35
    closed_or_stale: int = -50


class ScoringConfig(StrictModel):
    version: str = "rules-v2"
    fresh_hours: PositiveInt = 12
    stale_days: PositiveInt = 30
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    thresholds: Thresholds = Field(default_factory=Thresholds)


class ClassificationRule(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    pattern: str = Field(min_length=1)
    intent: Intent | None = None
    subject: Subject | None = None
    priority: int = 0
    confidence: float = Field(default=0.9, ge=0, le=1)

    @field_validator("pattern")
    @classmethod
    def valid_pattern(cls, value: str) -> str:
        try:
            re.compile(value, re.IGNORECASE)
        except re.error:
            raise ValueError("Invalid rule regex") from None
        return value

    @model_validator(mode="after")
    def one_label(self) -> Self:
        if (self.intent is None) == (self.subject is None):
            raise ValueError("Each rule requires exactly one intent or subject")
        return self


class ProcessingConfig(StrictModel):
    version: str = "rules-v2"
    boilerplate: list[str] = Field(default_factory=list)
    rules: list[ClassificationRule]

    @model_validator(mode="after")
    def unique_rules(self) -> Self:
        ids = [rule.id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("Rule IDs must be unique")
        return self


class SourcePolicy(StrictModel):
    authorization_basis: str = Field(min_length=1)
    terms_review: str = Field(min_length=1)
    rate_limit: str = Field(min_length=1)
    retained_fields: list[str] = Field(min_length=1)
    reviewed_at: datetime | None = None
    reviewer: str | None = None
    notes: str = ""

    @field_validator("reviewer")
    @classmethod
    def normalized_reviewer(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value or len(value) > 160 or not value.isprintable():
            raise ValueError("Reviewer must be nonblank and at most 160 printable characters")
        return value

    @field_validator("reviewed_at")
    @classmethod
    def utc_review_time(cls, value: datetime | None) -> datetime | None:
        return utc(value) if value is not None else None


class SourceOperations(StrictModel):
    freshness_sla_seconds: PositiveInt
    pause_after_consecutive_failures: PositiveInt
    quota_policy: str = Field(min_length=1, pattern=r"\S")
    authorization_expires_at: datetime | None = None

    @field_validator("authorization_expires_at")
    @classmethod
    def utc_authorization_expiry(cls, value: datetime | None) -> datetime | None:
        return utc(value) if value is not None else None


class SourceConfig(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal[
        "fixture",
        "web_search",
        "vk_api",
        "telegram_bot_updates",
        "manual_import",
        "avito_notifications",
    ]
    display_name: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    enabled: bool = False
    policy_status: Literal["pending", "approved", "paused", "blocked"] = "pending"
    access_method: Literal[
        "fixture", "official_api", "authorized_bot", "saved_notification", "public_web", "manual"
    ]
    collector_interval_seconds: PositiveInt
    credential_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    operations: SourceOperations
    config: dict[str, JSONValue] = Field(default_factory=dict)
    policy: SourcePolicy
    retention_days: PositiveInt = 90

    @model_validator(mode="after")
    def enablement(self) -> Self:
        if self.enabled and self.policy_status != "approved":
            raise ValueError("Enabled sources require approved policy")
        if self.enabled and self.kind not in {"fixture", "web_search", "vk_api"}:
            raise ValueError("Unsupported collector kind")
        if self.enabled and self.kind != "fixture":
            self.check_authorization()
        if self.kind == "fixture":
            if self.access_method != "fixture":
                raise ValueError("Fixture source requires fixture access method")
            FixtureOptions.model_validate(self.config)
        elif self.kind == "web_search":
            if (
                self.access_method != "official_api"
                or self.credential_env != "YANDEX_SEARCH_API_KEY"
            ):
                raise ValueError("Yandex requires official_api and its credential environment name")
            WebSearchOptions.model_validate(self.config)
        elif self.kind == "vk_api":
            if self.access_method != "official_api" or self.credential_env != "VK_ACCESS_TOKEN":
                raise ValueError("VK requires official_api and its credential environment name")
            options = VKOptions.model_validate(self.config)
            if VK_COMMUNITIES[options.community_id] != (self.key, options.screen_name):
                raise ValueError("VK source must match the reviewed community allowlist")
        return self

    def check_authorization(self) -> None:
        """Recheck at execution too: approvals can change and authorization can expire."""
        if self.kind != "fixture":
            reviewer = SourcePolicy.normalized_reviewer(self.policy.reviewer)
            reviewed = self.policy.reviewed_at
            if self.policy_status != "approved" or reviewer is None or reviewed is None:
                raise ValueError(
                    "Real sources require approved policy and complete review metadata"
                )
            utc(reviewed)
        expiry = self.operations.authorization_expires_at
        if expiry is not None and utc(expiry) <= datetime.now(UTC):
            raise ValueError("Source authorization has expired")


class FixtureOptions(StrictModel):
    page_size: PositiveInt = 2
    dataset: Literal["primary", "crosspost"] = "primary"


# This increment is restricted to the owner's seven reviewed communities.
VK_COMMUNITIES = {
    44923684: ("vk_repetera", "repetera"),
    218494134: ("vk_ishchu_repetitora", "ishchu_repetitora"),
    69560393: ("vk_vakrep", "vakrep"),
    236075282: ("vk_kaliningrad", "club236075282"),
    236062075: ("vk_ulyanovsk", "club236062075"),
    181505950: ("vk_repetitor_poisk", "repetitor_poisk"),
    236082001: ("vk_ulan_ude", "club236082001"),
}


class VKOptions(StrictModel):
    community_id: int = Field(gt=0, strict=True)
    screen_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    api_version: Literal["5.199"] = "5.199"
    initial_posts: int = Field(default=20, ge=1, le=20, strict=True)
    incremental_posts: int = Field(default=100, ge=1, le=100, strict=True)

    @field_validator("community_id")
    @classmethod
    def reviewed_community(cls, value: int) -> int:
        if value not in VK_COMMUNITIES:
            raise ValueError("VK community is outside the reviewed allowlist")
        return value


class WebSearchOptions(StrictModel):
    provider: Literal["yandex"]
    query_ids: list[str] = Field(min_length=1, max_length=10)
    max_requests_per_run: int = Field(default=3, ge=1, le=10, strict=True)
    results_per_query: int = Field(default=10, ge=1, le=20, strict=True)

    @model_validator(mode="after")
    def bounded_queries(self) -> Self:
        if len(set(self.query_ids)) != len(self.query_ids):
            raise ValueError("Query IDs must be unique")
        if len(self.query_ids) > self.max_requests_per_run:
            raise ValueError("Selected queries exceed the per-run request budget")
        return self


class SourceRegistry(StrictModel):
    sources: list[SourceConfig]

    @model_validator(mode="after")
    def unique_keys(self) -> Self:
        keys = [source.key for source in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("Source keys must be unique")
        return self


class QueryGroup(StrictModel):
    seek_any: list[str] = Field(default_factory=list)
    subject_any: list[str] = Field(default_factory=list)
    phrases_any: list[str] = Field(default_factory=list)


class QueryConfig(StrictModel):
    query_groups: dict[str, QueryGroup] = Field(default_factory=dict)
    searches: dict[str, "SearchQueryConfig"] = Field(default_factory=dict)

    @field_validator("searches")
    @classmethod
    def stable_ids(cls, value: dict[str, "SearchQueryConfig"]) -> dict[str, "SearchQueryConfig"]:
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) for key in value):
            raise ValueError("Search IDs must be bounded stable identifiers")
        return value


class SearchQueryConfig(StrictModel):
    group: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    text: str = Field(min_length=1, max_length=400)

    @field_validator("text")
    @classmethod
    def valid_query(cls, value: str) -> str:
        if not value.strip() or len(value.split()) > 40 or any(ord(c) < 32 for c in value):
            raise ValueError("Search query must be nonempty, single-line, and at most 40 words")
        return value.strip()


class AppConfig(StrictModel):
    business: BusinessConfig
    scoring: ScoringConfig
    registry: SourceRegistry
    queries: QueryConfig
    processing: ProcessingConfig

    @model_validator(mode="after")
    def known_searches(self) -> Self:
        for source in self.registry.sources:
            if source.kind == "web_search":
                options = WebSearchOptions.model_validate(source.config)
                if any(key not in self.queries.searches for key in options.query_ids):
                    raise ValueError("Source refers to unknown search IDs")
        if any(q.group not in self.queries.query_groups for q in self.queries.searches.values()):
            raise ValueError("Search refers to an unknown query group")
        return self


def load_config(directory: Path) -> AppConfig:
    """YAML overrides typed defaults. Deployment values come from Settings."""
    models: dict[str, type[BaseModel]] = {
        "business": BusinessConfig,
        "scoring": ScoringConfig,
        "sources": SourceRegistry,
        "queries": QueryConfig,
        "processing": ProcessingConfig,
    }
    loaded: dict[str, BaseModel] = {}
    for name, model in models.items():
        try:
            with (directory / f"{name}.yml").open(encoding="utf-8") as stream:
                loaded[name] = model.model_validate(yaml.safe_load(stream))
        except (OSError, ValueError, yaml.YAMLError):
            raise ConfigError(f"Cannot load or validate {name}.yml; check its schema") from None
    return AppConfig.model_validate(
        {
            "business": loaded["business"],
            "scoring": loaded["scoring"],
            "registry": loaded["sources"],
            "queries": loaded["queries"],
            "processing": loaded["processing"],
        }
    )

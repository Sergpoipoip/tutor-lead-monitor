import re
from datetime import datetime, time
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
    timezone: str = "UTC"
    digest_time: time = time(19)
    send_empty_digest: bool = False
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    deduplication: DeduplicationConfig = Field(default_factory=DeduplicationConfig)

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
    kind: str = Field(min_length=1)
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
        if self.enabled and self.kind != "fixture":
            raise ValueError("Only fixture sources can be enabled in Milestones 1 and 2")
        if self.kind == "fixture":
            if self.access_method != "fixture":
                raise ValueError("Fixture source requires fixture access method")
            FixtureOptions.model_validate(self.config)
        return self


class FixtureOptions(StrictModel):
    page_size: PositiveInt = 2
    dataset: Literal["primary", "crosspost"] = "primary"


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


class AppConfig(StrictModel):
    business: BusinessConfig
    scoring: ScoringConfig
    registry: SourceRegistry
    queries: QueryConfig
    processing: ProcessingConfig


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

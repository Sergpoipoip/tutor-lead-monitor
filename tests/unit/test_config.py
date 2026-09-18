from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr, ValidationError

from tutor_lead_monitor.config import (
    BusinessConfig,
    ConfigError,
    RetentionConfig,
    Settings,
    SourceConfig,
    SourceOperations,
    SourceRegistry,
    Thresholds,
    load_config,
)


def test_versioned_configuration() -> None:
    config = load_config(Path("config"))
    assert config.scoring.weights.offering_tutoring == -70
    assert config.scoring.thresholds.immediate == 90
    assert config.business.retention.leads_days == 90
    assert config.business.timezone_display_name == "время Рима"
    assert {s.kind for s in config.registry.sources if s.enabled} == {"fixture"}


def test_environment_precedence_and_redaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("DATABASE_URL=postgresql+psycopg://dotenv@localhost/unused\n")
    url = "postgresql+psycopg://user:synthetic-sentinel@localhost/test"
    monkeypatch.setenv("DATABASE_URL", url)
    settings = Settings(_env_file=dotenv)
    assert settings.database_url.get_secret_value() == url
    assert "synthetic-sentinel" not in repr(settings)
    assert "synthetic-sentinel" not in settings.model_dump_json()


def test_bad_database_url_does_not_echo_input() -> None:
    with pytest.raises(ValidationError) as error:
        Settings(database_url=SecretStr("not-a-url-synthetic-sentinel"), _env_file=None)
    assert "synthetic-sentinel" not in str(error.value)


@pytest.mark.parametrize("url", ["sqlite:///test", "postgresql://localhost/test"])
def test_requires_psycopg_postgresql(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=SecretStr(url), _env_file=None)


@pytest.mark.parametrize("timezone", ["Unknown/Timezone", "../UTC"])
def test_bad_timezone(timezone: str) -> None:
    with pytest.raises(ValidationError):
        BusinessConfig(timezone=timezone)


@pytest.mark.parametrize("label", ["", "   ", "Europe/Rome", "я" * 81])
def test_bad_timezone_display_name(label: str) -> None:
    with pytest.raises(ValidationError):
        BusinessConfig(timezone_display_name=label)


def test_invalid_business_settings() -> None:
    with pytest.raises(ValidationError):
        Thresholds(review=65, digest=65)
    with pytest.raises(ValidationError):
        RetentionConfig(leads_days=0)
    with pytest.raises(ValidationError):
        BusinessConfig.model_validate({"digest_time": "19:00:00+01:00"})


def test_source_policy_and_scope(source_config: SourceConfig) -> None:
    data = source_config.model_dump()
    data["policy_status"] = "pending"
    with pytest.raises(ValidationError, match="approved"):
        SourceConfig.model_validate(data)
    data.update(policy_status="approved", kind="web_search", access_method="public_web")
    with pytest.raises(ValidationError, match="Milestones 1 and 2"):
        SourceConfig.model_validate(data)
    data["enabled"] = False
    assert not SourceConfig.model_validate(data).enabled


def test_source_keys_and_options(source_config: SourceConfig) -> None:
    with pytest.raises(ValidationError, match="unique"):
        SourceRegistry(sources=[source_config, source_config])
    data = source_config.model_dump()
    data["config"] = {"page_size": 0}
    with pytest.raises(ValidationError):
        SourceConfig.model_validate(data)


def test_yaml_failure_is_sanitized(tmp_path: Path) -> None:
    (tmp_path / "business.yml").write_text("timezone: [synthetic-sentinel\n")
    with pytest.raises(ConfigError) as error:
        load_config(tmp_path)
    assert "synthetic-sentinel" not in str(error.value)
    assert "business.yml" in str(error.value)


@pytest.mark.parametrize("filename", ["sources.yml", "sources.example.yml"])
def test_registry_operations_configuration(filename: str) -> None:
    registry = SourceRegistry.model_validate(
        yaml.safe_load((Path("config") / filename).read_text(encoding="utf-8"))
    )
    operations = registry.sources[0].operations
    assert isinstance(operations, SourceOperations)
    assert operations.freshness_sla_seconds == 900
    assert operations.pause_after_consecutive_failures == 5
    assert operations.quota_policy.strip()
    assert operations.authorization_expires_at is None


def test_source_requires_operations(source_config: SourceConfig) -> None:
    data = source_config.model_dump()
    del data["operations"]
    with pytest.raises(ValidationError, match="operations"):
        SourceConfig.model_validate(data)


@pytest.mark.parametrize("field", ["freshness_sla_seconds", "pause_after_consecutive_failures"])
@pytest.mark.parametrize("value", [0, -1, 1.5])
def test_invalid_operations_thresholds(
    source_config: SourceConfig, field: str, value: int | float
) -> None:
    data = source_config.model_dump()
    data["operations"][field] = value
    with pytest.raises(ValidationError, match=field):
        SourceConfig.model_validate(data)


@pytest.mark.parametrize("value", [0, -1])
def test_invalid_collection_interval(source_config: SourceConfig, value: int) -> None:
    data = source_config.model_dump()
    data["collector_interval_seconds"] = value
    with pytest.raises(ValidationError, match="collector_interval_seconds"):
        SourceConfig.model_validate(data)


@pytest.mark.parametrize("policy", ["", "   "])
def test_empty_quota_policy(source_config: SourceConfig, policy: str) -> None:
    data = source_config.operations.model_dump()
    data["quota_policy"] = policy
    with pytest.raises(ValidationError, match="quota_policy"):
        SourceOperations.model_validate(data)


@pytest.mark.parametrize(
    "expiry",
    [
        "2026-12-01T12:00:00+02:00",
        "2026-12-01T05:00:00-05:00",
        "2026-12-01T10:00:00Z",
        datetime.fromisoformat("2026-12-01T12:00:00+02:00"),
    ],
)
def test_authorization_expiry_normalized_to_utc(
    source_config: SourceConfig, expiry: str | datetime
) -> None:
    data = source_config.model_dump()
    data["operations"]["authorization_expires_at"] = expiry
    source = SourceConfig.model_validate(data)
    normalized = source.operations.authorization_expires_at
    assert normalized == datetime(2026, 12, 1, 10, tzinfo=UTC)
    assert normalized.tzinfo is UTC
    serialized = source.model_dump(mode="json")["operations"]
    assert serialized["authorization_expires_at"] == "2026-12-01T10:00:00Z"


@pytest.mark.parametrize("expiry", ["2026-12-01T12:00:00", datetime(2026, 12, 1, 12)])
def test_naive_authorization_expiry_rejected(
    source_config: SourceConfig, expiry: str | datetime
) -> None:
    data = source_config.operations.model_dump()
    data["authorization_expires_at"] = expiry
    with pytest.raises(ValidationError, match="Timezone-aware"):
        SourceOperations.model_validate(data)


def test_optional_authorization_expiry_defaults_to_none() -> None:
    operations = SourceOperations(
        freshness_sla_seconds=1, pause_after_consecutive_failures=1, quota_policy="Local only"
    )
    assert operations.authorization_expires_at is None

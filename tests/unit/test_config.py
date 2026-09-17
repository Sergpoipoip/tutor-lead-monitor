from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from tutor_lead_monitor.config import (
    BusinessConfig,
    ConfigError,
    RetentionConfig,
    Settings,
    SourceConfig,
    SourceRegistry,
    Thresholds,
    load_config,
)


def test_versioned_configuration() -> None:
    config = load_config(Path("config"))
    assert config.scoring.weights.offering_tutoring == -70
    assert config.scoring.thresholds.immediate == 90
    assert config.business.retention.leads_days == 90
    assert [s.kind for s in config.registry.sources if s.enabled] == ["fixture"]


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
    with pytest.raises(ValidationError, match="Milestone 1"):
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

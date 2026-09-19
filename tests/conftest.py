import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import Connection, Engine
from sqlalchemy.orm import Session

from tutor_lead_monitor.config import Settings, SourceConfig, load_config
from tutor_lead_monitor.db.session import create_db_engine

ROOT = Path(__file__).resolve().parents[1]
# Exercise PTB's forward-compatible RetryAfter timedelta representation.
os.environ.setdefault("PTB_TIMEDELTA", "true")


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests use explicit settings or synthetic dotenv fixtures, never owner secrets."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in (
        "VK_ACCESS_TOKEN",
        "YANDEX_SEARCH_API_KEY",
        "YANDEX_SEARCH_FOLDER_ID",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_IDS",
        "TELEGRAM_RECIPIENT_CHAT_ID",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """All HTTP tests must inject MockTransport, including Yandex and destinations."""
    attempts: list[bool] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        attempts.append(True)
        raise AssertionError("Real HTTP transport is forbidden in tests")

    async def async_forbidden(*args: object, **kwargs: object) -> None:
        forbidden()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_forbidden)
    yield
    assert not attempts, "A test attempted real HTTP, even if its caller caught the error"


@pytest.fixture(autouse=True)
def no_telegram_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Tests must use fake Telegram transports")

    monkeypatch.setattr("telegram.Bot._post", forbidden)


@pytest.fixture
def source_config() -> SourceConfig:
    return load_config(ROOT / "config").registry.sources[0]


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        if os.environ.get("CI") or os.environ.get("REQUIRE_INTEGRATION_TESTS"):
            raise pytest.UsageError(
                "TEST_DATABASE_URL is required in CI; integration tests cannot skip"
            )
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    settings = Settings(database_url=SecretStr(url), _env_file=None)
    db = create_db_engine(settings)
    if not (db.url.database or "").endswith("_test"):
        raise pytest.UsageError("Integration database name must end with _test (schema is reset)")
    yield db
    db.dispose()


def alembic_config(connection: Connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


@pytest.fixture
def migrated_engine(engine: Engine) -> Iterator[Engine]:
    # Use only an explicitly selected disposable test database; never create_all.
    with engine.begin() as connection:
        command.downgrade(alembic_config(connection), "base")
        command.upgrade(alembic_config(connection), "head")
    yield engine


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    with Session(migrated_engine) as db, db.begin():
        yield db

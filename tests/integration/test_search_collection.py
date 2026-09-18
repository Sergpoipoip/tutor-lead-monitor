import base64
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from tutor_lead_monitor.application.collect import run_collector
from tutor_lead_monitor.application.notify import send_digest
from tutor_lead_monitor.application.process import process_pending
from tutor_lead_monitor.cli import main
from tutor_lead_monitor.collectors.base import Collector
from tutor_lead_monitor.collectors.factory import build_collector
from tutor_lead_monitor.config import AppConfig, Settings, SourceConfig, load_config
from tutor_lead_monitor.db.models import (
    CollectionRun,
    CollectorState,
    Lead,
    Notification,
    RawItem,
    Source,
)
from tutor_lead_monitor.db.repositories import sync_source
from tutor_lead_monitor.notifications.base import FakeNotifier
from tutor_lead_monitor.search.yandex import ENDPOINT

pytestmark = pytest.mark.integration
KEY = "synthetic_api_key_not_real"
FOLDER = "syntheticfolder00001"


def approved_config() -> AppConfig:
    config = load_config(Path("config"))
    data = config.model_dump()
    data["registry"]["sources"][-1].update(enabled=True, policy_status="approved")
    data["registry"]["sources"][-1]["policy"].update(
        reviewer="Test reviewer", reviewed_at="2026-09-18T10:00:00Z"
    )
    return AppConfig.model_validate(data)


def settings(engine: Engine) -> Settings:
    return Settings(
        database_url=SecretStr(engine.url.render_as_string(hide_password=False)),
        yandex_search_api_key=SecretStr(KEY),
        yandex_search_folder_id=SecretStr(FOLDER),
        _env_file=None,
    )


def respond(request: httpx.Request) -> httpx.Response:
    assert str(request.url) == ENDPOINT
    xml = Path("tests/fixtures/yandex_search.xml").read_bytes()
    return httpx.Response(
        200, json={"rawData": base64.b64encode(xml).decode(), "ignored": KEY + FOLDER}
    )


async def test_search_idempotency_pipeline_and_russian_snippet_delivery(
    migrated_engine: Engine,
) -> None:
    config = approved_config()
    source = config.registry.sources[-1]
    collector = build_collector(
        source, config, settings(migrated_engine), transport=httpx.MockTransport(respond)
    )
    first = await run_collector(migrated_engine, source, collector)
    again = await run_collector(migrated_engine, source, collector)
    assert (first.items_seen, first.items_inserted, again.items_inserted) == (9, 3, 0)
    assert first.status == again.status == "succeeded"
    with Session(migrated_engine) as session:
        raws = session.scalars(select(RawItem)).all()
        assert len(raws) == 3
        assert all(KEY not in str(r.metadata_) and FOLDER not in str(r.metadata_) for r in raws)
        state = session.scalars(select(CollectorState)).one()
        assert state.cursor is None and state.consecutive_failures == 0
        assert session.scalar(select(func.count()).select_from(Notification)) == 0
    process_pending(migrated_engine, config, now=datetime(2026, 9, 18, 11, tzinfo=UTC))
    with Session(migrated_engine) as session:
        raw = session.scalars(select(RawItem).where(RawItem.url.contains("ref=42"))).one()
        assert raw.canonical_url == "https://example.invalid/request?ref=42"
        assert (session.scalar(select(func.count()).select_from(Lead)) or 0) >= 1
    fake = FakeNotifier()
    now = datetime(2026, 9, 18, 11, tzinfo=UTC)
    await send_digest(migrated_engine, config, 123, fake, now.date(), now=now)
    assert fake.sent
    assert "Фрагмент поисковой выдачи, не полный текст объявления:" in fake.sent[0][1].html
    assert 'href="https://example.invalid/request?ref=42"' in fake.sent[0][1].html
    assert "Дайджест за 18.09.2026 · время Рима" in fake.sent[0][1].html


async def test_partial_run_replays_completed_queries(migrated_engine: Engine) -> None:
    config = approved_config()
    source = config.registry.sources[-1]
    calls = 0

    def fail_second(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text=KEY + FOLDER) if calls == 2 else respond(request)

    collector = build_collector(
        source, config, settings(migrated_engine), transport=httpx.MockTransport(fail_second)
    )
    first = await run_collector(migrated_engine, source, collector)
    assert first.status == "partial" and first.items_inserted == 3 and calls == 2
    with Session(migrated_engine) as session:
        run = session.get(CollectionRun, first.run_id)
        assert run is not None and run.error_category == "provider_server"
        assert session.scalars(select(CollectorState)).one().cursor is None
    again = await run_collector(migrated_engine, source, collector)
    assert again.status == "succeeded" and again.items_inserted == 0 and calls == 5


async def test_rate_limit_pause_and_sanitized_audit(
    migrated_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    config = approved_config()
    source = config.registry.sources[-1]
    calls = 0

    def rate_limit(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text=KEY + FOLDER, headers={"Retry-After": "30"})

    collector = build_collector(
        source, config, settings(migrated_engine), transport=httpx.MockTransport(rate_limit)
    )
    outcome = await run_collector(migrated_engine, source, collector)
    assert outcome.status == "failed"
    with Session(migrated_engine) as session:
        run = session.get(CollectionRun, outcome.run_id)
        assert run is not None and run.error_category == "rate_limit" and run.error_message is None
        state = session.scalars(select(CollectorState)).one()
        assert state.paused_until is not None and state.paused_until > datetime.now(UTC)
        assert state.consecutive_failures == 1
    with pytest.raises(ValueError, match="paused"):
        await run_collector(migrated_engine, source, collector)
    assert calls == 1
    assert KEY not in caplog.text and FOLDER not in caplog.text


async def test_stored_disabled_source_blocks_requests_after_construction(
    migrated_engine: Engine,
) -> None:
    config = approved_config()
    source = config.registry.sources[-1]
    collector = build_collector(source, config, settings(migrated_engine))
    with Session(migrated_engine) as session, session.begin():
        sync_source(session, source.model_copy(update={"enabled": False}))
    with pytest.raises(ValueError, match="Stored source"):
        await run_collector(migrated_engine, source, collector)
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(CollectionRun)) == 0


def test_cli_factory_supports_mixed_pipeline_and_collect_boundary(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = approved_config()
    calls: list[str] = []

    def factory(source: SourceConfig, app: AppConfig, env: Settings) -> Collector:
        calls.append(source.kind)
        return build_collector(source, app, env, transport=httpx.MockTransport(respond))

    monkeypatch.setattr("tutor_lead_monitor.cli.build_collector", factory)
    monkeypatch.setattr("tutor_lead_monitor.cli.load_config", lambda path: config)
    monkeypatch.setenv("DATABASE_URL", migrated_engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", KEY)
    monkeypatch.setenv("YANDEX_SEARCH_FOLDER_ID", FOLDER)
    assert main(["collect", "--source", "yandex_web_search"]) == 0
    assert calls == ["web_search"]
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(Lead)) == 0
        assert set(session.scalars(select(RawItem.processing_status))) == {"pending"}
    assert main(["pipeline", "--as-of", "2026-09-18T11:00:00Z"]) == 0
    assert calls == ["web_search", "fixture", "fixture", "web_search"]
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(Notification)) == 0
    output = capsys.readouterr()
    assert all(secret not in output.out + output.err for secret in (KEY, FOLDER, "littérature"))


def test_default_cli_skips_yandex_and_sync_preserves_history(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "")
    monkeypatch.setenv("YANDEX_SEARCH_FOLDER_ID", "")
    assert main(["sync-sources"]) == 0
    with Session(migrated_engine) as session, session.begin():
        source = session.scalars(select(Source).where(Source.key == "yandex_web_search")).one()
        source_id = source.id
        assert not source.enabled and source.policy_status == "pending"
        state = session.get(CollectorState, source_id)
        assert state is not None
        state.consecutive_failures = 2
        state.cursor = {"historic": 1}
    assert main(["sync-sources"]) == 0
    with Session(migrated_engine) as session:
        state = session.get(CollectorState, source_id)
        assert (
            state is not None
            and state.consecutive_failures == 2
            and state.cursor == {"historic": 1}
        )
    assert main(["collect", "--source", "yandex_web_search"]) == 1
    assert main(["pipeline", "--as-of", "2026-01-01T13:00:00Z"]) == 0
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(RawItem)) == 14
        assert (
            session.scalar(
                select(func.count())
                .select_from(CollectionRun)
                .where(CollectionRun.source_id == source_id)
            )
            == 0
        )
    output = capsys.readouterr()
    assert "Configure valid Yandex" not in output.err  # Disabled policy gate takes precedence.


@pytest.mark.parametrize("missing", ["YANDEX_SEARCH_API_KEY", "YANDEX_SEARCH_FOLDER_ID"])
def test_cli_credentials_fail_without_http_or_secret_output(
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    monkeypatch.setattr("tutor_lead_monitor.cli.load_config", lambda path: approved_config())
    monkeypatch.setenv("DATABASE_URL", migrated_engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", KEY)
    monkeypatch.setenv("YANDEX_SEARCH_FOLDER_ID", FOLDER)
    monkeypatch.setenv(missing, "")
    assert main(["collect", "--source", "yandex_web_search"]) == 1
    output = capsys.readouterr()
    assert "ConfigError" in output.err
    assert KEY not in output.err + output.out and FOLDER not in output.err + output.out
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(RawItem)) == 0


@pytest.mark.parametrize(
    "field,value", [("reviewer", None), ("reviewed_at", None), ("reviewer", " ")]
)
async def test_collection_rechecks_review_after_construction(
    migrated_engine: Engine, field: str, value: str | None
) -> None:
    config = approved_config()
    source = config.registry.sources[-1]
    collector = build_collector(source, config, settings(migrated_engine))
    source.policy = source.policy.model_copy(update={field: value})
    with pytest.raises(ValueError):
        await run_collector(migrated_engine, source, collector)
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(CollectionRun)) == 0
        assert session.scalar(select(func.count()).select_from(RawItem)) == 0

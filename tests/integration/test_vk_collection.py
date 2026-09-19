import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from tutor_lead_monitor.application.collect import run_collector
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
from tutor_lead_monitor.db.repositories import persist_page, sync_source
from tutor_lead_monitor.domain.models import CollectionPage

pytestmark = pytest.mark.integration
TOKEN = "synthetic-vk-integration-token"
COMMUNITY = 218494134
FIXTURE = Path("tests/fixtures/vk_wall.json").read_bytes()


def approved(key: str = "vk_ishchu_repetitora") -> SourceConfig:
    source = next(s for s in load_config(Path("config")).registry.sources if s.key == key)
    data = source.model_dump()
    data.update(enabled=True, policy_status="approved")
    data["policy"].update(reviewer="Synthetic reviewer", reviewed_at="2026-09-19T00:00:00Z")
    return SourceConfig.model_validate(data)


def collector(source: SourceConfig, payload: bytes = FIXTURE) -> Collector:
    env = Settings(
        database_url=SecretStr("postgresql+psycopg://localhost/unused_test"),
        vk_access_token=SecretStr(TOKEN),
        _env_file=None,
    )
    return build_collector(
        source,
        load_config(Path("config")),
        env,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload)),
    )


async def test_replay_new_ids_deleted_boundary_and_processing(migrated_engine: Engine) -> None:
    source = approved()
    first = await run_collector(migrated_engine, source, collector(source))
    assert first.status == "succeeded" and first.items_inserted == 4
    repeat = await run_collector(migrated_engine, source, collector(source))
    assert repeat.status == "succeeded" and repeat.items_seen == repeat.items_inserted == 0
    payload = json.loads(FIXTURE)
    # Delete the previously newest item; an older boundary must still permit progress.
    payload["response"]["items"][1]["id"] = 31
    payload["response"]["items"][1]["text"] = "Нужен репетитор по литературе для 11 класса."
    next_run = await run_collector(
        migrated_engine, source, collector(source, json.dumps(payload).encode())
    )
    assert next_run.items_inserted == 1
    with Session(migrated_engine) as session:
        state = session.scalars(select(CollectorState)).one()
        assert state.cursor == {"version": 1, "community_id": COMMUNITY, "post_id": 31}
        assert session.scalar(select(func.count()).select_from(RawItem)) == 5
        old = session.scalar(select(RawItem).where(RawItem.external_id == f"{COMMUNITY}_30"))
        assert old is not None and old.text.startswith("  Ищу")
        assert session.scalar(select(func.count()).select_from(Lead)) == 0
        assert session.scalar(select(func.count()).select_from(Notification)) == 0
    config = load_config(Path("config"))
    outcome = process_pending(migrated_engine, config, now=datetime(2026, 9, 19, 13, tzinfo=UTC))
    assert outcome.failed == 0
    with Session(migrated_engine) as session:
        assert (session.scalar(select(func.count()).select_from(Lead)) or 0) >= 2
        advertisement = session.scalar(
            select(RawItem).where(RawItem.external_id == f"{COMMUNITY}_28")
        )
        assert advertisement is not None and advertisement.processing_status == "rejected"
        assert session.scalar(select(func.count()).select_from(Notification)) == 0


async def test_overflow_audit_without_cursor_or_evidence_advancement(
    migrated_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    source = approved()
    await run_collector(migrated_engine, source, collector(source))
    original = json.loads(FIXTURE)["response"]["items"][1]
    body = json.dumps(
        {"response": {"count": 200, "items": [original | {"id": i} for i in range(200, 100, -1)]}}
    ).encode()
    outcome = await run_collector(migrated_engine, source, collector(source, body))
    assert outcome.status == "failed" and outcome.items_inserted == 0
    with Session(migrated_engine) as session:
        state = session.scalars(select(CollectorState)).one()
        assert state.cursor == {"version": 1, "community_id": COMMUNITY, "post_id": 30}
        assert state.consecutive_failures == 1
        run = session.get(CollectionRun, outcome.run_id)
        assert (
            run is not None
            and run.error_category == "cursor_overflow"
            and run.error_message is None
        )
        assert session.scalar(select(func.count()).select_from(RawItem)) == 4
    assert TOKEN not in caplog.text


async def test_page_persistence_failure_replays_without_skipping(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = approved()
    with Session(migrated_engine) as session, session.begin():
        source_id = sync_source(session, source)
        state = session.get(CollectorState, source_id)
        assert state is not None
        state.cursor = {"version": 1, "community_id": COMMUNITY, "post_id": 0}

    def fail_one(
        session: Session,
        *,
        source_id: UUID,
        source_key: str,
        page: CollectionPage,
        high_water_mark: datetime | None = None,
    ) -> int:
        if page.items and page.items[0].external_id == f"{COMMUNITY}_29":
            raise ValueError("synthetic persistence failure")
        return persist_page(
            session,
            source_id=source_id,
            source_key=source_key,
            page=page,
            high_water_mark=high_water_mark,
        )

    with monkeypatch.context() as patch:
        patch.setattr("tutor_lead_monitor.application.collect.persist_page", fail_one)
        result = await run_collector(migrated_engine, source, collector(source))
    assert result.status == "partial" and result.items_inserted == 3 and result.items_failed == 1
    with Session(migrated_engine) as session:
        state = session.get(CollectorState, source_id)
        assert state is not None and state.cursor is not None and state.cursor["post_id"] == 0
    result = await run_collector(migrated_engine, source, collector(source))
    assert result.status == "succeeded" and result.items_inserted == 1
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(RawItem)) == 4
        state = session.get(CollectorState, source_id)
        assert state is not None and state.cursor is not None and state.cursor["post_id"] == 30


async def test_communities_have_independent_state(migrated_engine: Engine) -> None:
    first, second = approved(), approved("vk_repetera")
    await run_collector(migrated_engine, first, collector(first))
    failed = await run_collector(
        migrated_engine, second, collector(second, b'{"error":{"error_code":29}}')
    )
    assert failed.status == "failed"
    with Session(migrated_engine) as session:
        states = {
            key: state
            for key, state in session.execute(
                select(Source.key, CollectorState).join(CollectorState)
            )
        }
        assert states[first.key].cursor is not None
        assert states[first.key].cursor["community_id"] == COMMUNITY
        assert states[first.key].consecutive_failures == 0
        assert states[second.key].cursor is None and states[second.key].consecutive_failures == 1


@pytest.mark.parametrize("mode", ["disabled", "paused", "expired", "review_missing"])
async def test_runtime_gates_before_any_http(migrated_engine: Engine, mode: str) -> None:
    source = approved()
    instance = collector(source)
    with Session(migrated_engine) as session, session.begin():
        source_id = sync_source(session, source)
        stored = session.get(Source, source_id)
        state = session.get(CollectorState, source_id)
        assert stored is not None and state is not None
        if mode == "disabled":
            stored.enabled = False
        elif mode == "paused":
            state.paused_until = datetime.now(UTC) + timedelta(hours=1)
        elif mode == "expired":
            source.operations.authorization_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        else:
            source.policy.reviewer = None
    with pytest.raises(ValueError):
        await run_collector(migrated_engine, source, instance)
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(CollectionRun)) == 0


def test_default_cli_registers_but_never_collects_vk(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("VK_ACCESS_TOKEN", "")
    assert main(["sync-sources"]) == 0
    assert main(["pipeline", "--as-of", "2026-01-01T13:00:00Z"]) == 0
    assert main(["collect", "--source", "vk_ishchu_repetitora"]) == 1
    with Session(migrated_engine) as session:
        sources = session.scalars(select(Source).where(Source.kind == "vk_api")).all()
        assert len(sources) == 7 and all(
            not s.enabled and s.policy_status == "pending" for s in sources
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(CollectionRun)
                .join(Source)
                .where(Source.kind == "vk_api")
            )
            == 0
        )
    output = capsys.readouterr()
    assert "access_or_policy" in output.err and TOKEN not in output.out + output.err


def test_cli_collect_keeps_processing_separate_and_sanitizes_missing_token(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = load_config(Path("config"))
    config.registry.sources = [
        approved() if s.key == "vk_ishchu_repetitora" else s for s in config.registry.sources
    ]
    monkeypatch.setenv("DATABASE_URL", migrated_engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("VK_ACCESS_TOKEN", "")
    monkeypatch.setattr("tutor_lead_monitor.cli.load_config", lambda path: config)
    assert main(["collect", "--source", "vk_ishchu_repetitora"]) == 1
    assert "invalid_configuration" in capsys.readouterr().err
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=FIXTURE)

    def factory(source: SourceConfig, app: AppConfig, env: Settings) -> Collector:
        return build_collector(source, app, env, transport=httpx.MockTransport(respond))

    monkeypatch.setattr("tutor_lead_monitor.cli.build_collector", factory)
    monkeypatch.setenv("VK_ACCESS_TOKEN", TOKEN)
    assert main(["collect", "--source", "vk_ishchu_repetitora"]) == 0
    assert calls == 1
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(RawItem)) == 4
        assert session.scalar(select(func.count()).select_from(Lead)) == 0
        assert session.scalar(select(func.count()).select_from(Notification)) == 0
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


async def test_partial_first_window_cannot_skip_failed_item_after_new_arrival(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = approved()
    source.config["initial_posts"] = 2
    template = json.loads(FIXTURE)["response"]["items"][1]
    first = json.dumps(
        {"response": {"count": 30, "items": [template | {"id": i} for i in (30, 29)]}}
    ).encode()
    real_persist = persist_page

    def fail_one(
        session: Session,
        *,
        source_id: UUID,
        source_key: str,
        page: CollectionPage,
        high_water_mark: datetime | None = None,
    ) -> int:
        if page.items and page.items[0].external_id == f"{COMMUNITY}_29":
            raise ValueError("synthetic persistence failure")
        return real_persist(
            session,
            source_id=source_id,
            source_key=source_key,
            page=page,
            high_water_mark=high_water_mark,
        )

    with monkeypatch.context() as patch:
        patch.setattr("tutor_lead_monitor.application.collect.persist_page", fail_one)
        result = await run_collector(migrated_engine, source, collector(source, first))
    assert (result.status, result.items_seen, result.items_inserted, result.items_failed) == (
        "partial",
        2,
        1,
        1,
    )

    # A newer 31 would push failed 29 out of count=2. Never make that request.
    async def forbidden(context: object):  # type: ignore[no-untyped-def]
        raise AssertionError("Incomplete bootstrap must fail before HTTP")
        yield

    retry = collector(source)
    monkeypatch.setattr(retry, "collect", forbidden)
    result = await run_collector(migrated_engine, source, retry)
    assert result.status == "failed" and result.items_seen == result.items_inserted == 0
    with Session(migrated_engine) as session:
        state = session.scalars(select(CollectorState)).one()
        assert state.cursor is None
        run = session.get(CollectionRun, result.run_id)
        assert run is not None and run.error_category == "cursor_overflow"
        assert session.scalars(select(RawItem.external_id)).all() == [f"{COMMUNITY}_30"]


async def test_sync_preserves_vk_identity_cursor_and_audit(migrated_engine: Engine) -> None:
    source = approved()
    run = await run_collector(migrated_engine, source, collector(source))
    with Session(migrated_engine) as session, session.begin():
        stored = session.scalars(select(Source)).one()
        source_id = stored.id
        state = session.get(CollectorState, source_id)
        assert state is not None
        original_cursor = state.cursor
        state.consecutive_failures = 2
        state.paused_until = datetime.now(UTC) + timedelta(hours=1)
        original_pause = state.paused_until
    disabled = next(s for s in load_config(Path("config")).registry.sources if s.key == source.key)
    with Session(migrated_engine) as session, session.begin():
        assert sync_source(session, disabled) == source_id
    with Session(migrated_engine) as session:
        state = session.get(CollectorState, source_id)
        assert state is not None and state.cursor == original_cursor
        assert state.consecutive_failures == 2 and state.paused_until == original_pause
        assert session.get(CollectionRun, run.run_id) is not None
        assert session.scalar(select(func.count()).select_from(RawItem)) == 4


@pytest.mark.parametrize("cancel", [False, True])
async def test_api_failure_and_cancellation_preserve_cursor(
    migrated_engine: Engine, cancel: bool
) -> None:
    source = approved()
    await run_collector(migrated_engine, source, collector(source))

    async def respond(request: httpx.Request) -> httpx.Response:
        if cancel:
            raise asyncio.CancelledError
        return httpx.Response(
            200, json={"error": {"error_code": 5, "error_msg": "synthetic private body"}}
        )

    env = Settings(
        _env_file=None,
        database_url=SecretStr("postgresql+psycopg://localhost/unused_test"),
        vk_access_token=SecretStr(TOKEN),
    )
    instance = build_collector(
        source, load_config(Path("config")), env, transport=httpx.MockTransport(respond)
    )
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await run_collector(migrated_engine, source, instance)
    else:
        result = await run_collector(migrated_engine, source, instance)
        assert result.status == "failed" and result.items_inserted == 0
    with Session(migrated_engine) as session:
        assert session.scalars(select(CollectorState)).one().cursor == {
            "version": 1,
            "community_id": COMMUNITY,
            "post_id": 30,
        }
        assert session.scalar(select(func.count()).select_from(RawItem)) == 4
    await run_collector(migrated_engine, source, collector(source))
    with Session(migrated_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(CollectionRun)
                .where(CollectionRun.status == "running")
            )
            == 0
        )
        categories = session.scalars(select(CollectionRun.error_category)).all()
        assert ("interrupted" if cancel else "authentication") in categories


async def test_bare_repost_deduplicates_by_original_text(migrated_engine: Engine) -> None:
    template = json.loads(FIXTURE)["response"]["items"][1]
    first, second = approved(), approved("vk_repetera")
    original = template | {"id": 30}
    repost = template | {
        "id": 40,
        "owner_id": -44923684,
        "text": "",
        "post_type": "copy",
        "copy_history": [{"id": 30, "owner_id": -COMMUNITY, "text": original["text"]}],
    }
    for source, item in ((first, original), (second, repost)):
        body = json.dumps({"response": {"count": 1, "items": [item]}}).encode()
        result = await run_collector(migrated_engine, source, collector(source, body))
        assert result.items_inserted == 1
    outcome = process_pending(
        migrated_engine, load_config(Path("config")), now=datetime(2026, 9, 19, 13, tzinfo=UTC)
    )
    assert outcome.failed == 0
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(Lead)) == 1
        assert session.scalar(select(func.count()).select_from(RawItem)) == 2

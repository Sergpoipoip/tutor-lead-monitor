from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from tutor_lead_monitor.application.collect import run_collector
from tutor_lead_monitor.application.process import process_item, process_pending
from tutor_lead_monitor.cli import main
from tutor_lead_monitor.collectors.fixture import FIXTURE_TIME, TEXTS, FixtureCollector
from tutor_lead_monitor.config import AppConfig, SourceConfig, load_config
from tutor_lead_monitor.db.locks import AlreadyRunning, source_lock
from tutor_lead_monitor.db.models import (
    CollectionRun,
    CollectorState,
    Lead,
    LeadOccurrence,
    RawItem,
)
from tutor_lead_monitor.db.pipeline import claim_pending
from tutor_lead_monitor.db.repositories import persist_page, sync_source
from tutor_lead_monitor.domain.models import CollectedItem, CollectionContext, CollectionPage

pytestmark = pytest.mark.integration
NOW = FIXTURE_TIME + timedelta(hours=1)


def app_config() -> AppConfig:
    return load_config(Path("config"))


async def collect_corpus(engine: Engine) -> None:
    for source in app_config().registry.sources:
        if source.kind != "fixture":
            continue
        dataset = "crosspost" if source.key == "fixture_crosspost" else "primary"
        result = await run_collector(engine, source, FixtureCollector(source.key, dataset=dataset))
        assert result.status == "succeeded"


def counts(engine: Engine) -> tuple[int, int, int]:
    with Session(engine) as session:
        totals = [
            int(session.scalar(select(func.count()).select_from(model)) or 0)
            for model in (RawItem, Lead, LeadOccurrence)
        ]
        return totals[0], totals[1], totals[2]


async def test_fixture_pipeline_and_rerun(migrated_engine: Engine) -> None:
    await collect_corpus(migrated_engine)
    outcome = process_pending(migrated_engine, app_config(), now=NOW)
    assert (outcome.processed, outcome.rejected, outcome.failed) == (8, 6, 0)
    initial = counts(migrated_engine)
    assert initial == (14, 6, 8)
    with Session(migrated_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(LeadOccurrence)
                .where(LeadOccurrence.match_method == "fuzzy_text")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(LeadOccurrence)
                .where(LeadOccurrence.match_method == "exact_text")
            )
            == 1
        )
        raw = session.scalars(
            select(RawItem).where(RawItem.processing_status == "processed")
        ).first()
        assert raw is not None and raw.text in TEXTS + tuple(
            FixtureCollector(dataset="crosspost").texts
        )
        assert raw.normalized_text and raw.exact_fingerprint
        assert "_tlm_processing" in raw.metadata_
        assert raw.metadata_["fixture_version"] == 1
    await collect_corpus(migrated_engine)
    again = process_pending(migrated_engine, app_config(), now=NOW)
    assert again.processed == again.rejected == again.failed == 0
    assert counts(migrated_engine) == initial
    with Session(migrated_engine) as session:
        runs = session.scalars(select(CollectionRun).order_by(CollectionRun.started_at)).all()
        assert len(runs) == 4  # One durable audit record per explicit invocation, including no-ops.
        assert [(r.items_seen, r.items_inserted, r.items_failed) for r in runs[-2:]] == [
            (0, 0, 0),
            (0, 0, 0),
        ]


class BrokenPageCollector(FixtureCollector):
    async def collect(self, context: CollectionContext) -> AsyncIterator[CollectionPage]:
        page = await anext(super().collect(context))
        yield replace(page, items=(page.items[0], replace(page.items[1], text="invalid\x00text")))


class InterruptedCollector(FixtureCollector):
    async def collect(self, context: CollectionContext) -> AsyncIterator[CollectionPage]:
        yield await anext(super().collect(context))
        raise RuntimeError("synthetic-secret-must-not-be-persisted")


class BrokenMiddleCollector(FixtureCollector):
    async def collect(self, context: CollectionContext) -> AsyncIterator[CollectionPage]:
        async for page in super().collect(context):
            yield replace(
                page,
                items=tuple(
                    replace(item, text="invalid\x00text")
                    if item.external_id == "fixture-v1-5"
                    else item
                    for item in page.items
                ),
            )


async def test_collection_continues_after_bad_item_without_skipping_it(
    migrated_engine: Engine,
    source_config: SourceConfig,
) -> None:
    result = await run_collector(migrated_engine, source_config, BrokenMiddleCollector())
    assert (result.items_seen, result.items_inserted, result.items_failed) == (13, 12, 1)
    assert counts(migrated_engine)[0] == 12
    with Session(migrated_engine) as session:
        state = session.scalars(select(CollectorState)).one()
        assert state.cursor == {"version": 1, "offset": 4}
    retry = await run_collector(migrated_engine, source_config, FixtureCollector())
    assert (retry.items_seen, retry.items_inserted, retry.items_failed) == (9, 1, 0)
    assert counts(migrated_engine)[0] == 13


@pytest.mark.parametrize("broken", [True, False])
async def test_collection_failure_checkpoints_and_recovery(
    migrated_engine: Engine,
    source_config: SourceConfig,
    broken: bool,
) -> None:
    collector = BrokenPageCollector() if broken else InterruptedCollector()
    result = await run_collector(migrated_engine, source_config, collector)
    assert result.status == "partial"
    assert (result.items_seen, result.items_inserted, result.items_failed) == (
        (2, 1, 1) if broken else (2, 2, 0)
    )
    with Session(migrated_engine) as session:
        state = session.scalars(select(CollectorState)).one()
        assert state.cursor == (None if broken else {"version": 1, "offset": 2})
        assert state.consecutive_failures == 1
        run = session.get(CollectionRun, result.run_id)
        assert run is not None and run.finished_at is not None
        assert run.error_message is None
        assert run.error_category and "synthetic-secret" not in run.error_category
    recovered = await run_collector(migrated_engine, source_config, FixtureCollector())
    assert recovered.status == "succeeded"
    assert counts(migrated_engine)[0] == len(TEXTS)
    with Session(migrated_engine) as session:
        state = session.scalars(select(CollectorState)).one()
        assert state.consecutive_failures == 0 and state.last_success_at is not None


async def test_processing_failure_rolls_back_only_one_item(
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await collect_corpus(migrated_engine)

    def fail_one(session: Session, raw: RawItem, config: AppConfig, now: datetime) -> str:
        status = process_item(session, raw, config, now)
        if raw.external_id == "fixture-v1-5":
            session.flush()
            raise ValueError("synthetic-secret-must-not-be-persisted")
        return status

    monkeypatch.setattr("tutor_lead_monitor.application.process.process_item", fail_one)
    result = process_pending(migrated_engine, app_config(), now=NOW)
    assert result.failed == 1 and result.processed + result.rejected == 13
    with Session(migrated_engine) as session:
        raw = session.scalars(select(RawItem).where(RawItem.processing_status == "failed")).one()
        assert raw.processing_error == "ValueError"
        assert raw.normalized_text is None and "_tlm_processing" not in raw.metadata_
        assert (
            session.scalar(
                select(func.count())
                .select_from(LeadOccurrence)
                .where(LeadOccurrence.raw_item_id == raw.id)
            )
            == 0
        )


async def test_concurrent_claims_skip_locked(migrated_engine: Engine) -> None:
    await collect_corpus(migrated_engine)
    with Session(migrated_engine) as a, a.begin(), Session(migrated_engine) as b, b.begin():
        first, second = claim_pending(a), claim_pending(b)
        assert first is not None and second is not None and first.id != second.id


async def test_source_lock_is_exclusive_and_released(
    migrated_engine: Engine,
    source_config: SourceConfig,
) -> None:
    with source_lock(migrated_engine, "fixture"):
        with pytest.raises(AlreadyRunning):
            await run_collector(migrated_engine, source_config, FixtureCollector())
        with source_lock(migrated_engine, "different_source"):
            pass
    assert (
        await run_collector(migrated_engine, source_config, FixtureCollector())
    ).status == "succeeded"


async def test_parallel_processors_do_not_create_duplicate_leads(migrated_engine: Engine) -> None:
    await collect_corpus(migrated_engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(process_pending, migrated_engine, app_config(), now=NOW) for _ in range(2)
        ]
        results = [future.result(timeout=15) for future in futures]
    assert sum(r.processed for r in results) == 8
    assert sum(r.failed for r in results) == 0
    assert counts(migrated_engine) == (14, 6, 8)


def insert_pair(engine: Engine, first: CollectedItem, second: CollectedItem) -> None:
    with Session(engine) as session, session.begin():
        for source, item in zip(
            [s for s in app_config().registry.sources if s.kind == "fixture"],
            (first, second),
            strict=True,
        ):
            source_id = sync_source(session, source)
            persist_page(
                session,
                source_id=source_id,
                source_key=source.key,
                page=CollectionPage((replace(item, source_key=source.key),), None, False),
            )


@pytest.mark.parametrize(
    "variant,expected,method",
    [
        ("url", 1, "exact_url"),
        ("fingerprint", 1, "exact_text"),
        ("near", 1, "fuzzy_text"),
        ("different_grade", 2, None),
        ("outside_window", 2, None),
        ("borderline", 2, None),
        ("different_contact", 2, None),
    ],
)
def test_cross_source_deduplication_strategies(
    migrated_engine: Engine,
    variant: str,
    expected: int,
    method: str | None,
) -> None:
    first = CollectedItem(
        "fixture",
        "first",
        "https://example.invalid/request?id=1&utm_source=a",
        FIXTURE_TIME,
        FIXTURE_TIME,
        TEXTS[-1],
    )
    second = replace(
        first,
        external_id="second",
        url="https://example.invalid/another",
        collected_at=FIXTURE_TIME + timedelta(seconds=1),
    )
    if variant == "url":
        second = replace(
            second, url="https://example.invalid/request?utm_source=b&id=1", text=TEXTS[4]
        )
    elif variant == "fingerprint":
        second = replace(second, text=first.text.upper())
    elif variant == "near":
        second = replace(second, text=first.text + " Пожалуйста.")
    elif variant == "different_grade":
        second = replace(second, text=first.text.replace("10 класс", "9 класс"))
    elif variant == "outside_window":
        second = replace(second, published_at=FIXTURE_TIME + timedelta(days=31))
    elif variant == "borderline":
        second = replace(
            second,
            text=first.text
            + " Только опытный преподаватель с подтвержденными результатами и рекомендациями.",
        )
    elif variant == "different_contact":
        first = replace(first, text=first.text + " +7 (999) 123-45-67")
        second = replace(second, text=second.text + " +7 (999) 765-43-21")
    insert_pair(migrated_engine, first, second)
    outcome = process_pending(migrated_engine, app_config(), now=NOW)
    assert outcome.failed == 0 and outcome.processed == 2
    assert counts(migrated_engine) == (2, expected, 2)
    if method:
        with Session(migrated_engine) as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(LeadOccurrence)
                    .where(LeadOccurrence.match_method == method)
                )
                == 1
            )


async def test_invalid_time_and_processing_limit(migrated_engine: Engine) -> None:
    with pytest.raises(ValueError):
        process_pending(migrated_engine, app_config(), now=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        process_pending(migrated_engine, app_config(), limit=0)


def test_cli_pipeline_and_retention_commands(
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_engine.url.render_as_string(hide_password=False))
    assert main(["pipeline", "--as-of", "2026-01-01T13:00:00Z"]) == 0
    assert counts(migrated_engine) == (14, 6, 8)
    assert main(["collect", "--source", "fixture"]) == 0
    assert main(["process"]) == 0
    assert main(["retention", "--dry-run", "--as-of", "2026-10-01T00:00:00Z"]) == 0
    assert counts(migrated_engine) == (14, 6, 8)
    assert main(["retention", "--apply", "--as-of", "2026-10-01T00:00:00Z"]) == 0
    assert counts(migrated_engine) == (0, 0, 0)
    assert "Ищу репетитора" not in capsys.readouterr().out


def test_cli_rejects_naive_time_before_collection(
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_engine.url.render_as_string(hide_password=False))
    assert main(["pipeline", "--as-of", "2026-01-01T13:00:00"]) == 1
    assert counts(migrated_engine) == (0, 0, 0)

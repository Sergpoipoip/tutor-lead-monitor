from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from tutor_lead_monitor.collectors.base import Collector
from tutor_lead_monitor.collectors.fixture import FIXTURE_TIME, FixtureCollector
from tutor_lead_monitor.domain.models import CollectionContext


async def test_fixture_is_deterministic_and_resumable() -> None:
    collector: Collector = FixtureCollector()
    context = CollectionContext(None, None, "run-a")
    pages = [page async for page in collector.collect(context)]
    repeated = [page async for page in collector.collect(replace(context, run_id="run-b"))]
    assert pages == repeated
    assert [len(page.items) for page in pages] == [2, 2]
    assert [page.has_more for page in pages] == [True, False]
    assert len({item.external_id for page in pages for item in page.items}) == 4
    resumed = [
        page async for page in collector.collect(replace(context, cursor=pages[0].next_cursor))
    ]
    assert resumed == pages[1:]
    assert [
        page async for page in collector.collect(replace(context, cursor=pages[-1].next_cursor))
    ] == []


async def test_since_can_yield_successful_empty_pages() -> None:
    pages = [
        page
        async for page in FixtureCollector().collect(CollectionContext(FIXTURE_TIME, None, "empty"))
    ]
    assert pages and all(not page.items for page in pages)
    assert pages[-1].next_cursor == {"version": 1, "offset": 4}


@pytest.mark.parametrize("offset", [-1, 5, "2", True])
async def test_invalid_cursor(offset: int | str | bool) -> None:
    with pytest.raises(ValueError):
        _ = [
            page
            async for page in FixtureCollector().collect(
                CollectionContext(None, {"version": 1, "offset": offset}, "bad")
            )
        ]


async def test_unknown_cursor_version() -> None:
    with pytest.raises(ValueError, match="version"):
        _ = [
            page
            async for page in FixtureCollector().collect(
                CollectionContext(None, {"version": 2, "offset": 0}, "bad")
            )
        ]


async def test_utc_contract() -> None:
    pages = [
        page async for page in FixtureCollector().collect(CollectionContext(None, None, "utc"))
    ]
    item = pages[0].items[0]
    converted = replace(
        item, collected_at=datetime(2026, 1, 1, 14, tzinfo=timezone(timedelta(hours=2)))
    )
    assert converted.collected_at == FIXTURE_TIME
    assert converted.collected_at.tzinfo is UTC
    with pytest.raises(ValueError, match="Timezone-aware"):
        replace(item, collected_at=datetime(2026, 1, 1))


def test_positive_page_size() -> None:
    with pytest.raises(ValueError):
        FixtureCollector(page_size=0)

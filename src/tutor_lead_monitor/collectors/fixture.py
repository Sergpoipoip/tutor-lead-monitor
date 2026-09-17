from collections.abc import AsyncIterator
from datetime import UTC, datetime

from tutor_lead_monitor.domain.models import CollectedItem, CollectionContext, CollectionPage

FIXTURE_TIME = datetime(2026, 1, 1, 12, tzinfo=UTC)
TEXTS = (
    "Ищу репетитора по литературе, 10 класс, подготовка к ЕГЭ онлайн.",
    "Я репетитор по литературе, набираю учеников.",
    "Школа ищет учителя литературы в штат.",
    "Посоветуйте, кто может помочь дочери с литературой?",
)


class FixtureCollector:
    """Synthetic, fixed-time dataset; cursor offsets refer to dataset v1."""

    def __init__(self, key: str = "fixture", page_size: int = 2) -> None:
        if page_size < 1:
            raise ValueError("Page size must be positive")
        self.key = key
        self.page_size = page_size

    async def collect(self, context: CollectionContext) -> AsyncIterator[CollectionPage]:
        offset = (context.cursor or {}).get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= len(TEXTS):
            raise ValueError("Invalid fixture cursor offset")
        if context.cursor and context.cursor.get("version") != 1:
            raise ValueError("Unsupported fixture cursor version")
        while offset < len(TEXTS):
            end = min(offset + self.page_size, len(TEXTS))
            items = tuple(
                CollectedItem(
                    source_key=self.key,
                    external_id=f"fixture-v1-{index + 1}",
                    url=f"https://example.invalid/fixtures/{index + 1}",
                    published_at=FIXTURE_TIME,
                    collected_at=FIXTURE_TIME,
                    text=TEXTS[index],
                    metadata={"fixture_version": 1},
                )
                for index in range(offset, end)
                if context.since is None or context.since < FIXTURE_TIME
            )
            offset = end
            yield CollectionPage(items, {"version": 1, "offset": offset}, offset < len(TEXTS))

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from tutor_lead_monitor.domain.models import CollectedItem, CollectionContext, CollectionPage

FIXTURE_TIME = datetime(2026, 1, 1, 12, tzinfo=UTC)
TEXTS = (
    "Ищу репетитора по литературе, 10 класс, подготовка к ЕГЭ онлайн.",
    "Я репетитор по литературе, набираю учеников.",
    "Школа ищет учителя литературы в штат.",
    "Посоветуйте, кто может помочь дочери с литературой?",
    "Нужен репетитор по литературе, 9 класс, ОГЭ, онлайн. Бюджет до 2000 руб за час.",
    "Ищу преподавателя по литературе для подготовки к олимпиаде ВСОШ, 11 класс, онлайн.",
    "Ищу репетитора, русский и литература, 10 класс, ЕГЭ, онлайн, до 2000 руб.",
    "Литература - интересный предмет, размышляю об учебе.",
    "Образовательный центр объявляет набор на курс литературы.",
    "Опубликовано расписание экзаменов по литературе.",
    "Ищу репетитора по математике, 8 класс.",
    "Ищу репетитора по литературе, 10 класс, подготовка к ЕГЭ онлайн.",
    "Ищу репетитора по литературе для дочери, 10 класс, подготовка к ЕГЭ, "
    "занятия онлайн вечером два раза в неделю, город Москва.",
)
CROSSPOST_TEXTS = (
    "Ищу репетитора по литературе для дочери, 10 класс, подготовка к ЕГЭ, "
    "занятия онлайн вечером два раза в неделю, город Москва, пожалуйста.",
)


class FixtureCollector:
    """Synthetic, fixed-time dataset; cursor offsets refer to dataset v1."""

    def __init__(self, key: str = "fixture", page_size: int = 2, dataset: str = "primary") -> None:
        if page_size < 1:
            raise ValueError("Page size must be positive")
        self.key = key
        self.page_size = page_size
        if dataset not in {"primary", "crosspost"}:
            raise ValueError("Unknown fixture dataset")
        self.dataset = dataset
        self.texts: tuple[str, ...] = TEXTS if dataset == "primary" else CROSSPOST_TEXTS

    async def collect(self, context: CollectionContext) -> AsyncIterator[CollectionPage]:
        offset = (context.cursor or {}).get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= len(self.texts):
            raise ValueError("Invalid fixture cursor offset")
        if context.cursor and context.cursor.get("version") != 1:
            raise ValueError("Unsupported fixture cursor version")
        if context.cursor and context.cursor.get("dataset", "primary") != self.dataset:
            raise ValueError("Fixture cursor dataset mismatch")
        while offset < len(self.texts):
            end = min(offset + self.page_size, len(self.texts))
            items = tuple(
                CollectedItem(
                    source_key=self.key,
                    external_id=f"fixture-v1-{index + 1}",
                    url=(
                        f"https://example.invalid/fixtures/{index + 1}"
                        if self.dataset == "primary"
                        else f"https://example.invalid/crosspost/fixtures/{index + 1}"
                    ),
                    published_at=FIXTURE_TIME,
                    collected_at=FIXTURE_TIME,
                    text=self.texts[index],
                    metadata={"fixture_version": 1},
                )
                for index in range(offset, end)
                if context.since is None or context.since < FIXTURE_TIME
            )
            offset = end
            cursor = {"version": 1, "offset": offset}
            if self.dataset == "crosspost":
                yield CollectionPage(
                    items, {**cursor, "dataset": self.dataset}, offset < len(self.texts)
                )
            else:
                yield CollectionPage(items, cursor, offset < len(self.texts))

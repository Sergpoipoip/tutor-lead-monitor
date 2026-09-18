from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from tutor_lead_monitor.domain.models import utc


class SearchFailure(StrEnum):
    AUTHORIZATION = "authorization"
    RATE_LIMIT = "rate_limit"
    SERVER = "provider_server"
    TRANSPORT = "transport"
    MALFORMED = "malformed_response"
    REQUEST = "provider_request"


class SearchError(Exception):
    def __init__(self, category: SearchFailure, *, retry_after: int | None = None) -> None:
        self.category = category
        self.retry_after = retry_after
        super().__init__(category.value)


@dataclass(frozen=True, repr=False)
class SearchResult:
    url: str
    title: str
    passages: tuple[str, ...]
    rank: int
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.published_at is not None:
            object.__setattr__(self, "published_at", utc(self.published_at))


@dataclass(frozen=True, repr=False)
class SearchPage:
    results: tuple[SearchResult, ...]


class SearchProvider(Protocol):
    async def search(self, query: str, *, limit: int) -> SearchPage:
        """Return only the top page. Implementations must not fetch destination URLs."""
        ...

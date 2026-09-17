from collections.abc import AsyncIterator
from typing import Protocol

from tutor_lead_monitor.domain.models import CollectionContext, CollectionPage


class Collector(Protocol):
    @property
    def key(self) -> str: ...

    def collect(self, context: CollectionContext) -> AsyncIterator[CollectionPage]:
        """Yield pages; callers persist a page before committing its cursor.

        Implementations use async generators. Metadata must be JSON-compatible
        and contain no credentials. Empty results are successful collections.
        """
        ...

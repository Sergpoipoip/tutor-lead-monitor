from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit

from tutor_lead_monitor.config import SearchQueryConfig, WebSearchOptions
from tutor_lead_monitor.domain.models import CollectedItem, CollectionContext, CollectionPage
from tutor_lead_monitor.search.base import SearchError, SearchFailure, SearchProvider
from tutor_lead_monitor.search.urls import external_id, public_url


class WebSearchCollector:
    def __init__(
        self,
        key: str,
        provider: SearchProvider,
        queries: tuple[SearchQueryConfig, ...],
        options: WebSearchOptions,
    ) -> None:
        self.key, self.provider, self.queries, self.options = key, provider, queries, options

    async def collect(self, context: CollectionContext) -> AsyncIterator[CollectionPage]:
        # Deliberately ignore any saved cursor/high-water mark: revisit top results every run.
        if len(self.queries) > self.options.max_requests_per_run:
            raise SearchError(SearchFailure.REQUEST)
        for index, query in enumerate(self.queries):
            page = await self.provider.search(query.text, limit=self.options.results_per_query)
            collected = datetime.now(UTC)
            items: list[CollectedItem] = []
            for result in page.results[: self.options.results_per_query]:
                canonical = public_url(result.url)
                evidence = "\n".join(s for s in (result.title, *result.passages) if s)[:4096]
                if canonical is None or not evidence.strip():
                    continue
                items.append(
                    CollectedItem(
                        source_key=self.key,
                        external_id=external_id(canonical),
                        url=result.url,
                        published_at=result.published_at,
                        collected_at=collected,
                        text=evidence,
                        metadata={
                            "query_group": query.group,
                            "rank": result.rank,
                            "domain": urlsplit(canonical).hostname,
                            "provider": self.options.provider,
                            "evidence_kind": "search_result_snippet",
                        },
                    )
                )
            yield CollectionPage(tuple(items), None, index < len(self.queries) - 1)

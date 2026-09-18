from datetime import UTC, datetime

import httpx

from tutor_lead_monitor.collectors.base import Collector
from tutor_lead_monitor.collectors.fixture import FixtureCollector
from tutor_lead_monitor.collectors.web_search import WebSearchCollector
from tutor_lead_monitor.config import (
    AppConfig,
    FixtureOptions,
    Settings,
    SourceConfig,
    WebSearchOptions,
)
from tutor_lead_monitor.search.yandex import YandexSearchProvider


def build_collector(
    source: SourceConfig,
    config: AppConfig,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Collector:
    if not source.enabled or source.policy_status != "approved":
        raise ValueError("Source must be enabled and approved")
    expiry = source.operations.authorization_expires_at
    if expiry is not None and expiry <= datetime.now(UTC):
        raise ValueError("Source authorization has expired")
    if source.kind == "fixture":
        options = FixtureOptions.model_validate(source.config)
        return FixtureCollector(source.key, options.page_size, options.dataset)
    if source.kind == "web_search" and source.access_method == "official_api":
        search_options = WebSearchOptions.model_validate(source.config)
        api_key, folder_id = settings.yandex_access()
        return WebSearchCollector(
            source.key,
            YandexSearchProvider(api_key, folder_id, transport=transport),
            tuple(config.queries.searches[key] for key in search_options.query_ids),
            search_options,
        )
    raise ValueError("Unsupported collector kind or access method")

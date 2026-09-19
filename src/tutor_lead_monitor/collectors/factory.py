import httpx

from tutor_lead_monitor.collectors.base import Collector
from tutor_lead_monitor.collectors.fixture import FixtureCollector
from tutor_lead_monitor.collectors.vk import VKCollector
from tutor_lead_monitor.collectors.web_search import WebSearchCollector
from tutor_lead_monitor.config import (
    AppConfig,
    FixtureOptions,
    Settings,
    SourceConfig,
    VKOptions,
    WebSearchOptions,
)
from tutor_lead_monitor.search.yandex import YandexSearchProvider
from tutor_lead_monitor.vk_api import VKClient, VKError, VKFailure


def build_collector(
    source: SourceConfig,
    config: AppConfig,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Collector:
    if source.kind == "vk_api":
        if not source.enabled or source.policy_status != "approved":
            raise VKError(VKFailure.ACCESS)
        try:
            source.check_authorization()
            # Revalidate even if a caller used model_copy (which skips validation).
            source = SourceConfig.model_validate(source.model_dump())
            vk_options = VKOptions.model_validate(source.config)
            token = settings.vk_access()
        except ValueError:
            raise VKError(VKFailure.CONFIGURATION) from None
        return VKCollector(
            source.key,
            VKClient(token, api_version=vk_options.api_version, transport=transport),
            vk_options,
        )
    if not source.enabled or source.policy_status != "approved":
        raise ValueError("Source must be enabled and approved")
    source.check_authorization()
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

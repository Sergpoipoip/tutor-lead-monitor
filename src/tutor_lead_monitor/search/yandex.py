"""Yandex Search API v2 synchronous XML adapter. No SDK, smart snippets, or crawling."""

import asyncio
import base64
import binascii
import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import httpx
from pydantic import SecretStr, ValidationError

from tutor_lead_monitor.config import SearchQueryConfig
from tutor_lead_monitor.domain.models import utc
from tutor_lead_monitor.search.base import SearchError, SearchFailure, SearchPage, SearchResult
from tutor_lead_monitor.search.urls import public_url

ENDPOINT = "https://searchapi.api.cloud.yandex.net/v2/web/search"
MAX_RESPONSE_BYTES = 1_048_576
MAX_XML_BYTES = 524_288
TIMEOUT = httpx.Timeout(connect=10, read=30, write=10, pool=10)


def retry_delay(value: str | None, now: datetime) -> int | None:
    """Only bounded Retry-After delays; no automatic retries in this milestone."""
    if value is None or len(value) > 100:
        return None
    try:
        seconds = (
            int(value)
            if value.isascii() and value.isdigit()
            else int((utc(parsedate_to_datetime(value)) - now).total_seconds())
        )
        return seconds if 0 <= seconds <= 300 else None
    except (ValueError, TypeError, OverflowError):
        return None


def publication_time(value: str) -> datetime | None:
    # Accept explicit publication metadata only, never modtime, crawl time or snippet dates.
    try:
        return utc(datetime.fromisoformat(value))
    except ValueError:
        try:
            return utc(parsedate_to_datetime(value))
        except (ValueError, TypeError, OverflowError):
            return None


def node_text(node: ET.Element | None) -> str:
    return "" if node is None else "".join(node.itertext())


def parse_response(payload: bytes, limit: int) -> SearchPage:
    try:
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError
        envelope = json.loads(payload)
        if not isinstance(envelope, dict):
            raise ValueError
        encoded = envelope.get("rawData")
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_XML_BYTES + 2) // 3):
            raise ValueError
        xml = base64.b64decode(encoded, validate=True)
        if len(xml) > MAX_XML_BYTES:
            raise ValueError
        decoded = xml.decode("utf-8-sig")
        if "\x00" in decoded or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", decoded, re.I):
            raise ValueError
        root = ET.fromstring(decoded)
        if root.tag != "yandexsearch" or sum(1 for _ in root.iter()) > 10000:
            raise ValueError
    except (ValueError, TypeError, RecursionError, binascii.Error, ET.ParseError):
        raise SearchError(SearchFailure.MALFORMED) from None
    error = root.find(".//error")
    if error is not None:
        code = error.get("code")
        if code == "15":
            return SearchPage(())
        if code in {"31", "33", "42"}:
            category = SearchFailure.AUTHORIZATION
        elif code in {"32", "55"}:
            category = SearchFailure.RATE_LIMIT
        elif code == "20":
            category = SearchFailure.SERVER
        else:
            category = SearchFailure.REQUEST
        raise SearchError(category)
    results: list[SearchResult] = []
    for rank, doc in enumerate(root.findall(".//group/doc")[:limit], 1):
        url = node_text(doc.find("url"))
        if public_url(url) is None:
            continue
        title = node_text(doc.find("title"))[:600]
        passages = tuple(node_text(p)[:1000] for p in doc.findall("passages/passage")[:5])
        # Optional explicit publication metadata. These fields are not guaranteed by v2.
        published = node_text(doc.find("published-at")) or node_text(doc.find("pubDate"))
        results.append(SearchResult(url, title, passages, rank, publication_time(published)))
    return SearchPage(tuple(results))


class YandexSearchProvider:
    def __init__(
        self,
        api_key: SecretStr,
        folder_id: SecretStr,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key, self._folder_id, self._transport = api_key, folder_id, transport

    async def search(self, query: str, *, limit: int) -> SearchPage:
        try:
            validated = SearchQueryConfig(group="request", text=query)
            if not 1 <= limit <= 20:
                raise ValueError
        except (ValidationError, ValueError):
            raise SearchError(SearchFailure.REQUEST) from None
        body = {
            "query": {
                "searchType": "SEARCH_TYPE_RU",
                "queryText": validated.text,
                "familyMode": "FAMILY_MODE_STRICT",
                "page": "0",
                "fixTypoMode": "FIX_TYPO_MODE_OFF",
            },
            "sortSpec": {"sortMode": "SORT_MODE_BY_TIME", "sortOrder": "SORT_ORDER_DESC"},
            "groupSpec": {
                "groupMode": "GROUP_MODE_FLAT",
                "groupsOnPage": str(limit),
                "docsInGroup": "1",
            },
            "maxPassages": "5",
            "region": "225",
            "l10n": "LOCALIZATION_RU",
            "folderId": self._folder_id.get_secret_value(),
            "responseFormat": "FORMAT_XML",
            "period": "PERIOD_2_WEEKS",
        }
        headers = {
            "Authorization": f"Api-Key {self._api_key.get_secret_value()}",
            "x-data-logging-enabled": "false",
            "Accept-Encoding": "identity",
        }
        try:
            async with (
                asyncio.timeout(45),
                httpx.AsyncClient(
                    transport=self._transport,
                    timeout=TIMEOUT,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream("POST", ENDPOINT, headers=headers, json=body) as response,
            ):
                code = response.status_code
                delay = retry_delay(response.headers.get("Retry-After"), datetime.now(UTC))
                if code in {401, 403}:
                    raise SearchError(SearchFailure.AUTHORIZATION)
                if code == 429:
                    raise SearchError(SearchFailure.RATE_LIMIT, retry_after=delay)
                if code >= 500:
                    raise SearchError(SearchFailure.SERVER, retry_after=delay)
                if code != 200:
                    raise SearchError(SearchFailure.REQUEST)
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise SearchError(SearchFailure.MALFORMED)
                length = response.headers.get("Content-Length")
                if length is not None and (
                    len(length) > 10
                    or not length.isascii()
                    or not length.isdigit()
                    or int(length) > MAX_RESPONSE_BYTES
                ):
                    raise SearchError(SearchFailure.MALFORMED)
                content = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise SearchError(SearchFailure.MALFORMED)
                return parse_response(bytes(content), limit)
        except (httpx.HTTPError, TimeoutError):
            raise SearchError(SearchFailure.TRANSPORT) from None

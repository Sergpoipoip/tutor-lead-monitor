import base64
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from tutor_lead_monitor.collectors.factory import build_collector
from tutor_lead_monitor.config import (
    ConfigError,
    SearchQueryConfig,
    Settings,
    SourceConfig,
    WebSearchOptions,
    load_config,
)
from tutor_lead_monitor.domain.models import CollectionContext
from tutor_lead_monitor.search.base import SearchError
from tutor_lead_monitor.search.urls import external_id, public_url
from tutor_lead_monitor.search.yandex import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    MAX_XML_BYTES,
    YandexSearchProvider,
    parse_response,
    publication_time,
    retry_delay,
)

KEY = "synthetic_api_key_not_real"
FOLDER = "syntheticfolder00001"
XML = Path("tests/fixtures/yandex_search.xml").read_bytes()


def payload(xml: bytes = XML) -> bytes:
    return json.dumps({"rawData": base64.b64encode(xml).decode()}).encode()


def settings(key: str = KEY, folder: str = FOLDER) -> Settings:
    return Settings(
        database_url=SecretStr("postgresql+psycopg://localhost/unused_test"),
        yandex_search_api_key=SecretStr(key),
        yandex_search_folder_id=SecretStr(folder),
        _env_file=None,
    )


def approved() -> SourceConfig:
    source = load_config(Path("config")).registry.sources[-1]
    return SourceConfig.model_validate(
        source.model_dump() | {"enabled": True, "policy_status": "approved"}
    )


async def test_request_contract_and_evidence(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert str(request.url) == ENDPOINT and request.method == "POST"
        assert request.headers["Authorization"] == f"Api-Key {KEY}"
        assert request.headers["x-data-logging-enabled"] == "false"
        assert "x-genesis-full-text" not in request.headers
        assert request.extensions["timeout"] == {"connect": 10, "read": 30, "write": 10, "pool": 10}
        body = json.loads(request.content)
        assert body == {
            "query": {
                "searchType": "SEARCH_TYPE_RU",
                "queryText": '"ищу репетитора" литература',
                "familyMode": "FAMILY_MODE_STRICT",
                "page": "0",
                "fixTypoMode": "FIX_TYPO_MODE_OFF",
            },
            "sortSpec": {"sortMode": "SORT_MODE_BY_TIME", "sortOrder": "SORT_ORDER_DESC"},
            "groupSpec": {"groupMode": "GROUP_MODE_FLAT", "groupsOnPage": "10", "docsInGroup": "1"},
            "maxPassages": "5",
            "region": "225",
            "l10n": "LOCALIZATION_RU",
            "folderId": FOLDER,
            "responseFormat": "FORMAT_XML",
            "period": "PERIOD_2_WEEKS",
        }
        return httpx.Response(200, content=payload())

    provider = YandexSearchProvider(
        SecretStr(KEY), SecretStr(FOLDER), transport=httpx.MockTransport(respond)
    )
    page = await provider.search('"ищу репетитора" литература', limit=10)
    assert len(calls) == 1 and len(page.results) == 4
    assert page.results[0].title == "Ищу репетитора — littérature"
    assert page.results[0].passages[0] == "Помочь с литературой & сочинением. 😀"
    assert page.results[0].published_at == datetime(2026, 9, 18, 10, tzinfo=UTC)
    assert page.results[1].published_at is None
    assert page.results[2].published_at == page.results[0].published_at
    assert all(secret not in caplog.text + repr(provider) + repr(page) for secret in (KEY, FOLDER))


@pytest.mark.parametrize(
    "code,category",
    [
        (401, "authorization"),
        (403, "authorization"),
        (429, "rate_limit"),
        (500, "provider_server"),
        (503, "provider_server"),
        (400, "provider_request"),
        (422, "provider_request"),
        (302, "provider_request"),
    ],
)
async def test_http_errors_never_expose_payload_or_retry(code: int, category: str) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            code,
            text=KEY + FOLDER + "private-query",
            headers={"Retry-After": "12", "Location": "https://example.invalid/destination"},
        )

    provider = YandexSearchProvider(
        SecretStr(KEY), SecretStr(FOLDER), transport=httpx.MockTransport(respond)
    )
    with pytest.raises(SearchError) as caught:
        await provider.search("литература", limit=1)
    assert str(caught.value) == category and calls == 1
    assert caught.value.retry_after == (12 if code in {429, 500, 503} else None)


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError]
)
async def test_transport_failures_are_sanitized(error: type[httpx.HTTPError]) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise error(KEY + FOLDER)

    provider = YandexSearchProvider(
        SecretStr(KEY), SecretStr(FOLDER), transport=httpx.MockTransport(fail)
    )
    with pytest.raises(SearchError, match="^transport$") as caught:
        await provider.search("литература", limit=1)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"[]",
        b"{}",
        b'{"rawData": null}',
        b'{"rawData":"%%%"}',
        payload(b"<broken>"),
        payload(b"<html/>"),
        payload(b'<!DOCTYPE yandexsearch [<!ENTITY x "private">]><yandexsearch>&x;</yandexsearch>'),
        payload(b'<!ENTITY x SYSTEM "file:///private"><yandexsearch/>'),
        payload("<!DOCTYPE yandexsearch><yandexsearch/>".encode("utf-16")),
        payload(b"x" * (MAX_XML_BYTES + 1)),
        b" " * (MAX_RESPONSE_BYTES + 1),
    ],
)
def test_malformed_payloads_rejected(body: bytes) -> None:
    with pytest.raises(SearchError, match="^malformed_response$"):
        parse_response(body, 20)


@pytest.mark.parametrize(
    "headers,content",
    [
        ({"Content-Length": str(MAX_RESPONSE_BYTES + 1)}, b""),
        ({"Content-Encoding": "gzip"}, b""),
        ({}, b" " * (MAX_RESPONSE_BYTES + 1)),
    ],
)
async def test_network_body_bounds(headers: dict[str, str], content: bytes) -> None:
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for i in range(0, len(content), 65536):
                yield content[i : i + 65536]

    provider = YandexSearchProvider(
        SecretStr(KEY),
        SecretStr(FOLDER),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers=headers, stream=Stream())
        ),
    )
    with pytest.raises(SearchError, match="^malformed_response$"):
        await provider.search("литература", limit=1)


@pytest.mark.parametrize(
    "code,expected",
    [
        (15, None),
        (42, "authorization"),
        (32, "rate_limit"),
        (55, "rate_limit"),
        (20, "provider_server"),
        (1, "provider_request"),
        (100, "provider_request"),
    ],
)
def test_xml_error_categories(code: int, expected: str | None) -> None:
    data = payload(
        (
            f'<yandexsearch><response><error code="{code}">private</error>'
            "</response></yandexsearch>"
        ).encode()
    )
    if expected is None:
        assert parse_response(data, 10).results == ()
    else:
        with pytest.raises(SearchError, match=f"^{expected}$"):
            parse_response(data, 10)


def test_empty_xml_and_optional_fields() -> None:
    assert parse_response(payload(b"<yandexsearch/>"), 10).results == ()
    assert len(parse_response(payload(), 1).results) == 1
    assert parse_response(payload(), 10).results[-1].title == ""


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "HTTPS://Example.invalid:443/a?ref=1&x=%2f&x=2&utm_source=q#f",
            "https://example.invalid/a?ref=1&x=%2f&x=2",
        ),
        ("https://example.invalid/a?b=2&a=1&ysclid=q", "https://example.invalid/a?b=2&a=1"),
        ("https://пример.рф/путь?id=1", "https://xn--e1afmkfd.xn--p1ai/путь?id=1"),
        ("javascript:alert(1)", None),
        ("http://127.0.0.1/a", None),
        ("http://[::1]/", None),
        ("http://10.0.0.1/", None),
        ("http://localhost/", None),
        ("http://a.local/", None),
        ("https://user:password@example.invalid/a", None),
        ("https://example.invalid:bad/a", None),
        ("https://example.invalid/a\n", None),
    ],
)
def test_conservative_public_urls(url: str, expected: str | None) -> None:
    assert public_url(url) == expected
    if expected:
        assert external_id(expected) == external_id(public_url(url) or "")
        assert len(external_id(expected)) == 64


@pytest.mark.parametrize("value", ["2026-09-18", "2026-09-18T10:00:00", "yesterday", ""])
def test_unreliable_publication_dates_remain_unknown(value: str) -> None:
    assert publication_time(value) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("12", 12),
        ("0", 0),
        ("-1", None),
        ("301", None),
        ("nonsense", None),
        ("Fri, 18 Sep 2026 10:00:12 GMT", 12),
    ],
)
def test_bounded_retry_after(value: str, expected: int | None) -> None:
    assert retry_delay(value, datetime(2026, 9, 18, 10, tzinfo=UTC)) == expected


@pytest.mark.parametrize("text", ["", "  ", "я" * 401, "word " * 41, "a\nb"])
def test_bad_queries(text: str) -> None:
    with pytest.raises(ValidationError):
        SearchQueryConfig(group="explicit_literature", text=text)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "other"},
        {"results_per_query": 21},
        {"max_requests_per_run": 11},
        {"max_requests_per_run": 1},
        {"query_ids": ["a", "a"]},
        {"page": 1},
    ],
)
def test_search_options_fail_closed(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        WebSearchOptions.model_validate({"provider": "yandex", "query_ids": ["a", "b"]} | changes)


@pytest.mark.parametrize(
    "key,folder",
    [
        ("", FOLDER),
        (KEY, ""),
        ("REPLACE_WITH_YANDEX_SEARCH_API_KEY", FOLDER),
        (KEY + "\n", FOLDER),
        (KEY, "bad-folder"),
    ],
)
def test_credentials_are_lazy_and_sanitized(key: str, folder: str) -> None:
    value = settings(key, folder)
    with pytest.raises(ConfigError, match="^Configure valid Yandex Search credentials$"):
        value.yandex_access()
    assert value.model_dump().get("yandex_search_folder_id") is None
    assert value.model_dump().get("yandex_search_api_key") is None
    fixture = load_config(Path("config")).registry.sources[0]
    assert build_collector(fixture, load_config(Path("config")), value).key == "fixture"


async def test_collector_revisits_queries_without_cursor_or_destinations() -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert str(request.url) == ENDPOINT
        return httpx.Response(200, content=payload())

    config = load_config(Path("config"))
    collector = build_collector(
        approved(), config, settings(), transport=httpx.MockTransport(respond)
    )
    before = datetime.now(UTC)
    pages = [
        page async for page in collector.collect(CollectionContext(before, {"offset": 100}, "test"))
    ]
    assert len(calls) == len(pages) == 3
    assert all(page.next_cursor is None for page in pages)
    assert [p.has_more for p in pages] == [True, True, False]
    assert len(pages[0].items) == 3  # URL-less/private/empty evidence ignored.
    first = pages[0].items[0]
    assert first.external_id == pages[1].items[0].external_id
    assert first.url == "https://example.invalid/request?ref=42&utm_source=test#part"
    assert before <= first.collected_at <= datetime.now(UTC)
    assert first.collected_at.tzinfo is UTC
    assert first.metadata["evidence_kind"] == "search_result_snippet"
    assert set(first.metadata) == {"query_group", "rank", "domain", "provider", "evidence_kind"}
    assert KEY not in str(first.metadata) and FOLDER not in str(first.metadata)


@pytest.mark.parametrize("mode", ["disabled", "pending", "expired"])
def test_policy_gates_before_network(mode: str) -> None:
    config = load_config(Path("config"))
    source = approved()
    if mode == "disabled":
        source = source.model_copy(update={"enabled": False})
    elif mode == "pending":
        source = source.model_copy(update={"policy_status": "pending"})
    else:
        source = source.model_copy(
            update={
                "operations": source.operations.model_copy(
                    update={"authorization_expires_at": datetime.now(UTC) - timedelta(seconds=1)}
                )
            }
        )
    with pytest.raises(ValueError):
        build_collector(source, config, settings())


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "unknown"},
        {"access_method": "public_web"},
        {"credential_env": "OTHER_KEY"},
        {"enabled": True, "policy_status": "pending"},
    ],
)
def test_source_contract_rejects_unsupported_access(changes: dict[str, object]) -> None:
    data = load_config(Path("config")).registry.sources[-1].model_dump()
    with pytest.raises(ValidationError):
        SourceConfig.model_validate(data | changes)


def test_missing_credentials_and_unknown_query_references() -> None:
    from tutor_lead_monitor.config import AppConfig

    config = load_config(Path("config"))
    no_credentials = Settings(
        database_url=SecretStr("postgresql+psycopg://localhost/unused_test"),
        yandex_search_api_key=None,
        yandex_search_folder_id=None,
        _env_file=None,
    )
    with pytest.raises(ConfigError):
        build_collector(approved(), config, no_credentials)
    data = config.model_dump()
    data["registry"]["sources"][-1]["config"]["query_ids"] = ["not_in_catalog"]
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost./",
        "http://127.000.000.1/",
        "https://-bad.invalid/",
        "https://example.invalid:0/",
    ],
)
def test_nonpublic_or_unusable_host_spellings(url: str) -> None:
    assert public_url(url) is None

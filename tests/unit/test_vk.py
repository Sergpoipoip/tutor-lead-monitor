import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from tutor_lead_monitor.collectors.factory import build_collector
from tutor_lead_monitor.collectors.vk import VKCollector
from tutor_lead_monitor.config import (
    VK_COMMUNITIES,
    ConfigError,
    Settings,
    SourceConfig,
    VKOptions,
    load_config,
)
from tutor_lead_monitor.domain.models import CollectionContext, CollectionPage, JSONValue
from tutor_lead_monitor.vk_api import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    MAX_TEXT_CHARS,
    VKClient,
    VKError,
    VKFailure,
    parse_wall,
)

TOKEN = "synthetic-vk-token-not-real"
COMMUNITY = 218494134
FIXTURE = Path("tests/fixtures/vk_wall.json").read_bytes()


def approved_source() -> SourceConfig:
    source = next(
        s for s in load_config(Path("config")).registry.sources if s.key == "vk_ishchu_repetitora"
    )
    data = source.model_dump()
    data.update(enabled=True, policy_status="approved")
    data["policy"].update(reviewer="Synthetic reviewer", reviewed_at="2026-09-19T00:00:00Z")
    return SourceConfig.model_validate(data)


def settings(token: str | None = TOKEN) -> Settings:
    return Settings(
        database_url=SecretStr("postgresql+psycopg://localhost/unused_test"),
        vk_access_token=SecretStr(token) if token is not None else None,
        _env_file=None,
    )


def post(post_id: int, *, pinned: bool = False) -> dict[str, JSONValue]:
    return {
        "id": post_id,
        "owner_id": -COMMUNITY,
        "date": 1789819200,
        "text": "Ищу репетитора по литературе. Синтетический пример.",
        "post_type": "post",
        "is_pinned": int(pinned),
    }


def wall(*items: dict[str, JSONValue]) -> bytes:
    return json.dumps({"response": {"count": len(items), "items": items}}).encode()


def cursor(post_id: int) -> dict[str, JSONValue]:
    return {"version": 1, "community_id": COMMUNITY, "post_id": post_id}


async def collect(
    body: bytes, mark: dict[str, JSONValue] | None = None, initial: int = 20
) -> CollectionPage:
    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(body)
        count = int(parse_qs(request.content.decode())["count"][0])
        payload["response"]["items"] = payload["response"]["items"][:count]
        return httpx.Response(200, json=payload)

    client = VKClient(
        SecretStr(TOKEN),
        api_version="5.199",
        transport=httpx.MockTransport(respond),
    )
    collector = VKCollector(
        "vk_ishchu_repetitora",
        client,
        VKOptions(
            community_id=COMMUNITY,
            screen_name="ishchu_repetitora",
            initial_posts=initial,
        ),
    )
    pages = [page async for page in collector.collect(CollectionContext(None, mark, "test"))]
    assert len(pages) == 1 and not pages[0].has_more
    return pages[0]


async def test_post_contract_minimal_evidence_and_no_secret_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "POST" and str(request.url) == ENDPOINT
        assert TOKEN not in str(request.url) + str(request.headers)
        assert parse_qs(request.content.decode()) == {
            "access_token": [TOKEN],
            "v": ["5.199"],
            "owner_id": [str(-COMMUNITY)],
            "count": ["20"],
            "offset": ["0"],
            "filter": ["all"],
            "extended": ["0"],
        }
        assert request.extensions["timeout"] == {"connect": 10, "read": 30, "write": 10, "pool": 10}
        return httpx.Response(200, content=FIXTURE)

    config = load_config(Path("config"))
    collector = build_collector(
        approved_source(), config, settings(), transport=httpx.MockTransport(respond)
    )
    before = datetime.now(UTC)
    page = [p async for p in collector.collect(CollectionContext(None, None, "test"))][0]
    assert len(calls) == 1 and page.next_cursor == cursor(30)
    first = page.items[0]
    assert first.external_id == f"{COMMUNITY}_30"
    assert first.url == f"https://vk.ru/wall-{COMMUNITY}_30"
    assert first.published_at == datetime.fromtimestamp(1789819200, UTC)
    assert before <= first.collected_at <= datetime.now(UTC)
    assert first.text == json.loads(FIXTURE)["response"]["items"][1]["text"]
    assert first.author_label is None
    assert set(first.metadata) == {
        "community_id",
        "post_id",
        "screen_name",
        "is_pinned",
        "post_type",
        "evidence_kind",
    }
    repost = page.items[1]
    assert "Посоветуйте репетитора" in repost.text
    assert repost.metadata["original_posts"] == [
        {"owner_id": -999999, "post_id": 12, "url": "https://vk.ru/wall-999999_12"}
    ]
    assert all(word not in str(repost.metadata) for word in ("from_id", "attachments", "date"))
    assert (
        TOKEN not in caplog.text + repr(collector) + repr(settings()) + settings().model_dump_json()
    )
    assert "vk_access_token" not in settings().model_dump()
    assert "Синтетический" not in repr(parse_wall(FIXTURE, COMMUNITY))


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        " ",
        "abc def",
        "abc\n",
        "abc\x00",
        "REPLACE_TOKEN",
        "YOUR_TOKEN",
        "changeme",
        "placeholder",
        "<token>",
        "${TOKEN}",
        "x" * 4097,
    ],
)
def test_lazy_credentials_and_sanitized_factory(token: str | None) -> None:
    env = settings(token)
    config = load_config(Path("config"))
    assert build_collector(config.registry.sources[0], config, env).key == "fixture"
    with pytest.raises(ConfigError, match="Configure valid VK credentials"):
        env.vk_access()
    with pytest.raises(VKError) as caught:
        build_collector(approved_source(), config, env)
    assert str(caught.value) == "invalid_configuration"


@pytest.mark.parametrize("token", ["short", "printable:token/+=", "синтетический-токен"])
def test_credentials_do_not_assume_an_exact_format(token: str) -> None:
    assert settings(token).vk_access().get_secret_value() == token


@pytest.mark.parametrize(
    "change", [{"enabled": False}, {"policy_status": "pending"}, {"policy_status": "blocked"}]
)
def test_disabled_pending_never_construct_network(change: dict[str, object]) -> None:
    with pytest.raises(VKError, match="access_or_policy"):
        build_collector(
            approved_source().model_copy(update=change), load_config(Path("config")), settings(None)
        )


@pytest.mark.parametrize(
    "change",
    [
        {"community_id": 1},
        {"community_id": True},
        {"api_version": "5.200"},
        {"api_version": 5.199},
        {"initial_posts": 0},
        {"initial_posts": 21},
        {"initial_posts": True},
        {"incremental_posts": 0},
        {"incremental_posts": 101},
        {"incremental_posts": True},
        {"screen_name": "other"},
        {"access_token": TOKEN},
    ],
)
def test_configuration_restricted_to_reviewed_contract(change: dict[str, object]) -> None:
    data = approved_source().model_dump()
    data["config"].update(change)
    with pytest.raises(ValidationError) as caught:
        SourceConfig.model_validate(data)
    assert TOKEN not in str(caught.value)


def test_seven_sources_remain_pending_and_yandex_unchanged() -> None:
    sources = load_config(Path("config")).registry.sources
    vk = [s for s in sources if s.kind == "vk_api"]
    assert len(vk) == 7
    assert {s.config["community_id"] for s in vk} == set(VK_COMMUNITIES)
    for source in vk:
        assert not source.enabled and source.policy_status == "pending"
        assert source.policy.reviewer is None and source.policy.reviewed_at is None
        assert (
            source.credential_env == "VK_ACCESS_TOKEN" and source.collector_interval_seconds == 3600
        )
        assert source.config["initial_posts"] == 20 and source.config["api_version"] == "5.199"
    yandex = next(s for s in sources if s.key == "yandex_web_search")
    assert not yandex.enabled and yandex.policy_status == "pending"


async def test_initial_limit_pin_order_repeat_and_new_ids() -> None:
    payload = wall(post(1, pinned=True), *(post(i) for i in range(50, 1, -1)))
    first = await collect(payload)
    assert [i.metadata["post_id"] for i in first.items] == [*range(50, 31, -1), 1]
    assert first.next_cursor == cursor(50)
    assert len((await collect(payload, initial=5)).items) == 5
    assert not (await collect(payload, cursor(50))).items
    later = await collect(wall(post(1, pinned=True), post(52), post(51), post(49)), cursor(50))
    assert [i.metadata["post_id"] for i in later.items] == [52, 51]
    assert later.next_cursor == cursor(52)  # Deleted old boundary (50) is not required.


async def test_empty_wall_preserves_cursor_and_initializes_empty_state() -> None:
    first = await collect(wall())
    assert first.items == () and first.next_cursor == cursor(0)
    assert (await collect(wall(), cursor(50))).next_cursor == cursor(50)
    assert len((await collect(wall(*(post(i) for i in range(30, 0, -1))), cursor(0))).items) == 30


@pytest.mark.parametrize("pin", [False, True])
async def test_full_unseen_window_overflows_even_with_old_pin(pin: bool) -> None:
    items = ([post(1, pinned=True)] if pin else []) + [
        post(i) for i in range(200, 100 + int(pin), -1)
    ]
    assert len(items) == 100
    with pytest.raises(VKError, match="cursor_overflow"):
        await collect(wall(*items), cursor(100))
    # A non-pinned boundary, including one below a deleted cursor, proves overlap.
    items[-1] = post(99)
    assert (await collect(wall(*items), cursor(100))).next_cursor == cursor(200)


@pytest.mark.parametrize(
    "mark",
    [
        {},
        {"offset": 20},
        {"version": 2, "community_id": COMMUNITY, "post_id": 1},
        {"version": 1, "community_id": 1, "post_id": 1},
        {"version": 1, "community_id": COMMUNITY, "post_id": True},
        {"version": 1, "community_id": COMMUNITY, "post_id": -1},
    ],
)
async def test_invalid_cursors_fail_before_http(mark: dict[str, JSONValue]) -> None:
    client = VKClient(SecretStr(TOKEN), api_version="5.199")
    collector = VKCollector(
        "vk_ishchu_repetitora",
        client,
        VKOptions(community_id=COMMUNITY, screen_name="ishchu_repetitora"),
    )
    with pytest.raises(VKError, match="invalid_configuration"):
        _ = [p async for p in collector.collect(CollectionContext(None, mark, "test"))]


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"[]",
        b"null",
        b'{"response":null}',
        b'{"response":{},"error":{}}',
        b'{"response":{"count":0,"items":null}}',
        b'{"response":{"count":true,"items":[]}}',
        b'{"response":{"count":1,"items":[]}}',
        b'{"response":{"count":0,"count":0,"items":[]}}',
        b'{"response":{"count":NaN,"items":[]}}',
        b'{"error":{"error_code":"5"}}',
    ],
)
def test_malformed_json_and_envelopes(body: bytes) -> None:
    with pytest.raises(VKError, match="malformed_response"):
        parse_wall(body, COMMUNITY)


@pytest.mark.parametrize(
    "change",
    [
        {"id": True},
        {"owner_id": -1},
        {"owner_id": str(-COMMUNITY)},
        {"date": "1789819200"},
        {"date": True},
        {"date": 10**30},
        {"text": None},
        {"text": "\ud800"},
        {"text": "\x00"},
        {"post_type": "suggest"},
        {"is_pinned": True},
        {"copy_history": None},
        {"copy_history": [{"id": 1, "owner_id": 0, "text": ""}]},
    ],
)
def test_malformed_post_fails_entire_window(change: dict[str, JSONValue]) -> None:
    with pytest.raises(VKError, match="malformed_response"):
        parse_wall(wall(post(10) | change), COMMUNITY)


@pytest.mark.parametrize(
    "items", [(post(1), post(2)), (post(2), post(2)), (post(2, pinned=True), post(1, pinned=True))]
)
def test_duplicate_or_unordered_ids_fail_closed(items: tuple[dict[str, JSONValue], ...]) -> None:
    with pytest.raises(VKError, match="malformed_response"):
        parse_wall(wall(*items), COMMUNITY)


@pytest.mark.parametrize(
    "code,category",
    [
        (5, "authentication"),
        (27, "authentication"),
        (28, "authentication"),
        (7, "access_or_policy"),
        (14, "access_or_policy"),
        (15, "access_or_policy"),
        (203, "access_or_policy"),
        (6, "rate_limit_or_quota"),
        (9, "rate_limit_or_quota"),
        (29, "rate_limit_or_quota"),
        (36, "timeout"),
        (10, "transport"),
        (100, "permanent_request"),
        (999, "permanent_request"),
    ],
)
async def test_api_error_categories_and_no_retries(
    code: int, category: str, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "error": {
                    "error_code": code,
                    "error_msg": TOKEN,
                    "request_params": [{"value": TOKEN}],
                }
            },
        )

    client = VKClient(SecretStr(TOKEN), api_version="5.199", transport=httpx.MockTransport(respond))
    with pytest.raises(VKError) as caught:
        await client.wall(COMMUNITY)
    assert str(caught.value) == category and calls == 1
    assert TOKEN not in caplog.text + repr(caught.value)


@pytest.mark.parametrize(
    "status,category",
    [
        (401, "authentication"),
        (403, "access_or_policy"),
        (429, "rate_limit_or_quota"),
        (408, "timeout"),
        (504, "timeout"),
        (500, "transport"),
        (400, "permanent_request"),
        (302, "permanent_request"),
    ],
)
async def test_http_status_no_redirects(status: int, category: str) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text=TOKEN, headers={"Location": "https://example.invalid/"})

    with pytest.raises(VKError, match=category):
        await VKClient(
            SecretStr(TOKEN), api_version="5.199", transport=httpx.MockTransport(respond)
        ).wall(COMMUNITY)
    assert calls == 1


@pytest.mark.parametrize(
    "error,category",
    [
        (httpx.ConnectError, "transport"),
        (httpx.ReadError, "transport"),
        (httpx.ReadTimeout, "timeout"),
        (httpx.ConnectTimeout, "timeout"),
        (TimeoutError, "timeout"),
    ],
)
async def test_network_errors_sanitized(error: type[Exception], category: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise error(TOKEN)

    with pytest.raises(VKError) as caught:
        await VKClient(
            SecretStr(TOKEN), api_version="5.199", transport=httpx.MockTransport(respond)
        ).wall(COMMUNITY)
    assert str(caught.value) == category and caught.value.__suppress_context__


@pytest.mark.parametrize(
    "headers,body,category",
    [
        ({"Content-Length": str(MAX_RESPONSE_BYTES + 1)}, b"", "response_too_large"),
        ({}, b"x" * (MAX_RESPONSE_BYTES + 1), "response_too_large"),
        ({"Content-Length": "0"}, b"x" * (MAX_RESPONSE_BYTES + 1), "response_too_large"),
        ({"Content-Length": "bad"}, b"", "malformed_response"),
        ({"Content-Encoding": "br"}, b"", "malformed_response"),
    ],
)
async def test_bounded_http_response(headers: dict[str, str], body: bytes, category: str) -> None:
    with pytest.raises(VKError, match=category):
        await VKClient(
            SecretStr(TOKEN),
            api_version="5.199",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=body, headers=headers)
            ),
        ).wall(COMMUNITY)


async def test_total_timeout_and_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client = httpx.AsyncClient
    real_timeout = asyncio.timeout

    def client(**kwargs: object) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return real_client(**kwargs)  # type: ignore[arg-type]

    def timeout(delay: float | None) -> asyncio.Timeout:
        assert delay == 45
        return real_timeout(0.001)

    async def respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, content=FIXTURE)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(asyncio, "timeout", timeout)
    with pytest.raises(VKError) as caught:
        await VKClient(
            SecretStr(TOKEN), api_version="5.199", transport=httpx.MockTransport(respond)
        ).wall(COMMUNITY)
    assert caught.value.category == VKFailure.TIMEOUT


async def test_review_first_request_minimizes_data() -> None:
    counts: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        count = int(parse_qs(request.content.decode())["count"][0])
        counts.append(count)
        items = [post(i) for i in range(30, 0, -1)][:count]
        return httpx.Response(200, json={"response": {"count": 30, "items": items}})

    source = approved_source()
    source.config["initial_posts"] = 5
    instance = build_collector(
        source, load_config(Path("config")), settings(), transport=httpx.MockTransport(respond)
    )
    first = [p async for p in instance.collect(CollectionContext(None, None, "test"))][0]
    assert counts == [5]
    assert len(first.items) == 5 and first.next_cursor == cursor(30)
    again = [p async for p in instance.collect(CollectionContext(None, first.next_cursor, "test"))][
        0
    ]
    assert counts == [5, 100] and not again.items


def test_review_repost_separator_counts_towards_text_limit() -> None:
    item = post(1) | {
        "text": "w" * (MAX_TEXT_CHARS - 1),
        "copy_history": [{"id": 2, "owner_id": -123, "text": "r"}],
    }
    with pytest.raises(VKError, match="response_too_large"):
        parse_wall(wall(item), COMMUNITY)


async def test_review_total_deadline_includes_synchronous_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_timeout = asyncio.timeout
    real_parse = parse_wall

    def slow_parse(*args: object, **kwargs: object) -> object:
        time.sleep(0.02)
        return real_parse(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "timeout", lambda delay: real_timeout(0.005))
    monkeypatch.setattr("tutor_lead_monitor.vk_api.parse_wall", slow_parse)
    client = VKClient(
        SecretStr(TOKEN),
        api_version="5.199",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=FIXTURE)),
    )
    with pytest.raises(VKError, match="timeout"):
        await client.wall(COMMUNITY)


@pytest.mark.parametrize("new_count", [99, 100, 101])
async def test_incremental_overflow_boundary(new_count: int) -> None:
    # Model the provider's total count separately from the requested window.
    payload = wall(*(post(i) for i in range(100 + new_count, 99, -1)))
    if new_count >= 100:
        with pytest.raises(VKError, match="cursor_overflow"):
            await collect(payload, cursor(100))
    else:
        page = await collect(payload, cursor(100))
        assert len(page.items) == 99 and page.next_cursor == cursor(199)


async def test_configured_incremental_limit_and_first_pin_budget() -> None:
    counts: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        count = int(parse_qs(request.content.decode())["count"][0])
        counts.append(count)
        items = [post(1, pinned=True), *(post(i) for i in range(30, 1, -1))]
        return httpx.Response(200, json={"response": {"count": 30, "items": items[:count]}})

    source = approved_source()
    source.config.update(initial_posts=5, incremental_posts=7)
    instance = build_collector(
        source, load_config(Path("config")), settings(), transport=httpx.MockTransport(respond)
    )
    page = [p async for p in instance.collect(CollectionContext(None, None, "first"))][0]
    assert counts == [5]
    assert [i.external_id for i in page.items] == [f"{COMMUNITY}_{i}" for i in (30, 29, 28, 27, 1)]
    repeat = [
        p async for p in instance.collect(CollectionContext(None, page.next_cursor, "again"))
    ][0]
    assert counts == [5, 7] and not repeat.items
    with pytest.raises(VKError, match="cursor_overflow"):
        _ = [p async for p in instance.collect(CollectionContext(None, cursor(10), "overflow"))]
    assert counts == [5, 7, 7]


@pytest.mark.parametrize("field", ["id", "owner_id", "date", "text", "post_type"])
def test_missing_required_post_fields(field: str) -> None:
    item = post(1)
    del item[field]
    with pytest.raises(VKError, match="malformed_response"):
        parse_wall(wall(item), COMMUNITY)


@pytest.mark.parametrize(
    "change",
    [
        {"id": 0},
        {"id": -1},
        {"id": 1.5},
        {"id": None},
        {"owner_id": True},
        {"owner_id": COMMUNITY},
        {"date": -1},
        {"date": 1.5},
        {"copy_history": [{"id": True, "owner_id": -1, "text": ""}]},
        {"copy_history": [{"id": 1, "owner_id": -1, "text": ""}] * 6},
    ],
)
def test_additional_strict_post_validation(change: dict[str, JSONValue]) -> None:
    with pytest.raises(VKError, match="malformed_response"):
        parse_wall(wall(post(1) | change), COMMUNITY)


def test_repost_components_are_verbatim_unique_and_bounded() -> None:
    original: dict[str, JSONValue] = {"id": 2, "owner_id": -1, "text": "  Original wording\n"}
    item: dict[str, JSONValue] = post(1) | {
        "text": " Wrapper ",
        "copy_history": [original, original],
    }
    parsed = parse_wall(wall(item), COMMUNITY)[0]
    assert parsed.evidence_text == " Wrapper \n\n  Original wording\n"
    nested: dict[str, JSONValue] = original | {
        "copy_history": [original | {"copy_history": [original | {"copy_history": [original]}]}]
    }
    with pytest.raises(VKError, match="malformed_response"):
        parse_wall(wall(post(1) | {"copy_history": [nested]}), COMMUNITY)


@pytest.mark.parametrize(
    "mode,category",
    [
        ("success", None),
        ("oversized", "response_too_large"),
        ("bad_length", "response_too_large"),
        ("malformed", "malformed_response"),
        ("content_type", "malformed_response"),
        ("redirect", "permanent_request"),
        ("cancel", None),
        ("timeout", "timeout"),
    ],
)
async def test_stream_limits_and_resource_cleanup(mode: str, category: str | None) -> None:
    class Stream(httpx.AsyncByteStream):
        closed = False
        reads = 0

        async def __aiter__(self):  # type: ignore[no-untyped-def]
            self.reads += 1
            if mode == "cancel":
                raise asyncio.CancelledError
            if mode == "timeout":
                raise httpx.ReadTimeout("synthetic secret response must not escape")
            if mode == "oversized":
                for _ in range(MAX_RESPONSE_BYTES // 65536 + 10):
                    self.reads += 1
                    yield b"x" * 65536
            else:
                yield b"not-json" if mode == "malformed" else FIXTURE

        async def aclose(self) -> None:
            self.closed = True

    class Transport(httpx.MockTransport):
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    stream = Stream()
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        headers = {}
        if mode == "bad_length":
            headers["Content-Length"] = str(MAX_RESPONSE_BYTES + 1)
        if mode == "content_type":
            headers["Content-Type"] = "text/html"
        if mode == "redirect":
            headers["Location"] = "https://example.invalid/forbidden"
        return httpx.Response(302 if mode == "redirect" else 200, stream=stream, headers=headers)

    transport = Transport(respond)
    client = VKClient(SecretStr(TOKEN), api_version="5.199", transport=transport)
    if mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await client.wall(COMMUNITY)
    elif category:
        with pytest.raises(VKError, match=category) as error:
            await client.wall(COMMUNITY)
        assert str(error.value) == category
    else:
        assert len(await client.wall(COMMUNITY)) == 4
    assert calls == 1 and stream.closed and transport.closed
    if mode in {"bad_length", "content_type", "redirect"}:
        assert stream.reads == 0
    if mode == "oversized":
        assert stream.reads == MAX_RESPONSE_BYTES // 65536 + 2

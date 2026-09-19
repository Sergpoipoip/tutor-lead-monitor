"""Bounded official wall.get adapter; no destination, profile or attachment requests."""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import httpx
from pydantic import SecretStr

ENDPOINT = "https://api.vk.com/method/wall.get"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 65536
MAX_REPOSTS = 5
WALL_LIMIT = 100
TIMEOUT = httpx.Timeout(connect=10, read=30, write=10, pool=10)


class VKFailure(StrEnum):
    AUTHENTICATION = "authentication"
    ACCESS = "access_or_policy"
    QUOTA = "rate_limit_or_quota"
    TRANSPORT = "transport"
    TIMEOUT = "timeout"
    MALFORMED = "malformed_response"
    TOO_LARGE = "response_too_large"
    CONFIGURATION = "invalid_configuration"
    OVERFLOW = "cursor_overflow"
    REQUEST = "permanent_request"


class VKError(Exception):
    def __init__(self, category: VKFailure) -> None:
        self.category = category
        super().__init__(category.value)


@dataclass(frozen=True, repr=False)
class Repost:
    owner_id: int
    post_id: int
    text: str

    @property
    def url(self) -> str:
        return f"https://vk.ru/wall{self.owner_id}_{self.post_id}"


@dataclass(frozen=True, repr=False)
class WallPost:
    post_id: int
    published_at: datetime
    text: str
    is_pinned: bool
    post_type: str
    originals: tuple[Repost, ...]

    @property
    def evidence_text(self) -> str:
        texts = [self.text]
        texts.extend(p.text for p in self.originals if p.text and p.text not in texts)
        return "\n\n".join(t for t in texts if t)


def api_failure(code: int) -> VKFailure:
    if code in {4, 5, 16, 27, 28}:
        return VKFailure.AUTHENTICATION
    if code in {2, 7, 11, 14, 15, 17, 18, 19, 20, 21, 23, 24, 25, 30, 37, 42, 203, 210}:
        return VKFailure.ACCESS
    if code in {6, 9, 29, 32, 103}:
        return VKFailure.QUOTA
    if code == 36:
        return VKFailure.TIMEOUT
    if code in {1, 10}:
        return VKFailure.TRANSPORT
    return VKFailure.REQUEST


def integer(value: object, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum or value > 2**63 - 1:
        raise ValueError
    return value


def post_text(value: object) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError
    # Reject unpaired surrogate escapes too: PostgreSQL UTF-8 cannot retain them.
    value.encode("utf-8")
    if len(value) > MAX_TEXT_CHARS:
        raise VKError(VKFailure.TOO_LARGE)
    return value


def reposts(value: object, *, depth: int = 0) -> tuple[Repost, ...]:
    if not isinstance(value, list) or len(value) > MAX_REPOSTS or (value and depth >= 3):
        raise ValueError
    result: list[Repost] = []
    for original in value:
        if not isinstance(original, dict):
            raise ValueError
        owner = integer(original.get("owner_id"), minimum=-(2**63) + 1)
        if owner == 0:
            raise ValueError
        result.append(Repost(owner, integer(original.get("id")), post_text(original.get("text"))))
        result.extend(reposts(original.get("copy_history", []), depth=depth + 1))
        if len(result) > MAX_REPOSTS:
            raise ValueError
    return tuple(result)


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def invalid_constant(value: str) -> None:
    raise ValueError


def parse_wall(
    payload: bytes, community_id: int, *, limit: int = WALL_LIMIT
) -> tuple[WallPost, ...]:
    if len(payload) > MAX_RESPONSE_BYTES:
        raise VKError(VKFailure.TOO_LARGE)
    try:
        envelope = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=invalid_constant,
        )
        if not isinstance(envelope, dict) or ("error" in envelope) == ("response" in envelope):
            raise ValueError
        if "error" in envelope:
            error = envelope["error"]
            if not isinstance(error, dict):
                raise ValueError
            raise VKError(api_failure(integer(error.get("error_code"))))
        response = envelope["response"]
        if not isinstance(response, dict):
            raise ValueError
        count = integer(response.get("count"), minimum=0)
        items = response.get("items")
        if not isinstance(items, list) or len(items) > limit or count < len(items):
            raise ValueError
        # An unexpectedly short top page cannot prove that unseen posts fit in it.
        if len(items) != min(count, limit):
            raise ValueError
        results: list[WallPost] = []
        seen: set[int] = set()
        previous: int | None = None
        pins = 0
        for item in items:
            if not isinstance(item, dict):
                raise ValueError
            if integer(item.get("owner_id"), minimum=-(2**63) + 1) != -community_id:
                raise ValueError
            post_id = integer(item.get("id"))
            pin = item.get("is_pinned", 0)
            if type(pin) is not int or pin not in {0, 1} or post_id in seen:
                raise ValueError
            seen.add(post_id)
            pins += pin
            if pins > 1:
                raise ValueError
            # An old pin is not a chronological boundary. All other IDs must descend.
            if not pin:
                if previous is not None and post_id >= previous:
                    raise ValueError
                previous = post_id
            post_type = item.get("post_type")
            if post_type not in ("post", "copy"):
                raise ValueError
            published = datetime.fromtimestamp(integer(item.get("date"), minimum=0), UTC)
            text = post_text(item.get("text"))
            originals = reposts(item.get("copy_history", []))
            post = WallPost(post_id, published, text, bool(pin), post_type, originals)
            if len(post.evidence_text) > MAX_TEXT_CHARS:
                raise VKError(VKFailure.TOO_LARGE)
            results.append(post)
        return tuple(results)
    except (ValueError, TypeError, OverflowError, OSError, RecursionError):
        raise VKError(VKFailure.MALFORMED) from None


class VKClient:
    def __init__(
        self,
        token: SecretStr,
        *,
        api_version: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._version = api_version
        self._transport = transport

    async def wall(self, community_id: int, *, limit: int = WALL_LIMIT) -> tuple[WallPost, ...]:
        if type(limit) is not int or not 1 <= limit <= WALL_LIMIT:
            raise VKError(VKFailure.CONFIGURATION)
        body = {
            "access_token": self._token.get_secret_value(),
            "v": self._version,
            "owner_id": str(-community_id),
            "count": str(limit),
            "offset": "0",
            "filter": "all",
            "extended": "0",
        }
        deadline = asyncio.timeout(45)
        try:
            async with (
                deadline,
                httpx.AsyncClient(
                    transport=self._transport,
                    timeout=TIMEOUT,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    "POST", ENDPOINT, data=body, headers={"Accept-Encoding": "identity"}
                ) as response,
            ):
                code = response.status_code
                if code == 401:
                    raise VKError(VKFailure.AUTHENTICATION)
                if code == 403:
                    raise VKError(VKFailure.ACCESS)
                if code == 429:
                    raise VKError(VKFailure.QUOTA)
                if code == 408 or code == 504:
                    raise VKError(VKFailure.TIMEOUT)
                if code >= 500:
                    raise VKError(VKFailure.TRANSPORT)
                if code != 200:
                    raise VKError(VKFailure.REQUEST)
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise VKError(VKFailure.MALFORMED)
                content_type = response.headers.get("Content-Type", "application/json")
                if content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise VKError(VKFailure.MALFORMED)
                length = response.headers.get("Content-Length")
                if length is not None:
                    if not length.isascii() or not length.isdigit():
                        raise VKError(VKFailure.MALFORMED)
                    if len(length) > 10 or int(length) > MAX_RESPONSE_BYTES:
                        raise VKError(VKFailure.TOO_LARGE)
                content = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise VKError(VKFailure.TOO_LARGE)
                posts = parse_wall(bytes(content), community_id, limit=limit)
            # Synchronous parsing/cleanup cannot be interrupted by asyncio.timeout.
            # Check the same deadline explicitly before allowing evidence to escape.
            expires = deadline.when()
            if expires is not None and asyncio.get_running_loop().time() >= expires:
                raise VKError(VKFailure.TIMEOUT)
            return posts
        except (httpx.TimeoutException, TimeoutError):
            raise VKError(VKFailure.TIMEOUT) from None
        except httpx.HTTPError:
            raise VKError(VKFailure.TRANSPORT) from None

"""One explicitly invoked wall window, one community, one durable post-ID cursor."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from tutor_lead_monitor.config import VKOptions
from tutor_lead_monitor.domain.models import (
    CollectedItem,
    CollectionContext,
    CollectionPage,
    JSONValue,
)
from tutor_lead_monitor.vk_api import VKClient, VKError, VKFailure


class VKCollector:
    def __init__(self, key: str, client: VKClient, options: VKOptions) -> None:
        self.key, self.client, self.options = key, client, options

    async def collect(self, context: CollectionContext) -> AsyncIterator[CollectionPage]:
        cursor = context.cursor
        community = self.options.community_id
        mark = 0
        if cursor is not None:
            if (
                set(cursor) != {"version", "community_id", "post_id"}
                or type(cursor.get("version")) is not int
                or cursor["version"] != 1
                or type(cursor.get("community_id")) is not int
                or cursor["community_id"] != community
                or type(cursor.get("post_id")) is not int
            ):
                raise VKError(VKFailure.CONFIGURATION)
            raw_mark = cursor["post_id"]
            assert isinstance(raw_mark, int)
            if not 0 <= raw_mark <= 2**63 - 1:
                raise VKError(VKFailure.CONFIGURATION)
            mark = raw_mark
        limit = self.options.initial_posts if cursor is None else self.options.incremental_posts
        posts = await self.client.wall(community, limit=limit)
        collected_at = datetime.now(UTC)
        if (
            cursor is not None
            and len(posts) == limit
            and not any(not p.is_pinned and p.post_id <= mark for p in posts)
        ):
            # Even an old pin must not hide a full page of unseen ordinary posts.
            # Fail before yielding: neither evidence nor checkpoint advances.
            raise VKError(VKFailure.OVERFLOW)
        new_posts = sorted(
            (p for p in posts if p.post_id > mark), key=lambda p: p.post_id, reverse=True
        )
        items: list[CollectedItem] = []
        for post in new_posts:
            metadata: dict[str, JSONValue] = {
                "community_id": community,
                "post_id": post.post_id,
                "screen_name": self.options.screen_name,
                "is_pinned": post.is_pinned,
                "post_type": post.post_type,
                "evidence_kind": "vk_wall_post",
            }
            if post.originals:
                metadata["original_posts"] = [
                    {"owner_id": p.owner_id, "post_id": p.post_id, "url": p.url}
                    for p in post.originals
                ]
            items.append(
                CollectedItem(
                    source_key=self.key,
                    external_id=f"{community}_{post.post_id}",
                    url=f"https://vk.ru/wall-{community}_{post.post_id}",
                    published_at=post.published_at,
                    collected_at=collected_at,
                    text=post.evidence_text,
                    metadata=metadata,
                )
            )
        # Zero marks an initialized empty wall, avoiding first-run truncation later.
        next_mark = max((p.post_id for p in new_posts), default=mark)
        yield CollectionPage(
            tuple(items), {"version": 1, "community_id": community, "post_id": next_mark}, False
        )

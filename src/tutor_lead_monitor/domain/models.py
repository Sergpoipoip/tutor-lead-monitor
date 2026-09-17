import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from tutor_lead_monitor.domain.enums import Goal, Intent, LeadStatus, LessonFormat, Subject, Urgency

type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware timestamps are required")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class CollectedItem:
    source_key: str
    external_id: str
    url: str | None
    published_at: datetime | None
    collected_at: datetime
    text: str
    author_label: str | None = None
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_key or not self.external_id:
            raise ValueError("Source key and stable external ID are required")
        if "_tlm_processing" in self.metadata:
            raise ValueError("Collector metadata uses a reserved processing key")
        try:
            json.dumps(dict(self.metadata), allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("Collector metadata must be JSON-serializable") from None
        object.__setattr__(self, "collected_at", utc(self.collected_at))
        if self.published_at is not None:
            object.__setattr__(self, "published_at", utc(self.published_at))


@dataclass(frozen=True)
class CollectionContext:
    since: datetime | None
    cursor: Mapping[str, JSONValue] | None
    run_id: str

    def __post_init__(self) -> None:
        if self.since is not None:
            object.__setattr__(self, "since", utc(self.since))


@dataclass(frozen=True)
class CollectionPage:
    items: tuple[CollectedItem, ...]
    next_cursor: Mapping[str, JSONValue] | None
    has_more: bool


@dataclass(frozen=True)
class ScoreReason:
    rule_id: str
    delta: int
    explanation: str


@dataclass(frozen=True)
class Lead:
    """Canonical lead; source evidence is retained through raw-item occurrences."""

    id: UUID
    canonical_raw_item_id: UUID
    intent: Intent
    subject: Subject
    score: int
    classification_version: str
    scoring_version: str
    first_published_at: datetime | None
    last_seen_at: datetime
    grade: int | None = None
    goals: tuple[Goal, ...] = ()
    format: LessonFormat = LessonFormat.UNKNOWN
    urgency: Urgency = Urgency.UNKNOWN
    location_text: str | None = None
    budget_text: str | None = None
    contact_available: bool = False
    score_reasons: tuple[ScoreReason, ...] = ()
    status: LeadStatus = LeadStatus.NEW

    def __post_init__(self) -> None:
        if not 0 <= self.score <= 100:
            raise ValueError("Score must be in 0..100")
        if self.grade is not None and not 1 <= self.grade <= 11:
            raise ValueError("Grade must be in 1..11")
        object.__setattr__(self, "last_seen_at", utc(self.last_seen_at))
        if self.first_published_at is not None:
            object.__setattr__(self, "first_published_at", utc(self.first_published_at))

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tutor_lead_monitor.domain.enums import Intent, Subject
from tutor_lead_monitor.domain.models import CollectedItem, Lead


def test_lead_bounds_and_utc() -> None:
    lead = Lead(
        id=uuid4(),
        canonical_raw_item_id=uuid4(),
        intent=Intent.SEEKING_TUTOR,
        subject=Subject.LITERATURE,
        score=90,
        classification_version="test",
        scoring_version="test",
        first_published_at=None,
        last_seen_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="Score"):
        replace(lead, score=101)
    with pytest.raises(ValueError, match="Grade"):
        replace(lead, grade=12)
    with pytest.raises(ValueError, match="Timezone-aware"):
        replace(lead, last_seen_at=datetime(2026, 1, 1))


def test_collector_metadata_must_be_json_serializable() -> None:
    with pytest.raises(ValueError, match="JSON-serializable"):
        CollectedItem(
            source_key="fixture",
            external_id="stable",
            url=None,
            published_at=None,
            collected_at=datetime.now(UTC),
            text="synthetic",
            metadata={"bad": float("nan")},
        )


def test_collector_requires_stable_id() -> None:
    with pytest.raises(ValueError, match="stable external ID"):
        CollectedItem(
            source_key="fixture",
            external_id="",
            url=None,
            published_at=None,
            collected_at=datetime.now(UTC),
            text="synthetic",
        )

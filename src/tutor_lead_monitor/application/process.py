import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from tutor_lead_monitor.config import AppConfig
from tutor_lead_monitor.db.locks import DEDUPLICATION, MAINTENANCE, transaction_lock
from tutor_lead_monitor.db.models import Lead, LeadOccurrence, RawItem
from tutor_lead_monitor.db.pipeline import claim_pending, find_duplicate, occurrence_exists
from tutor_lead_monitor.domain.enums import Intent, Subject
from tutor_lead_monitor.domain.models import JSONValue, utc
from tutor_lead_monitor.processing.classify import classify
from tutor_lead_monitor.processing.extract import extract
from tutor_lead_monitor.processing.normalize import canonical_url, normalize
from tutor_lead_monitor.processing.score import score

logger = logging.getLogger(__name__)


@dataclass
class ProcessingResult:
    processed: int = 0
    rejected: int = 0
    failed: int = 0


def process_item(session: Session, raw: RawItem, config: AppConfig, now: datetime) -> str:
    now = utc(now)
    utc(raw.collected_at)
    if raw.published_at is not None:
        utc(raw.published_at)
    normalized = normalize(raw.text, tuple(config.processing.boilerplate))
    classification = classify(normalized.readable, config.processing)
    fields = extract(
        normalized.readable, response_available=raw.metadata_.get("response_available") is True
    )
    scored = score(
        classification, fields, published_at=raw.published_at, now=now, config=config.scoring
    )
    raw.normalized_text = normalized.readable
    raw.exact_fingerprint = normalized.exact_fingerprint
    raw.canonical_url = canonical_url(raw.url)
    metadata = dict(raw.metadata_)
    metadata["_tlm_processing"] = cast(
        JSONValue,
        {
            "fingerprint_text": normalized.fingerprint_text,
            "classification": asdict(classification),
            "extraction": asdict(fields),
            "score": asdict(scored),
        },
    )
    raw.metadata_ = metadata
    raw.processing_error = None
    eligible = (
        classification.intent in {Intent.SEEKING_TUTOR, Intent.UNCERTAIN}
        and classification.subject in {Subject.LITERATURE, Subject.RUSSIAN_AND_LITERATURE}
        and scored.score >= config.scoring.thresholds.review
    )
    # Deduplication and lead insertion share one lock, including empty-candidate races.
    transaction_lock(session, DEDUPLICATION)
    matched = find_duplicate(
        session, raw, normalized, classification, fields, config.business.deduplication
    )
    if not eligible and matched is None:
        raw.processing_status = "rejected"
        return "rejected"
    if matched is None:
        lead = Lead(
            canonical_raw_item_id=raw.id,
            intent=classification.intent.value,
            subject=classification.subject.value,
            grade=fields.grade,
            goals=[g.value for g in fields.goals],
            format=fields.format.value,
            location_text=fields.location_text,
            budget_text=fields.budget_text,
            urgency=fields.urgency.value,
            contact_available=fields.contact_available,
            score=scored.score,
            score_reasons=[cast(dict[str, JSONValue], asdict(r)) for r in scored.reasons],
            classification_version=classification.version,
            scoring_version=scored.version,
            first_published_at=raw.published_at,
            last_seen_at=raw.collected_at,
        )
        session.add(lead)
        session.flush()
        method, similarity = "exact_id", 100.0
    else:
        lead, method, similarity = matched
        lead.last_seen_at = max(lead.last_seen_at, raw.collected_at)
        if raw.published_at is not None:
            lead.first_published_at = min(
                lead.first_published_at or raw.published_at, raw.published_at
            )
    if not occurrence_exists(session, raw.id):
        session.add(
            LeadOccurrence(
                lead_id=lead.id,
                raw_item_id=raw.id,
                match_method=method,
                similarity=Decimal(str(round(similarity, 2))),
            )
        )
    raw.processing_status = "processed"
    return "processed"


def process_pending(
    engine: Engine, config: AppConfig, *, limit: int = 1000, now: datetime | None = None
) -> ProcessingResult:
    if limit < 1:
        raise ValueError("Processing limit must be positive")
    instant = utc(now) if now is not None else datetime.now(UTC)
    result = ProcessingResult()
    for _ in range(limit):
        with Session(engine) as session, session.begin():
            transaction_lock(session, MAINTENANCE, shared=True)
            raw = claim_pending(session)
            if raw is None:
                break
            try:
                with session.begin_nested():
                    status = process_item(session, raw, config, instant)
                    session.flush()
            except Exception as error:
                raw.processing_status = "failed"
                raw.processing_error = type(error).__name__
                status = "failed"
                logger.error(
                    "processing_failed",
                    extra={"record_id": str(raw.id), "error_category": type(error).__name__},
                )
            setattr(result, status, getattr(result, status) + 1)
    return result

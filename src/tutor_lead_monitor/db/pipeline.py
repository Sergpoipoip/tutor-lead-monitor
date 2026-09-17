from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from tutor_lead_monitor.config import DeduplicationConfig
from tutor_lead_monitor.db.models import Lead, LeadOccurrence, RawItem
from tutor_lead_monitor.domain.enums import Goal, LessonFormat, Urgency
from tutor_lead_monitor.processing.deduplicate import compatible, near_duplicate, tokens
from tutor_lead_monitor.processing.models import Classification, ExtractedFields
from tutor_lead_monitor.processing.normalize import NormalizedText, contact_signature, normalize


def claim_pending(session: Session) -> RawItem | None:
    return session.scalar(
        select(RawItem)
        .where(RawItem.processing_status == "pending")
        .order_by(RawItem.collected_at, RawItem.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )


def find_duplicate(
    session: Session,
    raw: RawItem,
    normalized: NormalizedText,
    classification: Classification,
    fields: ExtractedFields,
    config: DeduplicationConfig,
) -> tuple[Lead, str, float] | None:
    # Any occurrence, not just the canonical raw row, can identify a repeat.
    existing = session.scalar(
        select(Lead).join(LeadOccurrence).where(LeadOccurrence.raw_item_id == raw.id)
    )
    if existing is not None:
        return existing, "exact_id", 100.0
    instant = raw.published_at or raw.collected_at
    effective_time = func.coalesce(RawItem.published_at, RawItem.collected_at)
    query = (
        select(Lead, RawItem)
        .join(LeadOccurrence, LeadOccurrence.lead_id == Lead.id)
        .join(RawItem, LeadOccurrence.raw_item_id == RawItem.id)
        .where(RawItem.id != raw.id)
    )
    if raw.canonical_url:
        matched = session.execute(
            query.where(RawItem.canonical_url == raw.canonical_url)
            .order_by(Lead.created_at, Lead.id)
            .limit(1)
        ).first()
        if matched is not None:
            return matched[0], "exact_url", 100.0
    recent = query.where(
        effective_time >= instant - timedelta(days=config.window_days),
        effective_time <= instant + timedelta(days=config.window_days),
        Lead.subject == classification.subject.value,
        Lead.intent == classification.intent.value,
    )
    if fields.grade is not None:
        recent = recent.where(or_(Lead.grade.is_(None), Lead.grade == fields.grade))
    anchors = sorted(tokens(normalized.fingerprint_text), key=lambda t: (-len(t), t))[:3]
    if not anchors:
        return None
    candidates = session.execute(
        recent.where(
            or_(
                RawItem.exact_fingerprint == normalized.exact_fingerprint,
                *(
                    func.lower(RawItem.normalized_text).contains(t, autoescape=True)
                    for t in anchors
                ),
            )
        ).order_by(Lead.created_at, Lead.id, RawItem.id)
    ).all()
    best: tuple[Lead, str, float] | None = None
    for lead, occurrence in candidates:
        current_contacts = contact_signature(raw.text)
        previous_contacts = contact_signature(occurrence.text)
        if current_contacts and previous_contacts and current_contacts != previous_contacts:
            continue
        previous = lead_fields(lead)
        if not compatible(fields, previous):
            continue
        if (
            occurrence.exact_fingerprint == normalized.exact_fingerprint
            and normalized.fingerprint_text
        ):
            return lead, "exact_text", 100.0
        stored = occurrence.metadata_.get("_tlm_processing")
        fingerprint = stored.get("fingerprint_text") if isinstance(stored, dict) else None
        if not isinstance(fingerprint, str):
            fingerprint = normalize(occurrence.text).fingerprint_text
        similarity = near_duplicate(
            normalized.fingerprint_text, fingerprint, config.similarity_threshold
        )
        if similarity is not None and (best is None or similarity > best[2]):
            best = (lead, "fuzzy_text", similarity)
    return best


def occurrence_exists(session: Session, raw_id: UUID) -> bool:
    return (
        session.scalar(
            select(LeadOccurrence.raw_item_id).where(LeadOccurrence.raw_item_id == raw_id)
        )
        is not None
    )


def rejected_exact_candidates(
    session: Session, raw: RawItem, config: DeduplicationConfig
) -> list[RawItem]:
    """Only exact identities are strong enough to recover previously rejected evidence."""
    instant = raw.published_at or raw.collected_at
    effective_time = func.coalesce(RawItem.published_at, RawItem.collected_at)
    text_match = and_(
        RawItem.exact_fingerprint == raw.exact_fingerprint,
        effective_time >= instant - timedelta(days=config.window_days),
        effective_time <= instant + timedelta(days=config.window_days),
    )
    identity = (
        or_(RawItem.canonical_url == raw.canonical_url, text_match)
        if raw.canonical_url
        else text_match
    )
    return list(
        session.scalars(
            select(RawItem)
            .where(
                RawItem.processing_status == "rejected",
                RawItem.id != raw.id,
                ~select(LeadOccurrence.raw_item_id)
                .where(LeadOccurrence.raw_item_id == RawItem.id)
                .exists(),
                identity,
            )
            .order_by(RawItem.id)
            .with_for_update()
        )
    )


def lead_fields(lead: Lead) -> ExtractedFields:
    return ExtractedFields(
        grade=lead.grade,
        goals=tuple(Goal(g) for g in lead.goals),
        format=LessonFormat(lead.format),
        location_text=lead.location_text,
        budget_text=lead.budget_text,
        urgency=Urgency(lead.urgency),
        contact_available=lead.contact_available,
    )

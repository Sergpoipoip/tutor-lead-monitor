from datetime import datetime, timedelta

from tutor_lead_monitor.config import ScoringConfig
from tutor_lead_monitor.domain.enums import Goal, Intent, LessonFormat, Subject
from tutor_lead_monitor.domain.models import ScoreReason, utc
from tutor_lead_monitor.processing.models import Classification, ExtractedFields, ScoreResult


def score(
    classification: Classification,
    fields: ExtractedFields,
    *,
    published_at: datetime | None,
    now: datetime,
    config: ScoringConfig,
) -> ScoreResult:
    now = utc(now)
    age = now - utc(published_at) if published_at is not None else None
    features = (
        ("seeking_tutor", classification.intent == Intent.SEEKING_TUTOR, "Tutor request"),
        ("literature", classification.subject == Subject.LITERATURE, "Literature is primary"),
        (
            "russian_and_literature",
            classification.subject == Subject.RUSSIAN_AND_LITERATURE,
            "Russian and literature",
        ),
        ("exam", bool({Goal.EGE, Goal.OGE}.intersection(fields.goals)), "EGE or OGE preparation"),
        (
            "olympiad",
            bool({Goal.OLYMPIAD, Goal.VSOSH}.intersection(fields.goals)),
            "Olympiad or VSOSH",
        ),
        ("online", fields.format in {LessonFormat.ONLINE, LessonFormat.EITHER}, "Online accepted"),
        ("grade_stated", fields.grade is not None, "Grade stated"),
        (
            "fresh",
            age is not None and timedelta(0) <= age < timedelta(hours=config.fresh_hours),
            "Recent publication",
        ),
        ("budget_stated", fields.budget_text is not None, "Budget stated"),
        ("contact_available", fields.contact_available, "Response path available"),
        (
            "offering_tutoring",
            classification.intent == Intent.OFFERING_TUTORING,
            "Tutor advertisement",
        ),
        (
            "school_or_agency_ad",
            classification.intent == Intent.SCHOOL_OR_AGENCY_AD,
            "Agency advertisement",
        ),
        ("teaching_job", classification.intent == Intent.TEACHING_JOB, "Teaching vacancy"),
        (
            "closed_or_stale",
            fields.closed or (age is not None and age >= timedelta(days=config.stale_days)),
            "Closed or stale request",
        ),
    )
    weights = config.weights.model_dump()
    reasons = tuple(
        ScoreReason(key, weights[key], explanation)
        for key, applies, explanation in features
        if applies
    )
    return ScoreResult(max(0, min(100, sum(r.delta for r in reasons))), reasons, config.version)

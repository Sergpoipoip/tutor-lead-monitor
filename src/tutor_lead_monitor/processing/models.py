from dataclasses import dataclass

from tutor_lead_monitor.domain.enums import Goal, Intent, LessonFormat, Subject, Urgency
from tutor_lead_monitor.domain.models import ScoreReason


@dataclass(frozen=True)
class Evidence:
    rule_id: str
    start: int
    end: int


@dataclass(frozen=True)
class Classification:
    intent: Intent
    subject: Subject
    confidence: float
    matched_rule_ids: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    version: str


@dataclass(frozen=True)
class ExtractedFields:
    grade: int | None = None
    goals: tuple[Goal, ...] = ()
    format: LessonFormat = LessonFormat.UNKNOWN
    location_text: str | None = None
    urgency: Urgency = Urgency.UNKNOWN
    budget_text: str | None = None
    contact_available: bool = False
    closed: bool = False
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class ScoreResult:
    score: int
    reasons: tuple[ScoreReason, ...]
    version: str

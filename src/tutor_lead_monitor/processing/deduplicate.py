from tutor_lead_monitor.domain.enums import LessonFormat
from tutor_lead_monitor.processing.models import ExtractedFields

STOP_WORDS = {
    "и",
    "по",
    "для",
    "на",
    "в",
    "с",
    "к",
    "а",
    "но",
    "или",
    "из",
    "от",
    "до",
    "urltoken",
    "phonetoken",
}


def tokens(text: str) -> frozenset[str]:
    return frozenset(word for word in text.split() if word not in STOP_WORDS)


def token_similarity(left: str, right: str) -> float:
    a, b = tokens(left), tokens(right)
    return 100 * len(a & b) / len(a | b) if a and b else 0.0


def compatible(left: ExtractedFields, right: ExtractedFields) -> bool:
    if left.grade is not None and right.grade is not None and left.grade != right.grade:
        return False
    if left.goals and right.goals and set(left.goals) != set(right.goals):
        return False
    if {left.format, right.format} == {LessonFormat.ONLINE, LessonFormat.OFFLINE}:
        return False
    if (
        left.location_text
        and right.location_text
        and left.location_text.casefold() != right.location_text.casefold()
    ):
        return False
    return not (left.budget_text and right.budget_text and left.budget_text != right.budget_text)


def near_duplicate(left: str, right: str, threshold: int) -> float | None:
    # Short generic requests and subset matches need stronger evidence than one shared label.
    if min(len(tokens(left)), len(tokens(right))) < 5:
        return None
    similarity = token_similarity(left, right)
    return similarity if similarity >= threshold else None

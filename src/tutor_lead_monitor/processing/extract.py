import re

from tutor_lead_monitor.domain.enums import Goal, LessonFormat, Urgency
from tutor_lead_monitor.processing.models import Evidence, ExtractedFields
from tutor_lead_monitor.processing.normalize import PHONE_PATTERN

GOALS = {
    Goal.SCHOOL_SUPPORT: r"подтянуть|школьн\w*\s+программ|школьн\w*\s+поддерж|домашн\w*\s+задани",
    Goal.ESSAY: r"сочинени\w*|эссе",
    Goal.OGE: r"\bогэ\b",
    Goal.EGE: r"\bегэ\b",
    Goal.OLYMPIAD: r"олимпиад\w*",
    Goal.VSOSH: r"\bвсош\b|всероссийск\w*\s+олимпиад",
    Goal.ADMISSIONS: r"поступлени\w*|вступительн\w*\s+экзамен",
    Goal.ADULT_STUDY: r"для\s+взросл\w*|взрослый\s+ученик",
}


def extract(text: str, *, response_available: bool = False) -> ExtractedFields:
    evidence: list[Evidence] = []

    def find(rule: str, pattern: str) -> list[re.Match[str]]:
        matches = list(re.finditer(pattern, text, re.IGNORECASE))
        evidence.extend(Evidence(rule, m.start(), m.end()) for m in matches)
        return matches

    grades = find(
        "grade", r"\b(\d{1,2})(?:\s*-?\s*(?:й|м|го|ый))?\s*класс\w*\b|\bgrade\s+(\d{1,2})\b"
    )
    numbers = {int(m.group(1) or m.group(2)) for m in grades}
    # Ranges/multiple grades are deliberately not guessed.
    grade = (
        next(iter(numbers))
        if len(numbers) == 1 and not re.search(r"\b\d{1,2}\s*[-/]\s*\d{1,2}\s*класс", text, re.I)
        else None
    )
    if grade is not None and not 1 <= grade <= 11:
        grade = None
    goals = tuple(goal for goal, pattern in GOALS.items() if find(f"goal_{goal}", pattern))
    online = bool(find("format_online", r"\bонлайн\b|\bдистанционно\b"))
    offline = bool(find("format_offline", r"\bофлайн\b|\bоффлайн\b|\bочно\b"))
    if re.search(r"\bне\s+(онлайн|дистанционно)\b", text, re.I):
        online = False
    if re.search(r"\bне\s+(офлайн|оффлайн|очно)\b", text, re.I):
        offline = False
    format_ = (
        LessonFormat.EITHER
        if online and offline
        else (
            LessonFormat.ONLINE
            if online
            else LessonFormat.OFFLINE
            if offline
            else LessonFormat.UNKNOWN
        )
    )
    locations = find("location", r"\b(?:город|г\.)\s*([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\b")
    location = locations[0].group(1) if len(locations) == 1 else None
    high = find("urgency_high", r"\bсрочно\b|\bкак\s+можно\s+скорее\b")
    low = find("urgency_low", r"\bне\s+срочно\b|\bне\s+спешим\b")
    urgency = Urgency.LOW if low else Urgency.HIGH if high else Urgency.UNKNOWN
    budgets = find(
        "budget",
        r"\b(?:до\s+|от\s+)?\d[\d ]*(?:\s*-\s*\d[\d ]*)?\s*"
        r"(?:руб(?:лей|ля|ль)?\.?|₽)(?:\s*(?:/|за)\s*(?:час|урок|занятие))?",
    )
    contact = bool(
        find(
            "contact",
            PHONE_PATTERN.pattern + r"|\B@[A-Za-z][A-Za-z0-9_]{4,}\b|\bпишите\s+в\s+(?:личку|лс)\b",
        )
    )
    closed = bool(
        find(
            "closed",
            r"\b(?:уже\s+нашли|репетитор\s+найден|заявка\s+закрыта|больше\s+не\s+актуально)\b",
        )
    )
    return ExtractedFields(
        grade=grade,
        goals=goals,
        format=format_,
        location_text=location,
        urgency=urgency,
        budget_text=budgets[0].group().strip() if len(budgets) == 1 else None,
        contact_available=response_available or contact,
        closed=closed,
        evidence=tuple(evidence),
    )

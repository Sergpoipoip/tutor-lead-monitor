from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from tutor_lead_monitor.config import ProcessingConfig, ScoringConfig, load_config
from tutor_lead_monitor.domain.enums import Goal, Intent, LessonFormat, Subject, Urgency
from tutor_lead_monitor.processing.classify import classify
from tutor_lead_monitor.processing.deduplicate import compatible, near_duplicate, token_similarity
from tutor_lead_monitor.processing.extract import extract
from tutor_lead_monitor.processing.models import Classification, ExtractedFields
from tutor_lead_monitor.processing.normalize import canonical_url, normalize
from tutor_lead_monitor.processing.score import score

NOW = datetime(2026, 1, 1, 13, tzinfo=UTC)


@pytest.fixture
def rules() -> ProcessingConfig:
    return load_config(Path("config")).processing


def test_normalization_preserves_readable_information_and_stable_fingerprint() -> None:
    original = "  Репост из родительского чата\nИщу репетитора — всё ОК!\u200b  "
    first = normalize(original, ("Репост из родительского чата",))
    second = normalize("ищу РЕПЕТИТОРА, все ок")
    assert first.readable == "Репост из родительского чата Ищу репетитора - всё ОК!"
    assert first.fingerprint_text == second.fingerprint_text
    assert first.exact_fingerprint == second.exact_fingerprint
    assert len(first.exact_fingerprint) == 64
    assert "\n" in original
    assert normalize(first.readable, ("Репост из родительского чата",)) == first


def test_unicode_url_phone_normalization() -> None:
    a = normalize(
        "Ищу　репетитора Ａ +7 (999) 123-45-67 https://EXAMPLE.invalid/p?utm_source=x&id=1."
    )
    b = normalize("Ищу репетитора A 8 999 123 45 67 https://example.invalid/p?id=1&utm_source=y")
    assert a.exact_fingerprint == b.exact_fingerprint
    assert "phonetoken" in a.fingerprint_text and "urltoken" in a.fingerprint_text
    assert "utm_source" not in a.readable
    assert "+7 (999)" in a.readable


@pytest.mark.parametrize(
    "value,expected",
    [
        (
            "https://EXAMPLE.invalid:443/p?b=2&utm_source=x&a=1#section",
            "https://example.invalid/p?a=1&b=2",
        ),
        ("http://example.invalid:80", "http://example.invalid/"),
        ("https://example.invalid:8443/p?id=1", "https://example.invalid:8443/p?id=1"),
        ("https://example.invalid:bad", None),
        ("javascript:alert(1)", None),
        ("https://user:password@example.invalid/p", None),
        (None, None),
    ],
)
def test_canonical_urls(value: str | None, expected: str | None) -> None:
    assert canonical_url(value) == expected


@pytest.mark.parametrize(
    "text,intent,subject",
    [
        ("Ищу репетитора по литературе, 10 класс", Intent.SEEKING_TUTOR, Subject.LITERATURE),
        (
            "Посоветуйте, кто может помочь дочери с литературой?",
            Intent.SEEKING_TUTOR,
            Subject.LITERATURE,
        ),
        ("Нужно подтянуть литературу", Intent.SEEKING_TUTOR, Subject.LITERATURE),
        (
            "Я репетитор по литературе, набираю учеников",
            Intent.OFFERING_TUTORING,
            Subject.LITERATURE,
        ),
        (
            "Образовательный центр: набор на курс литературы",
            Intent.SCHOOL_OR_AGENCY_AD,
            Subject.LITERATURE,
        ),
        ("Школа ищет учителя литературы в штат", Intent.TEACHING_JOB, Subject.LITERATURE),
        ("Опубликовано расписание по литературе", Intent.INFORMATIONAL, Subject.LITERATURE),
        ("Литература интересна", Intent.UNCERTAIN, Subject.LITERATURE),
        (
            "Нужен репетитор, русский и литература",
            Intent.SEEKING_TUTOR,
            Subject.RUSSIAN_AND_LITERATURE,
        ),
        ("Нужен репетитор по русскому языку", Intent.SEEKING_TUTOR, Subject.RUSSIAN_LANGUAGE),
        ("Ищу репетитора по математике", Intent.SEEKING_TUTOR, Subject.OTHER),
        ("Привет всем", Intent.UNCERTAIN, Subject.UNKNOWN),
        ("Не ищу репетитора по литературе", Intent.UNCERTAIN, Subject.LITERATURE),
    ],
)
def test_classification(
    text: str, intent: Intent, subject: Subject, rules: ProcessingConfig
) -> None:
    result = classify(text, rules)
    assert (result.intent, result.subject) == (intent, subject)
    assert 0 <= result.confidence <= 1
    assert result.version == rules.version
    assert all(0 <= e.start < e.end <= len(text) for e in result.evidence)
    assert set(result.matched_rule_ids) == {e.rule_id for e in result.evidence}


def test_rule_configuration_and_evidence(rules: ProcessingConfig) -> None:
    text = "Ищу репетитора по литературе"
    result = classify(text, rules)
    evidence = next(e for e in result.evidence if e.rule_id == "intent_seek_explicit")
    assert text[evidence.start : evidence.end] == "Ищу репетитора"
    data = rules.model_dump()
    data["rules"] = [r for r in data["rules"] if r["intent"] != Intent.SEEKING_TUTOR]
    assert classify(text, ProcessingConfig.model_validate(data)).intent == Intent.UNCERTAIN
    with pytest.raises(ValidationError):
        ProcessingConfig.model_validate(
            {"rules": [{"id": "broken", "pattern": "[", "intent": "uncertain"}]}
        )
    with pytest.raises(ValidationError):
        ProcessingConfig(rules=[rules.rules[0], rules.rules[0]])


@pytest.mark.parametrize(
    "text,grade",
    [
        ("10 класс", 10),
        ("в 9-м классе", 9),
        ("grade 11", 11),
        ("12 класс", None),
        ("0 класс", None),
        ("9-10 класс", None),
        ("9 класс и 10 класс", None),
        ("10 лет", None),
    ],
)
def test_grade_extraction(text: str, grade: int | None) -> None:
    assert extract(text).grade == grade


def test_extraction_fields_and_evidence() -> None:
    text = (
        "Срочно! город Москва, 10 класс, ЕГЭ, ВСОШ и олимпиада, "
        "онлайн или очно, до 2000 руб за час. Пишите в личку."
    )
    fields = extract(text)
    assert fields.grade == 10
    assert fields.goals == (Goal.EGE, Goal.OLYMPIAD, Goal.VSOSH)
    assert fields.format == LessonFormat.EITHER
    assert fields.location_text == "Москва"
    assert fields.urgency == Urgency.HIGH
    assert fields.budget_text == "до 2000 руб за час"
    assert fields.contact_available
    assert all(text[e.start : e.end] for e in fields.evidence)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("онлайн", LessonFormat.ONLINE),
        ("офлайн", LessonFormat.OFFLINE),
        ("не онлайн, только очно", LessonFormat.OFFLINE),
        ("как угодно", LessonFormat.UNKNOWN),
    ],
)
def test_format_extraction(text: str, expected: LessonFormat) -> None:
    assert extract(text).format == expected


def test_unknowns_and_other_goals() -> None:
    assert extract("Ничего не указано") == ExtractedFields()
    assert extract("Не срочно").urgency == Urgency.LOW
    assert extract("Репетитор найден").closed
    assert extract(
        "Нужно подтянуть школьную программу, сочинение, ОГЭ, поступление для взрослых"
    ).goals == (Goal.SCHOOL_SUPPORT, Goal.ESSAY, Goal.OGE, Goal.ADMISSIONS, Goal.ADULT_STUDY)
    assert extract("", response_available=True).contact_available
    assert extract("Телефон +7 (999) 123-45-67").contact_available
    assert extract("10 класс, телефон +7 (999) 123-45-67, до 1000-2000 руб").grade == 10


@pytest.mark.parametrize("feature", list(ScoringConfig().weights.model_dump()))
def test_every_scoring_feature(feature: str) -> None:
    classification = Classification(Intent.UNCERTAIN, Subject.UNKNOWN, 0, (), (), "test")
    fields = ExtractedFields()
    published: datetime | None = None
    if feature in {"seeking_tutor", "offering_tutoring", "school_or_agency_ad", "teaching_job"}:
        classification = replace(classification, intent=Intent(feature))
    elif feature in {"literature", "russian_and_literature"}:
        classification = replace(classification, subject=Subject(feature))
    elif feature == "exam":
        fields = replace(fields, goals=(Goal.EGE, Goal.OGE))
    elif feature == "olympiad":
        fields = replace(fields, goals=(Goal.OLYMPIAD, Goal.VSOSH))
    elif feature == "online":
        fields = replace(fields, format=LessonFormat.EITHER)
    elif feature == "grade_stated":
        fields = replace(fields, grade=10)
    elif feature == "fresh":
        published = NOW - timedelta(hours=1)
    elif feature == "budget_stated":
        fields = replace(fields, budget_text="2000 руб")
    elif feature == "contact_available":
        fields = replace(fields, contact_available=True)
    elif feature == "closed_or_stale":
        fields = replace(fields, closed=True)
    config = ScoringConfig()
    result = score(classification, fields, published_at=published, now=NOW, config=config)
    assert len(result.reasons) == 1
    assert result.reasons[0].rule_id == feature
    assert result.reasons[0].delta == config.weights.model_dump()[feature]
    assert result.version == config.version
    assert result.score == max(0, min(100, result.reasons[0].delta))


def test_scoring_clamps_orders_and_uses_config(rules: ProcessingConfig) -> None:
    classification = classify("Ищу репетитора по литературе", rules)
    fields = ExtractedFields(
        grade=10,
        goals=(Goal.EGE, Goal.VSOSH),
        format=LessonFormat.ONLINE,
        budget_text="2000 руб",
        contact_available=True,
    )
    result = score(classification, fields, published_at=NOW, now=NOW, config=ScoringConfig())
    assert result.score == 100
    assert [r.rule_id for r in result.reasons][:3] == ["seeking_tutor", "literature", "exam"]
    config = ScoringConfig.model_validate({"version": "custom", "weights": {"literature": -500}})
    assert score(classification, fields, published_at=NOW, now=NOW, config=config).score == 0


@pytest.mark.parametrize(
    "hours,fresh,stale",
    [
        (0, True, False),
        (11.9, True, False),
        (12, False, False),
        (-1, False, False),
        (720, False, True),
    ],
)
def test_freshness_boundaries(hours: float, fresh: bool, stale: bool) -> None:
    classification = Classification(Intent.UNCERTAIN, Subject.UNKNOWN, 0, (), (), "test")
    published = (NOW - timedelta(hours=hours)).astimezone(timezone(timedelta(hours=3)))
    result = score(
        classification, ExtractedFields(), published_at=published, now=NOW, config=ScoringConfig()
    )
    ids = {r.rule_id for r in result.reasons}
    assert ("fresh" in ids) == fresh
    assert ("closed_or_stale" in ids) == stale
    with pytest.raises(ValueError, match="Timezone-aware"):
        score(
            classification,
            ExtractedFields(),
            published_at=published.replace(tzinfo=None),
            now=NOW,
            config=ScoringConfig(),
        )


def test_near_duplicate_threshold_and_short_generic_text() -> None:
    a = "один два три четыре пять шесть семь восемь девять десять"
    b = a + " одиннадцать"
    assert token_similarity(a, b) == pytest.approx(1000 / 11)
    assert near_duplicate(a, b, 90) is not None
    assert near_duplicate(a, b, 91) is None
    assert near_duplicate("ищу репетитора", "ищу репетитора", 90) is None
    assert token_similarity("", "") == 0


@pytest.mark.parametrize(
    "other",
    [
        ExtractedFields(grade=9),
        ExtractedFields(goals=(Goal.OGE,)),
        ExtractedFields(format=LessonFormat.OFFLINE),
        ExtractedFields(location_text="Казань"),
        ExtractedFields(budget_text="500 руб"),
    ],
)
def test_conflicting_attributes_stay_separate(other: ExtractedFields) -> None:
    known = ExtractedFields(
        grade=10,
        goals=(Goal.EGE,),
        format=LessonFormat.ONLINE,
        location_text="Москва",
        budget_text="2000 руб",
    )
    assert not compatible(known, other)
    assert compatible(known, ExtractedFields())

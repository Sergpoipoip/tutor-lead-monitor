"""Russian presentation labels only; persisted enums and rule IDs stay unchanged."""

from datetime import date, datetime

SUBJECTS = {
    "literature": "литература",
    "russian_and_literature": "русский язык и литература",
    "russian_language": "русский язык",
    "other": "другой предмет",
    "unknown": "предмет не указан",
}
GOALS = {
    "school_support": "школьная программа",
    "essay": "сочинение",
    "oge": "ОГЭ",
    "ege": "ЕГЭ",
    "olympiad": "олимпиада",
    "vsosh": "ВСОШ",
    "admissions": "вступительные испытания",
    "adult_study": "занятия для взрослого",
    "other": "другая цель",
}
FORMATS = {"online": "онлайн", "offline": "очно", "either": "онлайн или очно"}
URGENCY = {"low": "невысокая срочность", "normal": "обычная срочность", "high": "срочно"}
SCORE_REASONS = {
    "seeking_tutor": "ищут репетитора",
    "literature": "основной предмет — литература",
    "russian_and_literature": "русский язык и литература",
    "exam": "подготовка к ЕГЭ или ОГЭ",
    "olympiad": "олимпиада или ВСОШ",
    "online": "подходят онлайн-занятия",
    "grade_stated": "указан класс",
    "fresh": "свежее объявление",
    "budget_stated": "указан бюджет",
    "contact_available": "можно связаться с автором",
    "offering_tutoring": "реклама услуг репетитора",
    "school_or_agency_ad": "реклама школы, курсов или агентства",
    "teaching_job": "вакансия преподавателя",
    "closed_or_stale": "заявка закрыта или устарела",
}
UNKNOWN_REASON = "дополнительный фактор оценки"
COLLECTION_STATUSES = {
    "never_run": "ещё не запускался",
    "running": "выполняется",
    "succeeded": "завершён успешно",
    "partial": "завершён частично",
    "failed": "завершён с ошибкой",
}
ACTIONS = {"i": "Интересно", "n": "Не подходит", "d": "Дубликат", "c": "Закрыто"}


def display_date(value: date) -> str:
    return f"{value.day:02d}.{value.month:02d}.{value.year:04d}"


def display_datetime(value: datetime) -> str:
    """Caller converts to the configured timezone; no OS locale is involved."""
    return f"{display_date(value)} в {value.hour:02d}:{value.minute:02d}"


def yes_no(value: bool) -> str:
    return "да" if value else "нет"

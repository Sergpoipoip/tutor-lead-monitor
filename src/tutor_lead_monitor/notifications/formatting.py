import html
import unicodedata
from datetime import date, datetime
from uuid import UUID

from tutor_lead_monitor.domain.models import utc
from tutor_lead_monitor.notifications.base import Button, LeadView, Message
from tutor_lead_monitor.notifications.russian import (
    ACTIONS,
    FORMATS,
    GOALS,
    SCORE_REASONS,
    SUBJECTS,
    UNKNOWN_REASON,
    URGENCY,
    display_date,
)
from tutor_lead_monitor.processing.normalize import canonical_url

MESSAGE_LIMIT = 3800  # Conservative UTF-16 bound, including markup/entities.


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def safe(text: str, budget: int = 100) -> str:
    """Escape whole Unicode clusters; never cut an entity, combining mark or emoji join."""
    clusters: list[str] = []
    for char in text:
        if unicodedata.category(char) == "Cs" or (
            unicodedata.category(char) == "Cc" and char not in "\n\r\t"
        ):
            char = " "
        continuation = (
            unicodedata.combining(char)
            or char in {"\ufe0f", "\ufe0e", "\u200d"}
            or 0x1F3FB <= ord(char) <= 0x1F3FF
            or (clusters and clusters[-1].endswith("\u200d"))
            or (
                0x1F1E6 <= ord(char) <= 0x1F1FF
                and clusters
                and len(clusters[-1]) == 1
                and 0x1F1E6 <= ord(clusters[-1]) <= 0x1F1FF
            )
        )
        if clusters and continuation:
            clusters[-1] += char
        else:
            clusters.append(char)
    output = ""
    for cluster in clusters:
        escaped = html.escape(cluster)
        if units(output + escaped) > budget - 1:
            return output + "…"
        output += escaped
    return output


def age(published: datetime | None, now: datetime) -> str:
    instant = utc(now)
    if published is None:
        return "время публикации неизвестно"
    seconds = max(0, int((instant - utc(published)).total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    if seconds < 86400:
        return f"{seconds // 3600} ч назад"
    return f"{seconds // 86400} дн назад"


def feedback_buttons(lead_id: UUID) -> tuple[Button, ...]:
    return tuple(Button(label, f"{action}:{lead_id.hex}") for action, label in ACTIONS.items())


def lead_card(lead: LeadView, now: datetime) -> str:
    fields = [SUBJECTS.get(lead.subject, "предмет не указан")]
    if lead.grade is not None:
        fields.append(f"{lead.grade} класс")
    fields.extend(GOALS.get(goal, "цель не указана") for goal in lead.goals)
    if lead.format != "unknown":
        fields.append(FORMATS.get(lead.format, "формат не указан"))
    if lead.urgency != "unknown":
        fields.append(URGENCY.get(lead.urgency, "срочность не указана"))
    parts = [f"<b>{lead.score}/100</b> · {safe(' · '.join(fields), 300)}"]
    if lead.location:
        parts.append(f"Место: {safe(lead.location)}")
    if lead.budget:
        parts.append(f"Бюджет: {safe(lead.budget)}")
    reasons = [SCORE_REASONS.get(reason, UNKNOWN_REASON) for reason in lead.reason_ids[:3]]
    parts.append(f"Почему подходит: {safe('; '.join(reasons) or 'нет данных', 240)}")
    parts.append(f"{safe(lead.source, 120)} · {age(lead.published_at, now)}")
    parts.append(safe(lead.excerpt, 700))
    url = canonical_url(lead.url)
    if url and not any(ord(c) < 32 for c in url) and units(html.escape(url)) <= 1200:
        parts.append(f'<a href="{html.escape(url, quote=True)}">Открыть оригинал</a>')
    return "\n".join(parts)


def alert(lead: LeadView, now: datetime) -> Message:
    return Message(lead_card(lead, now), feedback_buttons(lead.id))


def digest_header(
    day: date,
    timezone_label: str,
    collected: int,
    rejected: int,
    new_leads: int,
    bands: tuple[int, int, int],
    eligible: int,
) -> str:
    day_label = display_date(day)
    return (
        f"Дайджест за {day_label} · {timezone_label}\n"
        f"Статистика за календарный день {day_label}\n"
        f"Собрано: {collected}; отсеяно: {rejected}; новых заявок: {new_leads}\n"
        f"Срочные: {bands[0]}; для дайджеста: {bands[1]}; "
        f"на проверку: {bands[2]}; в подборке: {eligible}"
    )


def digest_messages(header: str, leads: tuple[LeadView, ...], now: datetime) -> tuple[Message, ...]:
    messages: list[Message] = []
    body = safe(header, 600)
    buttons: tuple[Button, ...] = ()
    for index, lead in enumerate(leads, 1):
        card = f"<b>Заявка {index}</b>\n" + lead_card(lead, now)
        if units(body + "\n\n" + card) > MESSAGE_LIMIT or len(buttons) >= 80:
            messages.append(Message(body, buttons))
            body, buttons = "Продолжение дайджеста", ()
        body += "\n\n" + card
        buttons += tuple(Button(f"{index} {b.label}", b.data) for b in feedback_buttons(lead.id))
    messages.append(Message(body, buttons))
    return tuple(messages)

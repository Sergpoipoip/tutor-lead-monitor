from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID
from xml.etree import ElementTree

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine
from telegram import Bot
from telegram.error import (
    BadRequest,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

from tutor_lead_monitor.application.bot import OwnerAccess, callback, command
from tutor_lead_monitor.config import (
    BusinessConfig,
    ConfigError,
    ScoringWeights,
    Settings,
    load_config,
)
from tutor_lead_monitor.db.bot_state import StatusView
from tutor_lead_monitor.db.locks import AlreadyRunning
from tutor_lead_monitor.notifications.base import (
    DeliveryError,
    FailureKind,
    FakeNotifier,
    LeadView,
    Message,
)
from tutor_lead_monitor.notifications.digest import day_bounds, next_digest
from tutor_lead_monitor.notifications.formatting import (
    age,
    alert,
    digest_header,
    digest_messages,
    safe,
    units,
)
from tutor_lead_monitor.notifications.russian import (
    COLLECTION_STATUSES,
    FORMATS,
    GOALS,
    SCORE_REASONS,
    SUBJECTS,
    UNKNOWN_REASON,
    URGENCY,
)
from tutor_lead_monitor.notifications.telegram import TelegramNotifier, classify_error

NOW = datetime(2026, 3, 29, 10, tzinfo=UTC)


def lead_view() -> LeadView:
    return LeadView(
        UUID(int=1),
        94,
        ("seeking_tutor", "literature"),
        "literature",
        10,
        ("ege",),
        "online",
        "Rome",
        "high",
        "2000 руб",
        "fixture",
        NOW,
        'Ищу <репетитора> & "учителя" 👩‍👩‍👧‍👦',
        'https://example.invalid/?x=1&y="value"',
    )


@pytest.mark.parametrize(
    "day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (date(2026, 1, 1), 24)]
)
def test_rome_day_boundaries(day: date, hours: int) -> None:
    start, end = day_bounds(day, "Europe/Rome")
    assert (end - start).total_seconds() == hours * 3600
    assert start.tzinfo == end.tzinfo == UTC


@pytest.mark.parametrize(
    "instant,expected",
    [
        ("2026-03-28T10:00:00+00:00", "2026-03-29T07:00:00+00:00"),
        ("2026-10-24T10:00:00+00:00", "2026-10-25T08:00:00+00:00"),
        ("2026-01-01T07:59:00+00:00", "2026-01-01T08:00:00+00:00"),
        ("2026-01-01T08:00:00+00:00", "2026-01-02T08:00:00+00:00"),
    ],
)
def test_next_digest(instant: str, expected: str) -> None:
    assert next_digest(datetime.fromisoformat(instant), BusinessConfig()).isoformat() == expected
    with pytest.raises(ValueError):
        next_digest(datetime(2026, 1, 1), BusinessConfig())


def test_html_escape_and_immutable_views() -> None:
    lead = lead_view()
    message = alert(lead, NOW)
    ElementTree.fromstring("<root>" + message.html + "</root>")
    assert "&lt;репетитора&gt;" in message.html and "&amp;" in message.html
    assert len(message.buttons) == 4
    assert all(len(b.data.encode()) <= 64 and str(lead.id.hex) in b.data for b in message.buttons)
    assert "0 мин назад" in message.html and "10 класс" in message.html
    with pytest.raises(FrozenInstanceError):
        lead.score = 0  # type: ignore[misc]
    assert "репетитора" not in repr(lead) + repr(message)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///tmp/x",
        "https://u:p@example.invalid",
        "https://example.invalid/" + "x" * 2000,
    ],
)
def test_unsafe_links_are_omitted(url: str) -> None:
    assert "href=" not in alert(replace(lead_view(), url=url), NOW).html


@pytest.mark.parametrize("cluster", ["е\u0301", "😀", "👩‍👩‍👧‍👦", "🇮🇹", "👍🏽", "&"])
def test_unicode_safe_chunks(cluster: str) -> None:
    clipped = unescape(safe(cluster * 1000, 80)).removesuffix("…")
    assert clipped == cluster * (len(clipped) // len(cluster))
    leads = tuple(
        replace(lead_view(), id=UUID(int=i), excerpt=cluster * 10000) for i in range(1, 25)
    )
    messages = digest_messages("Дайджест", leads, NOW)
    assert len(messages) > 1
    assert sum(len(m.buttons) for m in messages) == 24 * 4
    for message in messages:
        assert units(message.html) <= 3800
        ElementTree.fromstring("<root>" + message.html + "</root>")


def test_secret_settings_and_telegram_only_validation() -> None:
    settings = Settings(
        database_url=SecretStr("postgresql+psycopg://localhost/test"), _env_file=None
    )
    with pytest.raises(ConfigError):
        settings.telegram_access()
    values = dict(
        telegram_bot_token=SecretStr("123456:synthetic_token"),
        telegram_allowed_user_ids=SecretStr("123,456"),
        telegram_recipient_chat_id=SecretStr("123"),
    )
    settings = settings.model_copy(update=values)
    assert settings.telegram_access() == ("123456:synthetic_token", frozenset({123, 456}), 123)
    assert "synthetic_token" not in repr(settings)
    assert "123,456" not in repr(settings)
    for field, value in [
        ("telegram_recipient_chat_id", "-123"),
        ("telegram_allowed_user_ids", ""),
        ("telegram_bot_token", "REPLACE_WITH_TOKEN"),
        ("telegram_recipient_chat_id", "999"),
    ]:
        invalid = settings.model_copy(update={field: SecretStr(value)})
        with pytest.raises(ConfigError) as caught:
            invalid.telegram_access()
        assert "synthetic_token" not in str(caught.value)


@pytest.mark.parametrize("name", ["start", "help", "status", "digest", "pause", "resume"])
async def test_unauthorized_commands_never_access_database(name: str) -> None:
    fake = FakeNotifier()
    for actor, chat in [(999, 999), (123, -1)]:
        await command(
            cast(Engine, None),
            load_config(Path("config")),
            OwnerAccess(frozenset({123}), 123),
            fake,
            actor=actor,
            chat=chat,
            name=name,
        )
    assert fake.calls == []


async def test_unauthorized_and_invalid_callbacks_are_acknowledged() -> None:
    fake = FakeNotifier()
    access = OwnerAccess(frozenset({123}), 123)
    await callback(
        cast(Engine, None),
        access,
        fake,
        actor=999,
        chat=123,
        callback_id="a",
        data="private-sentinel",
    )
    await callback(
        cast(Engine, None),
        access,
        fake,
        actor=123,
        chat=123,
        callback_id="b",
        data="private-sentinel",
    )
    assert fake.answers == [("a", "Нет доступа."), ("b", "Некорректный отзыв.")]


@pytest.mark.parametrize(
    "error,kind",
    [
        (RetryAfter(5), FailureKind.RATE_LIMITED),
        (Forbidden("secret"), FailureKind.AUTHORIZATION),
        (InvalidToken("secret"), FailureKind.AUTHORIZATION),
        (BadRequest("secret"), FailureKind.MALFORMED),
        (TimedOut("secret"), FailureKind.AMBIGUOUS),
        (NetworkError("secret"), FailureKind.AMBIGUOUS),
        (TelegramError("secret"), FailureKind.PERMANENT),
        (TimeoutError("secret"), FailureKind.AMBIGUOUS),
    ],
)
def test_transport_error_categories(error: Exception, kind: FailureKind) -> None:
    classified = classify_error(error)
    assert classified.kind == kind and "secret" not in str(classified)
    if kind == FailureKind.RATE_LIMITED:
        assert classified.retry_after == 5


def test_known_connect_failure_is_safe_to_retry() -> None:
    error = NetworkError("private")
    error.__cause__ = httpx.ConnectTimeout("private")
    assert classify_error(error).kind == FailureKind.RETRYABLE


async def test_telegram_adapter_uses_html_timeouts_and_safe_errors() -> None:
    calls: list[dict[str, object]] = []

    class FakeBot:
        async def send_message(self, **kwargs: object) -> SimpleNamespace:
            calls.append(kwargs)
            if len(calls) == 2:
                raise BadRequest("token-and-full-post-sentinel")
            return SimpleNamespace(message_id=42)

    adapter = TelegramNotifier(cast(Bot, FakeBot()))
    assert await adapter.send(123, alert(lead_view(), NOW)) == "42"
    assert calls[0]["parse_mode"] == "HTML" and calls[0]["read_timeout"] == 20
    with pytest.raises(DeliveryError) as caught:
        await adapter.send(123, Message("synthetic"))
    assert str(caught.value) == "malformed" and caught.value.__suppress_context__


async def test_status_handles_database_failure_without_exposing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object) -> None:
        raise RuntimeError("token-and-contact-sentinel")

    monkeypatch.setattr("tutor_lead_monitor.db.bot_state.status_view", fail)
    fake = FakeNotifier()
    await command(
        cast(Engine, None),
        load_config(Path("config")),
        OwnerAccess(frozenset({123}), 123),
        fake,
        actor=123,
        chat=123,
        name="status",
        now=NOW,
    )
    assert "База данных: недоступна" in fake.sent[0][1].html
    assert "sentinel" not in fake.sent[0][1].html


def test_telegram_cli_requires_access_settings_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tutor_lead_monitor.cli import main

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost/unused")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    for name in ("telegram-bot", "notify-immediate", "send-digest"):
        assert main([name]) == 1


def test_complete_russian_card_and_unchanged_callback_actions() -> None:
    lead = replace(lead_view(), source="Тестовый источник", location="Рим", excerpt="Ищу учителя")
    message = alert(lead, NOW)
    rendered = "".join(ElementTree.fromstring("<root>" + message.html + "</root>").itertext())
    assert rendered == (
        "94/100 · литература · 10 класс · ЕГЭ · онлайн · срочно\n"
        "Место: Рим\nБюджет: 2000 руб\n"
        "Почему подходит: ищут репетитора; основной предмет — литература\n"
        "Тестовый источник · 0 мин назад\nИщу учителя\nОткрыть оригинал"
    )
    assert [b.label for b in message.buttons] == ["Интересно", "Не подходит", "Дубликат", "Закрыто"]
    assert [b.data for b in message.buttons] == [f"{a}:{lead.id.hex}" for a in "indc"]


@pytest.mark.parametrize(
    "field,labels",
    [
        ("subject", SUBJECTS),
        ("goals", GOALS),
        ("format", FORMATS),
        ("urgency", URGENCY),
    ],
)
def test_all_extracted_enum_labels_are_russian(field: str, labels: dict[str, str]) -> None:
    for key, label in labels.items():
        lead = lead_view()
        lead = replace(
            lead,
            subject=key if field == "subject" else lead.subject,
            goals=(key,) if field == "goals" else lead.goals,
            format=key if field == "format" else lead.format,
            urgency=key if field == "urgency" else lead.urgency,
        )
        assert label in alert(lead, NOW).html


def test_score_reason_localization_covers_every_weight() -> None:
    assert set(SCORE_REASONS) == set(ScoringWeights.model_fields)
    for key, label in SCORE_REASONS.items():
        assert label in alert(replace(lead_view(), reason_ids=(key,)), NOW).html
    message = alert(replace(lead_view(), reason_ids=("future-private-english-reason",)), NOW)
    assert UNKNOWN_REASON in message.html and "future-private-english-reason" not in message.html


@pytest.mark.parametrize(
    "seconds,label",
    [
        (None, "время публикации неизвестно"),
        (-1, "0 мин назад"),
        (0, "0 мин назад"),
        (60, "1 мин назад"),
        (3599, "59 мин назад"),
        (3600, "1 ч назад"),
        (86399, "23 ч назад"),
        (86400, "1 дн назад"),
    ],
)
def test_russian_age(seconds: int | None, label: str) -> None:
    published = NOW - timedelta(seconds=seconds) if seconds is not None else None
    assert age(published, NOW) == label


def test_russian_header_and_continuation() -> None:
    header = digest_header(date(2026, 1, 2), "время Рима", 0, 0, 0, (0, 0, 0), 1)
    assert header == (
        "Дайджест за 02.01.2026 · время Рима\n"
        "Статистика за календарный день 02.01.2026\n"
        "Собрано: 0; отсеяно: 0; новых заявок: 0\n"
        "Срочные: 0; для дайджеста: 0; на проверку: 0; в подборке: 1"
    )
    messages = digest_messages(header, (lead_view(),) * 25, NOW)
    assert messages[0].html.startswith(header)
    assert len(messages) > 1
    assert all(m.html.startswith("Продолжение дайджеста") for m in messages[1:])
    assert "Заявка 25" in messages[-1].html


def test_non_russian_excerpt_remains_exact_after_html_escaping() -> None:
    excerpt = 'Looking  for <a tutor> & "literature".\n\tÀ bientôt! е\u0301'
    message = alert(replace(lead_view(), excerpt=excerpt), NOW)
    assert excerpt in unescape(message.html)


@pytest.mark.parametrize("paused", [False, True])
async def test_status_russian_booleans_run_statuses_and_dates(
    monkeypatch: pytest.MonkeyPatch, paused: bool
) -> None:
    state = StatusView(
        paused,
        1,
        2,
        3,
        4,
        tuple(
            ("Источник", status, None if status == "never_run" else NOW)
            for status in COLLECTION_STATUSES
        ),
    )
    monkeypatch.setattr("tutor_lead_monitor.db.bot_state.status_view", lambda *args: state)
    fake = FakeNotifier()
    await command(
        cast(Engine, None),
        load_config(Path("config")),
        OwnerAccess(frozenset({123}), 123),
        fake,
        actor=123,
        chat=123,
        name="status",
        now=NOW,
    )
    output = fake.sent[0][1].html
    assert f"Доставка приостановлена: {'да' if paused else 'нет'}" in output
    assert "29.03.2026 в 12:00 · время Рима" in output
    assert "30.03.2026 в 09:00 · время Рима" in output
    for key, value in COLLECTION_STATUSES.items():
        assert value in output and key not in output
    assert all(s not in output for s in ("True", "False", "Europe/Rome"))


@pytest.mark.parametrize(
    "error,expected",
    [
        (AlreadyRunning("private"), "Доставка занята. Повторите команду чуть позже."),
        (RuntimeError("private"), "Не удалось выполнить команду. Проверьте состояние приложения."),
    ],
)
async def test_russian_command_failures(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str
) -> None:
    def fail(*args: object) -> None:
        raise error

    monkeypatch.setattr("tutor_lead_monitor.db.bot_state.set_paused", fail)
    fake = FakeNotifier()
    await command(
        cast(Engine, None),
        load_config(Path("config")),
        OwnerAccess(frozenset({123}), 123),
        fake,
        actor=123,
        chat=123,
        name="pause",
        now=NOW,
    )
    assert fake.sent[0][1].html == expected


@pytest.mark.parametrize(
    "result,expected",
    [
        (True, "Отзыв сохранён."),
        (False, "Заявка больше недоступна."),
        (None, "Не удалось сохранить отзыв. Попробуйте ещё раз."),
    ],
)
async def test_russian_callback_results(
    monkeypatch: pytest.MonkeyPatch, result: bool | None, expected: str
) -> None:
    def apply(*args: object) -> bool:
        if result is None:
            raise RuntimeError("private")
        return result

    monkeypatch.setattr("tutor_lead_monitor.db.bot_state.feedback", apply)
    fake = FakeNotifier()
    await callback(
        cast(Engine, None),
        OwnerAccess(frozenset({123}), 123),
        fake,
        actor=123,
        chat=123,
        callback_id="test",
        data=f"i:{UUID(int=1).hex}",
    )
    assert fake.answers == [("test", expected)]

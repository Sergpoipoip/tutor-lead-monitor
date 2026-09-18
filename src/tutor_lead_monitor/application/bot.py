import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from tutor_lead_monitor.application.notify import send_digest
from tutor_lead_monitor.config import AppConfig
from tutor_lead_monitor.db import bot_state
from tutor_lead_monitor.db.locks import AlreadyRunning
from tutor_lead_monitor.domain.models import utc
from tutor_lead_monitor.notifications.base import Message, Notifier
from tutor_lead_monitor.notifications.digest import next_digest
from tutor_lead_monitor.notifications.formatting import safe
from tutor_lead_monitor.notifications.russian import COLLECTION_STATUSES, display_datetime, yes_no

HELP = (
    "Поиск заявок на занятия с репетитором\n"
    "/start — начало работы\n/help — справка\n/status — состояние системы\n"
    "/digest — сегодняшний дайджест, доступен и во время паузы\n"
    "/pause — приостановить уведомления\n/resume — возобновить уведомления"
)


@dataclass(frozen=True)
class OwnerAccess:
    allowed: frozenset[int]
    recipient: int

    def authorized(self, actor: int, chat: int) -> bool:
        return actor in self.allowed and chat == actor


async def command(
    engine: Engine,
    config: AppConfig,
    access: OwnerAccess,
    notifier: Notifier,
    *,
    actor: int,
    chat: int,
    name: str,
    now: datetime | None = None,
) -> None:
    if not access.authorized(actor, chat):
        return
    instant = utc(now) if now is not None else datetime.now(UTC)
    zone = ZoneInfo(config.business.timezone)
    timezone_label = config.business.timezone_display_name
    response = HELP
    try:
        if name == "status":
            try:
                state = bot_state.status_view(engine, access.recipient)
                collectors = []
                for source_name, status, finished in state.collectors:
                    finished_label = (
                        f"{display_datetime(finished.astimezone(zone))} · {timezone_label}"
                        if finished
                        else "нет времени завершения"
                    )
                    status_label = COLLECTION_STATUSES.get(status, "состояние неизвестно")
                    collectors.append(f"{source_name}: {status_label} ({finished_label})")
                response = (
                    f"База данных: подключена\nДоставка приостановлена: {yes_no(state.paused)}\n"
                    f"Обработка — ожидают/с ошибкой: {state.pending_processing}/"
                    f"{state.failed_processing}\n"
                    f"Уведомления — ожидают/с ошибкой: {state.pending_notifications}/"
                    f"{state.failed_notifications}\n" + "\n".join(collectors)
                )
            except Exception:
                response = "База данных: недоступна\nСостояние доставки: неизвестно"
            target = next_digest(instant, config.business).astimezone(
                ZoneInfo(config.business.timezone)
            )
            response += (
                f"\nСледующий дайджест по настройкам: {display_datetime(target)} · {timezone_label}"
                " (запуск вручную)"
            )
        elif name in {"pause", "resume"}:
            bot_state.set_paused(engine, access.recipient, name == "pause")
            response = (
                "Доставка приостановлена. Команда /digest остаётся доступной."
                if name == "pause"
                else "Доставка возобновлена."
            )
        elif name == "digest":
            result = await send_digest(
                engine,
                config,
                access.recipient,
                notifier,
                instant.astimezone(ZoneInfo(config.business.timezone)).date(),
                now=instant,
                explicit=True,
            )
            response = (
                f"Дайджест: отправлено — {result.sent}, с ошибкой — {result.failed}, "
                f"отложено — {result.deferred}, доставка занята — {yes_no(result.busy)}."
            )
    except AlreadyRunning:
        response = "Доставка занята. Повторите команду чуть позже."
    except Exception:
        response = "Не удалось выполнить команду. Проверьте состояние приложения."
    await notifier.send(actor, Message(safe(response, 3700)))


async def callback(
    engine: Engine,
    access: OwnerAccess,
    notifier: Notifier,
    *,
    actor: int,
    chat: int,
    callback_id: str,
    data: str,
) -> None:
    if actor not in access.allowed or chat != access.recipient:
        await notifier.answer(callback_id, "Нет доступа.")
        return
    response = "Некорректный отзыв."
    try:
        match = re.fullmatch(r"([indc]):([0-9a-f]{32})", data)
        if match and 0 < len(callback_id) <= 128:
            applied = bot_state.feedback(engine, actor, callback_id, match[1], UUID(hex=match[2]))
            response = "Отзыв сохранён." if applied else "Заявка больше недоступна."
    except Exception:
        response = "Не удалось сохранить отзыв. Попробуйте ещё раз."
    await notifier.answer(callback_id, response)

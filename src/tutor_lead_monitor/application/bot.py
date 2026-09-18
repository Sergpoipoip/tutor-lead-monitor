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

HELP = (
    "Tutor Lead Monitor\n/start /help /status /digest /pause /resume\n"
    "/digest sends today's snapshot, including while paused."
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
    response = HELP
    try:
        if name == "status":
            try:
                state = bot_state.status_view(engine, access.recipient)
                response = (
                    f"Database: connected\nDelivery paused: {state.paused}\n"
                    f"Processing pending/failed: {state.pending_processing}/"
                    f"{state.failed_processing}\n"
                    f"Notifications pending/failed: {state.pending_notifications}/"
                    f"{state.failed_notifications}\n"
                    + "\n".join(
                        f"{key}: {status} ({finished.isoformat() if finished else 'no completion'})"
                        for key, status, finished in state.collectors
                    )
                )
            except Exception:
                response = "Database: unavailable\nDelivery state: unknown"
            target = next_digest(instant, config.business).astimezone(
                ZoneInfo(config.business.timezone)
            )
            response += (
                f"\nNext configured digest: {target.isoformat()} (manual execution; no scheduler)"
            )
        elif name in {"pause", "resume"}:
            bot_state.set_paused(engine, access.recipient, name == "pause")
            response = (
                "Delivery paused. Explicit /digest remains available."
                if name == "pause"
                else "Delivery resumed."
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
                f"Digest: sent={result.sent}, failed={result.failed}, "
                f"deferred={result.deferred}, busy={result.busy}."
            )
    except AlreadyRunning:
        response = "Delivery is busy; retry the command shortly."
    except Exception:
        response = "Command could not complete. Check local operational status."
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
        await notifier.answer(callback_id, "Not authorized.")
        return
    response = "Invalid feedback."
    try:
        match = re.fullmatch(r"([indc]):([0-9a-f]{32})", data)
        if match and 0 < len(callback_id) <= 128:
            applied = bot_state.feedback(engine, actor, callback_id, match[1], UUID(hex=match[2]))
            response = "Feedback saved." if applied else "Lead no longer available."
    except Exception:
        response = "Feedback could not be saved. Please retry."
    await notifier.answer(callback_id, response)

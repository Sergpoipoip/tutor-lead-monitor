from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from time import monotonic
from uuid import UUID

from sqlalchemy import Engine

from tutor_lead_monitor.config import AppConfig
from tutor_lead_monitor.db import delivery
from tutor_lead_monitor.db.locks import AlreadyRunning, source_lock
from tutor_lead_monitor.domain.models import utc
from tutor_lead_monitor.notifications.base import DeliveryError, FailureKind, Notifier


@dataclass
class DeliveryResult:
    sent: int = 0
    failed: int = 0
    deferred: int = 0
    busy: bool = False


async def deliver(
    engine: Engine,
    recipient: int,
    ids: tuple[UUID, ...],
    notifier: Notifier,
    now: datetime,
    explicit: bool,
) -> DeliveryResult:
    result = DeliveryResult()
    started = monotonic()

    def clock() -> datetime:
        return now + timedelta(seconds=monotonic() - started)

    for notification_id in ids:
        if delivery.notification_status(engine, notification_id) in {"sent", "skipped"}:
            continue
        while attempt := delivery.claim_chunk(
            engine, recipient, notification_id, clock(), explicit
        ):
            try:
                provider_id = await notifier.send(recipient, attempt.message)
            except DeliveryError as error:
                delivery.finish_chunk(engine, attempt, clock(), error=error)
                break
            except Exception:
                # An unexpected transport failure cannot prove the send did not happen.
                delivery.finish_chunk(
                    engine, attempt, clock(), error=DeliveryError(FailureKind.AMBIGUOUS)
                )
                break
            delivery.finish_chunk(engine, attempt, clock(), message_id=provider_id)
        status = delivery.notification_status(engine, notification_id)
        if status == "sent":
            result.sent += 1
        elif status == "failed":
            result.failed += 1
        else:
            result.deferred += 1
    return result


async def send_immediate(
    engine: Engine,
    config: AppConfig,
    recipient: int,
    notifier: Notifier,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> DeliveryResult:
    if not 1 <= limit <= 1000:
        raise ValueError("Delivery limit must be between 1 and 1000")
    instant = utc(now) if now is not None else datetime.now(UTC)
    started = monotonic()
    try:
        with source_lock(engine, f"delivery:{recipient}"):
            ids = delivery.reserve_immediate(engine, recipient, config, instant, limit)
            return await deliver(
                engine,
                recipient,
                ids,
                notifier,
                instant + timedelta(seconds=monotonic() - started),
                False,
            )
    except AlreadyRunning:
        return DeliveryResult(busy=True)


async def send_digest(
    engine: Engine,
    config: AppConfig,
    recipient: int,
    notifier: Notifier,
    day: date,
    *,
    now: datetime | None = None,
    explicit: bool = False,
) -> DeliveryResult:
    instant = utc(now) if now is not None else datetime.now(UTC)
    started = monotonic()
    try:
        with source_lock(engine, f"delivery:{recipient}"):
            notification_id = delivery.reserve_digest(
                engine, recipient, config, day, instant, explicit
            )
            ids = (notification_id,) if notification_id else ()
            return await deliver(
                engine,
                recipient,
                ids,
                notifier,
                instant + timedelta(seconds=monotonic() - started),
                explicit,
            )
    except AlreadyRunning:
        return DeliveryResult(busy=True)

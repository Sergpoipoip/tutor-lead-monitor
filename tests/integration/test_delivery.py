import asyncio
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from tutor_lead_monitor.application.bot import OwnerAccess, callback, command
from tutor_lead_monitor.application.collect import run_collector
from tutor_lead_monitor.application.notify import send_digest, send_immediate
from tutor_lead_monitor.application.process import process_pending
from tutor_lead_monitor.application.retention import run_retention
from tutor_lead_monitor.cli import main
from tutor_lead_monitor.collectors.fixture import FIXTURE_TIME, FixtureCollector
from tutor_lead_monitor.config import AppConfig, load_config
from tutor_lead_monitor.db import bot_state, delivery
from tutor_lead_monitor.db.models import (
    CallbackReceipt,
    CollectionRun,
    Feedback,
    Lead,
    LeadOccurrence,
    Notification,
    NotificationChunk,
    NotificationItem,
    RawItem,
)
from tutor_lead_monitor.db.repositories import sync_source
from tutor_lead_monitor.notifications.base import DeliveryError, FailureKind, FakeNotifier, Message
from tutor_lead_monitor.notifications.digest import day_bounds

pytestmark = pytest.mark.integration
NOW = datetime(2026, 3, 29, 10, tzinfo=UTC)
OWNER = 123
ACCESS = OwnerAccess(frozenset({OWNER}), OWNER)


@pytest.fixture
def config() -> AppConfig:
    return load_config(Path("config"))


def seed(
    engine: Engine,
    config: AppConfig,
    *,
    score: int = 95,
    status: str = "new",
    when: datetime = NOW,
    published: datetime = NOW,
    content: str = "Ищу <репетитора> & литература",
) -> UUID:
    with Session(engine) as session, session.begin():
        source_id = sync_source(session, config.registry.sources[0])
        raw = RawItem(
            source_id=source_id,
            external_id=str(uuid4()),
            text=content,
            url="https://example.invalid/request",
            published_at=published,
            collected_at=when,
            processing_status="processed",
            metadata_={},
        )
        session.add(raw)
        session.flush()
        lead = Lead(
            canonical_raw_item_id=raw.id,
            intent="seeking_tutor",
            subject="literature",
            score=score,
            score_reasons=[{"rule_id": "test", "delta": score, "explanation": "Synthetic reason"}],
            classification_version="test",
            scoring_version="test",
            last_seen_at=when,
            created_at=when,
            status=status,
        )
        session.add(lead)
        session.flush()
        session.add(LeadOccurrence(lead_id=lead.id, raw_item_id=raw.id, match_method="exact_id"))
        return lead.id


async def test_immediate_idempotency_and_transaction_boundary(
    migrated_engine: Engine, config: AppConfig
) -> None:
    lead_id = seed(migrated_engine, config)

    class InspectingNotifier(FakeNotifier):
        async def send(self, recipient: int, message: Message) -> str:
            with Session(migrated_engine) as session:
                envelope = session.scalars(select(Notification)).one()
                assert envelope.status == "sending" and envelope.attempt_count == 1
                assert (
                    session.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                            "AND state = 'idle in transaction'"
                        )
                    )
                    == 0
                )
            await asyncio.sleep(0)
            return await super().send(recipient, message)

    fake = InspectingNotifier()
    first = await send_immediate(migrated_engine, config, OWNER, fake, now=NOW)
    assert first.sent == 1 and len(fake.sent) == 1
    assert (await send_immediate(migrated_engine, config, OWNER, fake, now=NOW)).sent == 0
    with Session(migrated_engine) as session:
        notification = session.scalars(select(Notification)).one()
        assert notification.lead_id == lead_id and notification.status == "sent"
        assert notification.provider_message_id == "1"
        assert notification.sent_at is not None and notification.sent_at >= NOW
        chunk = session.scalars(select(NotificationChunk)).one()
        assert chunk.status == "sent" and chunk.attempt_count == 1


async def test_concurrent_immediate_sends_are_exclusive(
    migrated_engine: Engine, config: AppConfig
) -> None:
    seed(migrated_engine, config)

    class YieldingNotifier(FakeNotifier):
        async def send(self, recipient: int, message: Message) -> str:
            await asyncio.sleep(0.05)
            return await super().send(recipient, message)

    fake = YieldingNotifier()
    outcomes = await asyncio.gather(
        *(send_immediate(migrated_engine, config, OWNER, fake, now=NOW) for _ in range(2))
    )
    assert sum(o.sent for o in outcomes) == 1 and any(o.busy for o in outcomes)
    assert len(fake.calls) == 1
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(Notification)) == 1


@pytest.mark.parametrize("status", ["closed", "rejected", "duplicate", "expired"])
async def test_excluded_states_never_deliver(
    migrated_engine: Engine, config: AppConfig, status: str
) -> None:
    seed(migrated_engine, config, status=status)
    fake = FakeNotifier()
    await send_immediate(migrated_engine, config, OWNER, fake, now=NOW)
    await send_digest(migrated_engine, config, OWNER, fake, NOW.date(), now=NOW)
    assert not fake.calls


async def test_digest_order_membership_and_immediate_inclusion(
    migrated_engine: Engine, config: AppConfig
) -> None:
    low = seed(migrated_engine, config, score=55)
    old = seed(migrated_engine, config, score=90, published=NOW - timedelta(hours=1))
    fresh = seed(migrated_engine, config, score=90)
    tie = seed(migrated_engine, config, score=90)
    seed(migrated_engine, config, score=54)
    start, end = day_bounds(NOW.date(), "Europe/Rome")
    seed(migrated_engine, config, when=start - timedelta(microseconds=1))
    seed(migrated_engine, config, when=end)
    fake = FakeNotifier()
    await send_immediate(migrated_engine, config, OWNER, fake, now=NOW)
    fake.calls.clear()
    result = await send_digest(migrated_engine, config, OWNER, fake, NOW.date(), now=NOW)
    assert result.sent == 1
    ids = [
        UUID(hex=b.data[2:]) for _, m in fake.calls for b in m.buttons if b.data.startswith("i:")
    ]
    assert ids == sorted([fresh, tie]) + [old, low]
    assert "Collected: 5" in fake.calls[0][1].html and "review: 1" in fake.calls[0][1].html
    count = len(fake.calls)
    await send_digest(migrated_engine, config, OWNER, fake, NOW.date(), now=NOW)
    assert len(fake.calls) == count
    with Session(migrated_engine) as session:
        assert set(session.scalars(select(NotificationItem.lead_id))) == {fresh, tie, old, low}
        assert (
            session.scalar(
                select(func.count()).select_from(Notification).where(Notification.kind == "digest")
            )
            == 1
        )


@pytest.mark.parametrize(
    "day", [datetime(2026, 3, 29, 10, tzinfo=UTC), datetime(2026, 10, 25, 10, tzinfo=UTC)]
)
async def test_digest_dst_boundary_membership(
    migrated_engine: Engine, config: AppConfig, day: datetime
) -> None:
    start, end = day_bounds(day.date(), "Europe/Rome")
    first = seed(migrated_engine, config, when=start)
    last = seed(migrated_engine, config, when=end - timedelta(microseconds=1))
    seed(migrated_engine, config, when=start - timedelta(microseconds=1))
    seed(migrated_engine, config, when=end)
    await send_digest(migrated_engine, config, OWNER, FakeNotifier(), day.date(), now=day)
    with Session(migrated_engine) as session:
        assert set(session.scalars(select(NotificationItem.lead_id))) == {first, last}


async def test_empty_digest_does_not_seal_the_day(
    migrated_engine: Engine, config: AppConfig
) -> None:
    fake = FakeNotifier()
    await send_digest(migrated_engine, config, OWNER, fake, NOW.date(), now=NOW)
    assert not fake.calls
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(Notification)) == 0
    seed(migrated_engine, config, score=61)
    await send_digest(migrated_engine, config, OWNER, fake, NOW.date(), now=NOW)
    assert fake.calls


async def test_digest_partial_failure_resumes_frozen_chunks(
    migrated_engine: Engine, config: AppConfig
) -> None:
    for _ in range(9):
        seed(migrated_engine, config, content="😀 & <литература> " * 300)
    fake = FakeNotifier(outcomes=[None, DeliveryError(FailureKind.RATE_LIMITED, retry_after=60)])
    result = await send_digest(migrated_engine, config, OWNER, fake, NOW.date(), now=NOW)
    assert result.failed == 1 and len(fake.sent) == 1 and len(fake.calls) == 2
    with Session(migrated_engine) as session:
        envelope = session.scalars(select(Notification)).one()
        notification_id = envelope.id
        chunks = session.scalars(
            select(NotificationChunk).order_by(NotificationChunk.position)
        ).all()
        assert len(chunks) >= 3 and chunks[0].status == "sent" and chunks[1].status == "failed"
        assert chunks[1].next_attempt_at is not None
        assert chunks[1].next_attempt_at >= NOW + timedelta(seconds=60)
        snapshots = [chunk.text for chunk in chunks]
    # A later arrival cannot change an already delivered digest's ordering or membership.
    seed(migrated_engine, config)
    await send_digest(
        migrated_engine, config, OWNER, fake, NOW.date(), now=NOW + timedelta(seconds=59)
    )
    assert len(fake.calls) == 2
    result = await send_digest(
        migrated_engine, config, OWNER, fake, NOW.date(), now=NOW + timedelta(seconds=61)
    )
    assert result.sent == 1
    assert [message.html for _, message in fake.sent] == snapshots
    await send_digest(
        migrated_engine, config, OWNER, fake, NOW.date(), now=NOW + timedelta(hours=1)
    )
    assert len(fake.sent) == len(snapshots)
    with Session(migrated_engine) as session:
        assert session.scalars(select(Notification)).one().id == notification_id
        assert session.scalar(select(func.count()).select_from(NotificationItem)) == 9


@pytest.mark.parametrize("kind", list(FailureKind))
async def test_failures_are_sanitized_bounded_and_do_not_block_others(
    migrated_engine: Engine,
    config: AppConfig,
    kind: FailureKind,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(migrated_engine, config)
    seed(migrated_engine, config)
    fake = FakeNotifier(outcomes=[DeliveryError(kind, retry_after=10), None])
    result = await send_immediate(migrated_engine, config, OWNER, fake, now=NOW)
    assert result.failed == result.sent == 1
    initial_calls = len(fake.calls)
    retryable = kind in {FailureKind.RETRYABLE, FailureKind.RATE_LIMITED}
    for hours in range(1, 5):
        fake.outcomes = [DeliveryError(kind)]
        await send_immediate(migrated_engine, config, OWNER, fake, now=NOW + timedelta(hours=hours))
    assert len(fake.calls) == initial_calls + (2 if retryable else 0)
    with Session(migrated_engine) as session:
        failed = session.scalars(select(Notification).where(Notification.status == "failed")).one()
        assert failed.last_error == kind.value
        assert failed.attempt_count == (3 if retryable else 1)
        assert session.scalar(select(func.count()).select_from(Notification)) == 2
    assert "репетитора" not in capsys.readouterr().err


async def test_interrupted_sending_is_held_not_resent(
    migrated_engine: Engine, config: AppConfig
) -> None:
    seed(migrated_engine, config)
    ids = delivery.reserve_immediate(migrated_engine, OWNER, config, NOW, 1)
    assert delivery.claim_chunk(migrated_engine, OWNER, ids[0], NOW, False)
    fake = FakeNotifier()
    await send_immediate(migrated_engine, config, OWNER, fake, now=NOW + timedelta(minutes=1))
    assert not fake.calls
    with Session(migrated_engine) as session:
        assert session.scalars(select(NotificationChunk)).one().status == "ambiguous"
        assert session.scalars(select(Notification)).one().last_error == "ambiguous"


async def test_retry_after_starts_after_the_failed_response(
    migrated_engine: Engine, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(migrated_engine, config)
    elapsed = 0.0
    reserve = delivery.reserve_immediate

    def slow_reservation(
        engine: Engine, recipient: int, app: AppConfig, now: datetime, limit: int
    ) -> tuple[UUID, ...]:
        nonlocal elapsed
        elapsed += 20
        return reserve(engine, recipient, app, now, limit)

    class SlowNotifier(FakeNotifier):
        async def send(self, recipient: int, message: Message) -> str:
            nonlocal elapsed
            elapsed += 30
            raise DeliveryError(FailureKind.RATE_LIMITED, retry_after=10)

    monkeypatch.setattr("tutor_lead_monitor.application.notify.monotonic", lambda: elapsed)
    monkeypatch.setattr(delivery, "reserve_immediate", slow_reservation)
    await send_immediate(migrated_engine, config, OWNER, SlowNotifier(), now=NOW)
    with Session(migrated_engine) as session:
        assert session.scalars(select(NotificationChunk)).one().next_attempt_at == NOW + timedelta(
            seconds=60
        )


async def test_feedback_replay_and_changes_preserve_score_history(
    migrated_engine: Engine, config: AppConfig
) -> None:
    lead_id = seed(migrated_engine, config)
    fake = FakeNotifier()
    with Session(migrated_engine) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        original = lead.score_reasons
    for i, (action, expected) in enumerate(
        [("i", "interested"), ("n", "rejected"), ("d", "duplicate"), ("c", "closed")]
    ):
        await callback(
            migrated_engine,
            ACCESS,
            fake,
            actor=OWNER,
            chat=OWNER,
            callback_id=str(i),
            data=f"{action}:{lead_id.hex}",
        )
        with Session(migrated_engine) as session:
            lead = session.get(Lead, lead_id)
            assert lead is not None and lead.status == expected and lead.score_reasons == original
    # Replayed old update does not undo a newer choice. A repeated same-action click is a no-op.
    for callback_id, action in [("0", "i"), ("same-action", "c"), ("same-action", "c")]:
        await callback(
            migrated_engine,
            ACCESS,
            fake,
            actor=OWNER,
            chat=OWNER,
            callback_id=callback_id,
            data=f"{action}:{lead_id.hex}",
        )
    await callback(
        migrated_engine,
        ACCESS,
        fake,
        actor=999,
        chat=OWNER,
        callback_id="unauthorized",
        data=f"i:{lead_id.hex}",
    )
    assert len(fake.answers) == 8
    with Session(migrated_engine) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.status == "closed"
        assert session.scalar(select(func.count()).select_from(Feedback)) == 4
        assert session.scalar(select(func.count()).select_from(CallbackReceipt)) == 5


async def test_pause_persists_and_explicit_digest_is_allowed(
    migrated_engine: Engine, config: AppConfig
) -> None:
    seed(migrated_engine, config)
    fake = FakeNotifier()
    await command(
        migrated_engine, config, ACCESS, fake, actor=OWNER, chat=OWNER, name="pause", now=NOW
    )
    migrated_engine.dispose()  # A new connection/process sees persisted pause state.
    assert bot_state.status_view(migrated_engine, OWNER).paused
    fake.calls.clear()
    await send_immediate(migrated_engine, config, OWNER, fake, now=NOW)
    await send_digest(migrated_engine, config, OWNER, fake, NOW.date(), now=NOW)
    assert not fake.calls
    await command(
        migrated_engine, config, ACCESS, fake, actor=OWNER, chat=OWNER, name="digest", now=NOW
    )
    assert any("Digest 2026" in m.html for _, m in fake.calls)
    await command(
        migrated_engine, config, ACCESS, fake, actor=OWNER, chat=OWNER, name="resume", now=NOW
    )
    assert not bot_state.status_view(migrated_engine, OWNER).paused
    assert (await send_immediate(migrated_engine, config, OWNER, fake, now=NOW)).sent == 1


@pytest.mark.parametrize("name", ["start", "help", "status"])
async def test_owner_commands(migrated_engine: Engine, config: AppConfig, name: str) -> None:
    seed(migrated_engine, config)
    with Session(migrated_engine) as session, session.begin():
        source_id = sync_source(session, config.registry.sources[0])
        session.add(
            CollectionRun(source_id=source_id, started_at=NOW, finished_at=NOW, status="succeeded")
        )
    fake = FakeNotifier()
    await command(
        migrated_engine, config, ACCESS, fake, actor=OWNER, chat=OWNER, name=name, now=NOW
    )
    assert len(fake.sent) == 1 and fake.sent[0][0] == OWNER
    output = fake.sent[0][1].html
    if name == "status":
        assert "Database: connected" in output and "paused: False" in output
        assert "succeeded" in output and "2026-03-30T09:00:00+02:00" in output
    else:
        assert "/digest" in output and "/pause" in output


async def test_retention_deletes_chunks_in_dependency_order(
    migrated_engine: Engine, config: AppConfig
) -> None:
    seed(migrated_engine, config)
    await send_immediate(migrated_engine, config, OWNER, FakeNotifier(), now=NOW)
    with Session(migrated_engine) as session, session.begin():
        for row in session.scalars(select(Notification)):
            row.created_at = NOW
    future = NOW + timedelta(days=200)
    dry = run_retention(migrated_engine, config.business.retention, now=future)
    assert dry.notification_chunks == dry.notifications == 1
    result = run_retention(migrated_engine, config.business.retention, now=future, dry_run=False)
    assert asdict(result) | {"dry_run": True} == asdict(dry)


async def test_fixture_to_delivery_and_repeated_runs(
    migrated_engine: Engine, config: AppConfig
) -> None:
    source = config.registry.sources[0]
    collector = FixtureCollector()
    collector.texts += (
        "Ищу репетитора по литературе, 10 класс, ЕГЭ и ВСОШ, онлайн, "
        "до 2000 руб за час. Пишите в личку.",
    )
    await run_collector(migrated_engine, source, collector)
    process_pending(migrated_engine, config, now=FIXTURE_TIME + timedelta(hours=1))
    with Session(migrated_engine) as session:
        expected = set(session.scalars(select(Lead.id).where(Lead.score >= 90)))
        assert len(expected) == 1
        created = session.scalars(select(Lead.created_at)).first()
        assert created is not None
    from zoneinfo import ZoneInfo

    local_day = created.astimezone(ZoneInfo("Europe/Rome")).date()
    fake = FakeNotifier()
    await send_immediate(migrated_engine, config, OWNER, fake, now=created)
    await send_digest(migrated_engine, config, OWNER, fake, local_day, now=created)
    initial_calls = len(fake.calls)
    await send_immediate(migrated_engine, config, OWNER, fake, now=created)
    await send_digest(migrated_engine, config, OWNER, fake, local_day, now=created)
    assert len(fake.calls) == initial_calls
    with Session(migrated_engine) as session:
        assert (
            set(
                session.scalars(
                    select(Notification.lead_id).where(Notification.kind == "immediate")
                )
            )
            == expected
        )
        assert (
            session.scalar(
                select(func.count()).select_from(Notification).where(Notification.kind == "digest")
            )
            == 1
        )


async def test_concurrent_digest_has_one_envelope(
    migrated_engine: Engine, config: AppConfig
) -> None:
    seed(migrated_engine, config)

    class YieldingNotifier(FakeNotifier):
        async def send(self, recipient: int, message: Message) -> str:
            await asyncio.sleep(0.05)
            return await super().send(recipient, message)

    fake = YieldingNotifier()
    results = await asyncio.gather(
        *(send_digest(migrated_engine, config, OWNER, fake, NOW.date(), now=NOW) for _ in range(2))
    )
    assert sum(result.sent for result in results) == 1 and len(fake.calls) == 1
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(Notification)) == 1


async def test_retained_lead_keeps_notification_idempotency(
    migrated_engine: Engine, config: AppConfig
) -> None:
    lead_id = seed(migrated_engine, config)
    fake = FakeNotifier()
    await send_immediate(migrated_engine, config, OWNER, fake, now=NOW)
    await callback(
        migrated_engine,
        ACCESS,
        fake,
        actor=OWNER,
        chat=OWNER,
        callback_id="retained",
        data=f"i:{lead_id.hex}",
    )
    with Session(migrated_engine) as session, session.begin():
        for envelope in session.scalars(select(Notification)):
            envelope.created_at = NOW
    future = NOW + timedelta(days=200)
    result = run_retention(migrated_engine, config.business.retention, now=future, dry_run=False)
    assert result.notifications == result.notification_chunks == 0
    await send_immediate(migrated_engine, config, OWNER, fake, now=future)
    assert len(fake.calls) == 1


def test_cli_delivery_uses_fake_transport_and_is_idempotent(
    migrated_engine: Engine,
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(migrated_engine, config)
    fake = FakeNotifier()

    class FakeBotContext:
        def __init__(self, token: str) -> None:
            pass

        async def __aenter__(self) -> "FakeBotContext":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

    monkeypatch.setenv("DATABASE_URL", migrated_engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:synthetic_token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", str(OWNER))
    monkeypatch.setenv("TELEGRAM_RECIPIENT_CHAT_ID", str(OWNER))
    monkeypatch.setattr("telegram.Bot", FakeBotContext)
    monkeypatch.setattr(
        "tutor_lead_monitor.notifications.telegram.TelegramNotifier", lambda bot: fake
    )
    for _ in range(2):
        assert main(["notify-immediate", "--as-of", NOW.isoformat()]) == 0
        assert (
            main(
                ["send-digest", "--local-date", NOW.date().isoformat(), "--as-of", NOW.isoformat()]
            )
            == 0
        )
    assert len(fake.calls) == 2
    output = capsys.readouterr()
    assert "synthetic_token" not in output.out + output.err
    assert "репетитора" not in output.out + output.err

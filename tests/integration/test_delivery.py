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
    NotificationMarker,
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
    outside_stats = [
        seed(migrated_engine, config, when=start - timedelta(microseconds=1)),
        seed(migrated_engine, config, when=end),
    ]
    fake = FakeNotifier()
    await send_immediate(migrated_engine, config, OWNER, fake, now=NOW)
    fake.calls.clear()
    result = await send_digest(migrated_engine, config, OWNER, fake, NOW.date(), now=NOW)
    assert result.sent == 1
    ids = [
        UUID(hex=b.data[2:]) for _, m in fake.calls for b in m.buttons if b.data.startswith("i:")
    ]
    assert ids == sorted(outside_stats) + sorted([fresh, tie]) + [old, low]
    assert "Collected: 5" in fake.calls[0][1].html and "review: 1" in fake.calls[0][1].html
    count = len(fake.calls)
    await send_digest(migrated_engine, config, OWNER, fake, NOW.date(), now=NOW)
    assert len(fake.calls) == count
    with Session(migrated_engine) as session:
        assert set(session.scalars(select(NotificationItem.lead_id))) == {
            fresh,
            tie,
            old,
            low,
            *outside_stats,
        }
        assert (
            session.scalar(
                select(func.count()).select_from(Notification).where(Notification.kind == "digest")
            )
            == 1
        )


@pytest.mark.parametrize(
    "day", [datetime(2026, 3, 29, 10, tzinfo=UTC), datetime(2026, 10, 25, 10, tzinfo=UTC)]
)
async def test_digest_dst_statistics_are_separate_from_backlog(
    migrated_engine: Engine, config: AppConfig, day: datetime
) -> None:
    start, end = day_bounds(day.date(), "Europe/Rome")
    first = seed(migrated_engine, config, when=start)
    last = seed(migrated_engine, config, when=end - timedelta(microseconds=1))
    earlier = seed(migrated_engine, config, when=start - timedelta(microseconds=1))
    later = seed(migrated_engine, config, when=end)
    fake = FakeNotifier()
    await send_digest(migrated_engine, config, OWNER, fake, day.date(), now=day)
    assert "Collected: 2" in fake.calls[0][1].html
    assert "eligible: 4" in fake.calls[0][1].html
    with Session(migrated_engine) as session:
        assert set(session.scalars(select(NotificationItem.lead_id))) == {
            first,
            last,
            earlier,
            later,
        }


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
    late = seed(migrated_engine, config)
    await send_digest(
        migrated_engine, config, OWNER, fake, NOW.date(), now=NOW + timedelta(seconds=59)
    )
    assert len(fake.calls) == 2
    tomorrow = NOW + timedelta(days=1)
    next_digest = FakeNotifier()
    await send_digest(migrated_engine, config, OWNER, next_digest, tomorrow.date(), now=tomorrow)
    assert len(next_digest.sent) == 1
    assert [
        UUID(hex=b.data[2:])
        for _, message in next_digest.sent
        for b in message.buttons
        if b.data.startswith("i:")
    ] == [late]
    result = await send_digest(migrated_engine, config, OWNER, fake, NOW.date(), now=tomorrow)
    assert result.sent == 1
    assert [message.html for _, message in fake.sent] == snapshots
    await send_digest(
        migrated_engine, config, OWNER, fake, NOW.date(), now=tomorrow + timedelta(hours=1)
    )
    assert len(fake.sent) == len(snapshots)
    with Session(migrated_engine) as session:
        assert session.get(Notification, notification_id) is not None
        assert session.scalar(select(func.count()).select_from(Notification)) == 2
        assert session.scalar(select(func.count()).select_from(NotificationItem)) == 10


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
    assert result.notifications == result.notification_chunks == 1
    with Session(migrated_engine) as session:
        assert session.get(Lead, lead_id) is not None
        assert session.scalar(select(func.count()).select_from(Feedback)) == 1
        assert session.scalar(select(func.count()).select_from(Notification)) == 0
        assert session.scalar(select(func.count()).select_from(NotificationChunk)) == 0
        assert session.scalar(select(func.count()).select_from(NotificationMarker)) == 1
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


async def test_late_arrival_is_delivered_once_in_next_digest(
    migrated_engine: Engine, config: AppConfig
) -> None:
    nine_rome = NOW.replace(hour=7)  # 09:00 CEST, on the spring DST day.
    first = seed(migrated_engine, config, when=nine_rome - timedelta(hours=1))
    fake = FakeNotifier()
    await send_digest(migrated_engine, config, OWNER, fake, nine_rome.date(), now=nine_rome)
    frozen = [m.html for _, m in fake.sent]
    late = seed(migrated_engine, config, when=nine_rome + timedelta(hours=2))
    await send_digest(
        migrated_engine, config, OWNER, fake, nine_rome.date(), now=nine_rome + timedelta(hours=3)
    )
    assert [m.html for _, m in fake.sent] == frozen
    tomorrow = nine_rome + timedelta(days=1)
    await asyncio.gather(
        *(
            send_digest(migrated_engine, config, OWNER, fake, tomorrow.date(), now=tomorrow)
            for _ in range(2)
        )
    )
    await send_digest(
        migrated_engine,
        config,
        OWNER,
        fake,
        (tomorrow + timedelta(days=1)).date(),
        now=tomorrow + timedelta(days=1),
    )
    assert len(fake.sent) == 2
    with Session(migrated_engine) as session:
        rows = session.execute(
            select(Notification.period_key, NotificationItem.lead_id).join(NotificationItem)
        ).all()
        assert set(rows) == {
            (nine_rome.date().isoformat(), first),
            (tomorrow.date().isoformat(), late),
        }
        assert len(rows) == 2


async def test_old_lead_promoted_across_threshold_enters_backlog(
    migrated_engine: Engine, config: AppConfig
) -> None:
    review = seed(migrated_engine, config, score=46, when=NOW - timedelta(days=1))
    fake = FakeNotifier()
    await send_digest(migrated_engine, config, OWNER, fake, NOW.date(), now=NOW)
    assert not fake.calls
    tomorrow = NOW + timedelta(days=1)
    with Session(migrated_engine) as session, session.begin():
        lead = session.get(Lead, review)
        assert lead is not None
        raw = session.get(RawItem, lead.canonical_raw_item_id)
        assert raw is not None
        raw.canonical_url = raw.url
        session.add(
            RawItem(
                source_id=raw.source_id,
                external_id="promoted-occurrence",
                text="Ищу репетитора по литературе, 10 класс, онлайн",
                url=raw.url,
                published_at=tomorrow,
                collected_at=tomorrow,
                metadata_={},
            )
        )
    assert process_pending(migrated_engine, config, now=tomorrow).processed == 1
    await send_digest(migrated_engine, config, OWNER, fake, tomorrow.date(), now=tomorrow)
    await send_digest(migrated_engine, config, OWNER, fake, tomorrow.date(), now=tomorrow)
    await send_digest(
        migrated_engine,
        config,
        OWNER,
        fake,
        (tomorrow + timedelta(days=1)).date(),
        now=tomorrow + timedelta(days=1),
    )
    assert len(fake.sent) == 1
    with Session(migrated_engine) as session:
        lead = session.get(Lead, review)
        assert lead is not None and lead.score >= 55 and lead.created_at < NOW
        assert list(session.scalars(select(NotificationItem.lead_id))) == [review]


async def test_digest_membership_is_once_per_recipient_not_once_per_day(
    migrated_engine: Engine, config: AppConfig
) -> None:
    lead_id = seed(migrated_engine, config)
    fake = FakeNotifier()
    await send_immediate(migrated_engine, config, OWNER, fake, now=NOW)
    for day in range(3):
        instant = NOW + timedelta(days=day)
        for recipient in (OWNER, OWNER + 1):
            await send_digest(migrated_engine, config, recipient, fake, instant.date(), now=instant)
    assert len(fake.sent) == 3  # Immediate plus one digest for each recipient.
    with Session(migrated_engine) as session:
        rows = session.execute(
            select(Notification.recipient_key, NotificationItem.lead_id).join(NotificationItem)
        ).all()
        assert set(rows) == {(str(OWNER), lead_id), (str(OWNER + 1), lead_id)} and len(rows) == 2


@pytest.mark.parametrize("status", ["pending", "sending", "failed", "sent", "skipped"])
async def test_expired_digest_payload_removed_but_feedback_and_dedup_survive(
    migrated_engine: Engine, config: AppConfig, status: str
) -> None:
    lead_id = seed(migrated_engine, config, content="payload-contact-sentinel")
    fake = FakeNotifier()
    await send_immediate(migrated_engine, config, OWNER, fake, now=NOW)
    await send_digest(migrated_engine, config, OWNER, fake, NOW.date(), now=NOW)
    await callback(
        migrated_engine,
        ACCESS,
        fake,
        actor=OWNER,
        chat=OWNER,
        callback_id="protected",
        data=f"i:{lead_id.hex}",
    )
    with Session(migrated_engine) as session, session.begin():
        for envelope in session.scalars(select(Notification)):
            envelope.created_at = NOW
            envelope.status = status
    future = NOW + timedelta(days=config.business.retention.notifications_days, seconds=1)
    preview = run_retention(migrated_engine, config.business.retention, now=future)
    assert preview.notifications == preview.notification_chunks == 2
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(NotificationMarker)) == 0
    result = run_retention(migrated_engine, config.business.retention, now=future, dry_run=False)
    assert result.notifications == 2 and result.notification_items == 1
    with Session(migrated_engine) as session:
        assert session.get(Lead, lead_id) is not None
        assert session.scalar(select(func.count()).select_from(Feedback)) == 1
        assert session.scalar(select(func.count()).select_from(RawItem)) == 1
        for model in (Notification, NotificationItem, NotificationChunk):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        markers = session.scalars(select(NotificationMarker)).all()
        assert {m.kind for m in markers} == {"immediate", "digest", "digest_period"}
        assert all(len(m.recipient_hash) == 64 and m.recipient_hash != str(OWNER) for m in markers)
        assert set(NotificationMarker.__table__.columns.keys()) == {
            "recipient_hash",
            "kind",
            "scope_key",
        }
    before = len(fake.calls)
    await send_immediate(migrated_engine, config, OWNER, fake, now=future)
    await send_digest(migrated_engine, config, OWNER, fake, future.date(), now=future)
    assert len(fake.calls) == before
    new_lead = seed(migrated_engine, config, when=future)
    # The expired period itself cannot be reopened, even with new eligible leads.
    await send_digest(migrated_engine, config, OWNER, fake, NOW.date(), now=future)
    assert len(fake.calls) == before
    await send_digest(migrated_engine, config, OWNER, fake, future.date(), now=future)
    with Session(migrated_engine) as session:
        assert list(session.scalars(select(NotificationItem.lead_id))) == [new_lead]


async def test_retention_defers_only_active_recipient_and_expires_at_configured_boundary(
    migrated_engine: Engine, config: AppConfig
) -> None:
    from tutor_lead_monitor.db.locks import source_lock

    seed(migrated_engine, config)
    await send_digest(migrated_engine, config, OWNER, FakeNotifier(), NOW.date(), now=NOW)
    with Session(migrated_engine) as session, session.begin():
        envelope = session.scalars(select(Notification)).one()
        envelope.created_at = NOW
    retention = config.business.retention.model_copy(update={"notifications_days": 2})
    boundary = NOW + timedelta(days=2)
    assert run_retention(migrated_engine, retention, now=boundary).notifications == 0
    with source_lock(migrated_engine, f"delivery:{OWNER}"):
        assert (
            run_retention(
                migrated_engine, retention, now=boundary + timedelta(seconds=1), dry_run=False
            ).notifications
            == 0
        )
    assert (
        run_retention(
            migrated_engine, retention, now=boundary + timedelta(seconds=1), dry_run=False
        ).notifications
        == 1
    )


async def test_lead_markers_expire_with_lead_but_period_stays_sealed(
    migrated_engine: Engine, config: AppConfig
) -> None:
    lead_id = seed(migrated_engine, config)
    fake = FakeNotifier()
    await send_immediate(migrated_engine, config, OWNER, fake, now=NOW)
    await send_digest(migrated_engine, config, OWNER, fake, NOW.date(), now=NOW)
    future = NOW + timedelta(days=config.business.retention.notifications_days, seconds=1)
    with Session(migrated_engine) as session, session.begin():
        for envelope in session.scalars(select(Notification)):
            envelope.created_at = NOW
        lead = session.get(Lead, lead_id)
        assert lead is not None
        lead.last_seen_at = future  # Still-recent evidence keeps this lead, without feedback.
    run_retention(migrated_engine, config.business.retention, now=future, dry_run=False)
    with Session(migrated_engine) as session:
        assert session.get(Lead, lead_id) is not None
        assert session.scalar(select(func.count()).select_from(NotificationMarker)) == 3
    after_lead_expiry = future + timedelta(days=config.business.retention.leads_days, seconds=1)
    result = run_retention(
        migrated_engine, config.business.retention, now=after_lead_expiry, dry_run=False
    )
    assert result.leads == 1
    with Session(migrated_engine) as session:
        assert session.get(Lead, lead_id) is None
        assert session.scalars(select(NotificationMarker)).one().kind == "digest_period"
    seed(migrated_engine, config, when=after_lead_expiry)
    before = len(fake.calls)
    await send_digest(migrated_engine, config, OWNER, fake, NOW.date(), now=after_lead_expiry)
    assert len(fake.calls) == before
    await send_digest(
        migrated_engine, config, OWNER, fake, after_lead_expiry.date(), now=after_lead_expiry
    )
    assert len(fake.calls) == before + 1

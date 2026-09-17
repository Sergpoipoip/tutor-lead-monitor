import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from tutor_lead_monitor.application.failed import inspect_failed, reset_failed
from tutor_lead_monitor.application.process import process_pending
from tutor_lead_monitor.cli import main
from tutor_lead_monitor.collectors.fixture import FIXTURE_TIME
from tutor_lead_monitor.config import AppConfig, load_config
from tutor_lead_monitor.db.models import Lead, LeadOccurrence, RawItem
from tutor_lead_monitor.db.repositories import sync_source

pytestmark = pytest.mark.integration
NOW = FIXTURE_TIME + timedelta(hours=1)
SIMPLE = "Ищу репетитора по литературе"
REVIEW = "Ищу репетитора по русскому и литературе"
RICH = (
    "Ищу репетитора по литературе, 10 класс, ЕГЭ и ВСОШ, онлайн, "
    "город Москва, до 2000 руб за час. Срочно! Пишите в личку."
)


@pytest.fixture
def config() -> AppConfig:
    return load_config(Path("config"))


def insert_raw(
    engine: Engine,
    config: AppConfig,
    text: str,
    *,
    url: str = "https://example.invalid/request",
    status: str = "pending",
    record_id: UUID | None = None,
    days_old: int = 0,
) -> UUID:
    with Session(engine) as session, session.begin():
        source_id = sync_source(session, config.registry.sources[0])
        raw = RawItem(
            id=record_id or uuid4(),
            source_id=source_id,
            external_id=str(uuid4()),
            text=text,
            url=url,
            collected_at=FIXTURE_TIME,
            published_at=FIXTURE_TIME - timedelta(days=days_old),
            processing_status=status,
            metadata_={},
        )
        session.add(raw)
        session.flush()
        return raw.id


def test_richer_duplicate_promotes_all_fields_without_score_regression(
    migrated_engine: Engine, config: AppConfig
) -> None:
    first = insert_raw(migrated_engine, config, REVIEW)
    old_config = config.model_copy(deep=True)
    old_config.processing.version = "historical-classifier"
    old_config.scoring.version = "historical-score"
    assert process_pending(migrated_engine, old_config, now=NOW).processed == 1
    with Session(migrated_engine) as session, session.begin():
        lead = session.scalars(select(Lead)).one()
        assert config.scoring.thresholds.review <= lead.score < config.scoring.thresholds.digest
        lead.status = "interested"
        lead_id = lead.id
        old_raw = session.get(RawItem, first)
        assert old_raw is not None
        old_evidence = old_raw.metadata_
    richer = insert_raw(migrated_engine, config, RICH)
    assert process_pending(migrated_engine, config, now=NOW).processed == 1
    with Session(migrated_engine) as session:
        lead = session.scalars(select(Lead)).one()
        assert lead.id == lead_id and lead.status == "interested"
        assert lead.canonical_raw_item_id == richer
        assert lead.score == 100 >= config.scoring.thresholds.immediate
        assert (lead.intent, lead.subject, lead.grade) == ("seeking_tutor", "literature", 10)
        assert (lead.format, lead.urgency, lead.location_text) == ("online", "high", "Москва")
        assert lead.goals == ["ege", "vsosh"] and lead.contact_available
        assert lead.budget_text == "до 2000 руб за час"
        assert lead.classification_version == config.processing.version
        assert lead.scoring_version == config.scoring.version
        raw = session.get(RawItem, richer)
        assert raw is not None
        evidence = raw.metadata_["_tlm_processing"]
        assert isinstance(evidence, dict)
        scored = evidence["score"]
        assert isinstance(scored, dict)
        assert lead.score_reasons == scored["reasons"]
        previous = session.get(RawItem, first)
        assert previous is not None and previous.metadata_ == old_evidence
        snapshot = (lead.score, lead.score_reasons, lead.scoring_version)
    poorer = insert_raw(migrated_engine, config, SIMPLE)
    assert process_pending(migrated_engine, config, now=NOW).processed == 1
    # Explicit reprocessing must also keep occurrences and canonical choice idempotent.
    with Session(migrated_engine) as session, session.begin():
        raw = session.get(RawItem, richer)
        assert raw is not None
        raw.processing_status = "pending"
    assert process_pending(migrated_engine, config, now=NOW).processed == 1
    with Session(migrated_engine) as session:
        lead = session.scalars(select(Lead)).one()
        assert lead.canonical_raw_item_id == richer
        assert (lead.score, lead.score_reasons, lead.scoring_version) == snapshot
        assert set(session.scalars(select(LeadOccurrence.raw_item_id))) == {first, richer, poorer}


def test_equal_score_more_complete_extraction_is_promoted(
    migrated_engine: Engine, config: AppConfig
) -> None:
    insert_raw(migrated_engine, config, SIMPLE)
    process_pending(migrated_engine, config, now=NOW)
    richer = insert_raw(migrated_engine, config, SIMPLE + ", город Москва. Срочно!")
    process_pending(migrated_engine, config, now=NOW)
    with Session(migrated_engine) as session:
        lead = session.scalars(select(Lead)).one()
        assert lead.canonical_raw_item_id == richer and lead.score == 61
        assert lead.location_text == "Москва" and lead.urgency == "high"


def test_more_fields_do_not_override_a_higher_score(
    migrated_engine: Engine, config: AppConfig
) -> None:
    first = insert_raw(migrated_engine, config, SIMPLE)
    process_pending(migrated_engine, config, now=NOW)
    insert_raw(migrated_engine, config, REVIEW + ", город Москва. Срочно!")
    process_pending(migrated_engine, config, now=NOW)
    with Session(migrated_engine) as session:
        lead = session.scalars(select(Lead)).one()
        assert lead.canonical_raw_item_id == first and lead.score == 61
        assert session.scalar(select(func.count()).select_from(LeadOccurrence)) == 2


def test_promotion_failure_rolls_back_and_manual_retry_is_safe(
    migrated_engine: Engine, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = insert_raw(migrated_engine, config, REVIEW)
    process_pending(migrated_engine, config, now=NOW)
    richer = insert_raw(migrated_engine, config, RICH)

    def fail_after_promotion(session: Session, *args: object) -> None:
        session.flush()
        raise RuntimeError("synthetic-private-error")

    with monkeypatch.context() as patch:
        patch.setattr(
            "tutor_lead_monitor.application.process.recover_rejected", fail_after_promotion
        )
        result = process_pending(migrated_engine, config, now=NOW)
    assert result.failed == 1
    with Session(migrated_engine) as session:
        lead = session.scalars(select(Lead)).one()
        assert lead.canonical_raw_item_id == first and lead.score == 46
        raw = session.get(RawItem, richer)
        assert raw is not None and raw.processing_error == "RuntimeError"
        assert "_tlm_processing" not in raw.metadata_
        assert session.scalar(select(func.count()).select_from(LeadOccurrence)) == 1
    assert reset_failed(migrated_engine, record_id=richer) == (richer,)
    assert process_pending(migrated_engine, config, now=NOW).processed == 1
    with Session(migrated_engine) as session:
        lead = session.scalars(select(Lead)).one()
        assert lead.canonical_raw_item_id == richer and lead.score == 100
        assert session.scalar(select(func.count()).select_from(LeadOccurrence)) == 2


@pytest.mark.parametrize("reverse", [False, True])
def test_canonical_ties_are_independent_of_arrival_order(
    migrated_engine: Engine, config: AppConfig, reverse: bool
) -> None:
    ids = [UUID(int=1), UUID(int=2)]
    for record_id in reversed(ids) if reverse else ids:
        insert_raw(migrated_engine, config, SIMPLE, record_id=record_id)
        process_pending(migrated_engine, config, now=NOW)
    with Session(migrated_engine) as session:
        lead = session.scalars(select(Lead)).one()
        assert lead.canonical_raw_item_id == ids[0]
        assert session.scalar(select(func.count()).select_from(LeadOccurrence)) == 2


def test_concurrent_duplicate_promotion_keeps_best_occurrence(
    migrated_engine: Engine, config: AppConfig
) -> None:
    insert_raw(migrated_engine, config, REVIEW)
    richer = insert_raw(migrated_engine, config, RICH)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(process_pending, migrated_engine, config, now=NOW) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert sum(r.failed for r in results) == 0 and sum(r.processed for r in results) == 2
    with Session(migrated_engine) as session:
        lead = session.scalars(select(Lead)).one()
        assert lead.canonical_raw_item_id == richer and lead.score == 100
        assert session.scalar(select(func.count()).select_from(LeadOccurrence)) == 2


@pytest.mark.parametrize("identity", ["url", "fingerprint", "outside_window", "contact", "fuzzy"])
def test_earlier_rejected_evidence_recovery(
    migrated_engine: Engine, config: AppConfig, identity: str
) -> None:
    # A stale original was rejected; the same request is later renewed.
    text = SIMPLE + " для дочери, помощь по школьной программе"
    old_text = text + " +7 (000) 000-00-01" if identity == "contact" else text
    earlier = insert_raw(migrated_engine, config, old_text, days_old=31)
    assert process_pending(migrated_engine, config, now=NOW).rejected == 1
    with Session(migrated_engine) as session:
        old = session.get(RawItem, earlier)
        assert old is not None
        evidence = old.metadata_
    # 29 days between publications is within the matching window, but neither is fresh.
    later_text = text + " +7 (000) 000-00-02" if identity == "contact" else text
    if identity == "fuzzy":
        later_text += " Пожалуйста."
    later = insert_raw(
        migrated_engine,
        config,
        later_text,
        url="https://example.invalid/request"
        if identity == "url"
        else "https://example.invalid/new",
        days_old=0 if identity == "outside_window" else 2,
    )
    assert process_pending(migrated_engine, config, now=NOW).processed == 1
    assert process_pending(migrated_engine, config, now=NOW).processed == 0
    recovered = identity in {"url", "fingerprint"}
    with Session(migrated_engine) as session:
        lead = session.scalars(select(Lead)).one()
        assert lead.canonical_raw_item_id == later
        occurrences = set(session.scalars(select(LeadOccurrence.raw_item_id)))
        assert occurrences == ({earlier, later} if recovered else {later})
        old = session.get(RawItem, earlier)
        assert old is not None and old.metadata_ == evidence
        assert old.processing_status == ("processed" if recovered else "rejected")


def test_failed_cli_inspection_selection_and_retry(
    migrated_engine: Engine,
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_engine.url.render_as_string(hide_password=False))
    failed = insert_raw(migrated_engine, config, SIMPLE, status="failed")
    second = insert_raw(migrated_engine, config, "synthetic-private-text", status="failed")
    pending = insert_raw(migrated_engine, config, SIMPLE)
    with Session(migrated_engine) as session, session.begin():
        raw = session.get(RawItem, failed)
        assert raw is not None
        raw.processing_error = "synthetic-private-error +7 (000) 000-00-01"
        raw.metadata_ = {"private": "synthetic-private-metadata"}
    capsys.readouterr()
    assert main(["failed", "--limit", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total"] == 2 and len(report["record_ids"]) == 1
    assert set(report) == {"total", "record_ids"}
    for args in (
        ["retry-failed"],
        ["retry-failed", "--limit", "0"],
        ["retry-failed", "--limit", "1001"],
        ["retry-failed", "--limit", "1", "--dry-run"],
        ["retry-failed", "--record-id", str(failed), "--limit", "1"],
    ):
        assert main(args) == 1
        assert inspect_failed(migrated_engine).total == 2
    assert main(["retry-failed", "--record-id", str(pending)]) == 0
    assert inspect_failed(migrated_engine).total == 2
    assert main(["retry-failed", "--record-id", str(failed)]) == 0
    assert main(["retry-failed", "--record-id", str(failed)]) == 0
    assert inspect_failed(migrated_engine).record_ids == (second,)
    with Session(migrated_engine) as session:
        raw = session.get(RawItem, failed)
        assert raw is not None and raw.processing_status == "pending"
        assert (
            raw.processing_error is None
            and raw.metadata_["private"] == "synthetic-private-metadata"
        )
    assert main(["retry-failed", "--limit", "1"]) == 0
    output = capsys.readouterr()
    assert "synthetic-private" not in output.out + output.err
    assert "+7" not in output.out + output.err and SIMPLE not in output.out + output.err
    assert inspect_failed(migrated_engine).total == 0
    assert main(["process", "--as-of", NOW.isoformat()]) == 0
    assert inspect_failed(migrated_engine).total == 0


def test_reset_failed_skips_locks_and_keeps_batch_bounded(
    migrated_engine: Engine, config: AppConfig
) -> None:
    first = insert_raw(migrated_engine, config, SIMPLE, status="failed")
    second = insert_raw(migrated_engine, config, SIMPLE, status="failed")
    third = insert_raw(migrated_engine, config, SIMPLE, status="failed")
    with Session(migrated_engine) as session, session.begin():
        session.scalar(select(RawItem).where(RawItem.id == first).with_for_update())
        assert reset_failed(migrated_engine, record_id=first) == ()
        reset = reset_failed(migrated_engine, limit=1)
        assert len(reset) == 1 and reset[0] in {second, third}
        assert inspect_failed(migrated_engine).total == 2
    assert reset_failed(migrated_engine, record_id=first) == (first,)

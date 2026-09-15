from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from hashlib import sha256
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import build_engine, initialize_database
from app.models import (
    ResurfacingCloseReason,
    ResurfacingEvent,
    ResurfacingOutcome,
    ResurfacingState,
    SavedItem,
    SavedItemStatus,
    ScheduleKind,
    User,
)
from app.repositories import EventRepository, SavedItemRepository, UserRepository
from app.services import (
    ResurfacingEngine,
    ResurfacingErrorCode,
    ResurfacingRejected,
    SelectionStatus,
)


NOW = datetime(2026, 9, 14, 16, 30, tzinfo=timezone.utc)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


@pytest.fixture
def session(tmp_path) -> Session:
    engine = build_engine(f"sqlite:///{tmp_path / 'resurfacing.db'}")
    initialize_database(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as database:
        yield database
    engine.dispose()


def create_user(
    session: Session,
    *,
    suffix: str = "one",
    timezone_name: str = "Asia/Shanghai",
) -> User:
    return UserRepository().create(
        session,
        access_code_hash=f"pilot-code-hash-{suffix}",
        timezone_name=timezone_name,
    )


def create_item(
    session: Session,
    *,
    user: User,
    title: str,
    due_on: date,
    saved_at: datetime,
) -> SavedItem:
    url_hash = sha256(f"{user.id}:{title}".encode()).hexdigest()
    result = SavedItemRepository().create(
        session,
        user_id=user.id,
        source_url=f"https://example.com/{url_hash[:10]}",
        source_url_hash=url_hash,
        title=title,
        source_domain="example.com",
        next_due_on=due_on,
        saved_at=saved_at,
        schedule_timezone=user.timezone,
    )
    assert result.created is True
    return result.item


def test_selects_earliest_due_then_oldest_saved_and_snapshots_count(session: Session) -> None:
    user = create_user(session)
    newer_earliest = create_item(
        session,
        user=user,
        title="Earliest but newer",
        due_on=date(2026, 9, 13),
        saved_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
    )
    create_item(
        session,
        user=user,
        title="Later due but older",
        due_on=date(2026, 9, 14),
        saved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    older_tied = create_item(
        session,
        user=user,
        title="Earliest and oldest",
        due_on=date(2026, 9, 13),
        saved_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    future = create_item(
        session,
        user=user,
        title="Future",
        due_on=date(2026, 9, 16),
        saved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    dismissed = create_item(
        session,
        user=user,
        title="Dismissed",
        due_on=date(2026, 9, 1),
        saved_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    dismissed.status = SavedItemStatus.DISMISSED
    recall = create_item(
        session,
        user=user,
        title="Future Recall implementation",
        due_on=date(2026, 9, 1),
        saved_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    recall.schedule_kind = ScheduleKind.RECALL
    unscheduled = create_item(
        session,
        user=user,
        title="Unscheduled",
        due_on=date(2026, 9, 1),
        saved_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    unscheduled.schedule_kind = ScheduleKind.NONE
    unscheduled.next_due_on = None
    session.flush()

    result = ResurfacingEngine(clock=FrozenClock()).select_for_today(
        session,
        user_id=user.id,
    )

    assert result.status is SelectionStatus.RESERVED
    assert result.local_today == date(2026, 9, 15)
    assert result.event is not None
    assert result.event.saved_item_id == older_tied.id
    assert result.event.saved_item_id != newer_earliest.id
    assert result.event.scheduled_on_snapshot == date(2026, 9, 13)
    assert result.event.eligible_count_snapshot == 3
    assert result.event.timezone_at_selection == "Asia/Shanghai"
    assert result.event.state is ResurfacingState.PENDING
    assert future.id != result.event.saved_item_id


def test_item_id_is_the_stable_final_tie_breaker(session: Session) -> None:
    user = create_user(session)
    shared_saved_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    later_id = SavedItem(
        id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        user_id=user.id,
        source_url="https://example.com/b",
        source_url_hash="b" * 64,
        title="B",
        source_domain="example.com",
        saved_at=shared_saved_at,
        next_due_on=date(2026, 9, 15),
    )
    earlier_id = SavedItem(
        id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        user_id=user.id,
        source_url="https://example.com/a",
        source_url_hash="a" * 64,
        title="A",
        source_domain="example.com",
        saved_at=shared_saved_at,
        next_due_on=date(2026, 9, 15),
    )
    session.add_all((later_id, earlier_id))
    session.flush()

    result = ResurfacingEngine(clock=FrozenClock()).select_for_today(
        session,
        user_id=user.id,
    )

    assert result.event is not None
    assert result.event.saved_item_id == earlier_id.id


def test_same_pending_reservation_is_reused_without_duplicate_row(session: Session) -> None:
    user = create_user(session)
    create_item(
        session,
        user=user,
        title="Due",
        due_on=date(2026, 9, 15),
        saved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    engine = ResurfacingEngine(clock=FrozenClock())

    first = engine.select_for_today(session, user_id=user.id)
    second = engine.select_for_today(session, user_id=user.id)

    assert first.status is SelectionStatus.RESERVED
    assert second.status is SelectionStatus.REUSED_PENDING
    assert second.event is not None and first.event is not None
    assert second.event.id == first.event.id
    assert session.scalar(select(func.count()).select_from(ResurfacingEvent)) == 1


@pytest.mark.parametrize(
    ("state", "expected_status"),
    [
        (ResurfacingState.SHOWN, SelectionStatus.DAILY_SLOT_USED),
        (ResurfacingState.RESOLVED, SelectionStatus.DAILY_SLOT_USED),
        (ResurfacingState.CLOSED, SelectionStatus.DAILY_SLOT_USED),
    ],
)
def test_finished_or_mounted_today_slot_never_returns_another_card(
    session: Session,
    state: ResurfacingState,
    expected_status: SelectionStatus,
) -> None:
    user = create_user(session)
    create_item(
        session,
        user=user,
        title="Due",
        due_on=date(2026, 9, 15),
        saved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    engine = ResurfacingEngine(clock=FrozenClock())
    first = engine.select_for_today(session, user_id=user.id)
    assert first.event is not None
    event = first.event
    event.state = state
    if state in (ResurfacingState.SHOWN, ResurfacingState.RESOLVED):
        event.shown_at = NOW
    if state is ResurfacingState.RESOLVED:
        event.outcome = ResurfacingOutcome.OPENED
        event.outcome_at = NOW
        event.closed_at = NOW
    if state is ResurfacingState.CLOSED:
        event.closed_at = NOW
        event.close_reason = ResurfacingCloseReason.PAUSED
    session.flush()

    repeated = engine.select_for_today(session, user_id=user.id)

    assert repeated.status is expected_status
    assert repeated.event is None
    assert session.scalar(select(func.count()).select_from(ResurfacingEvent)) == 1


def test_prior_pending_and_shown_events_are_closed_before_pause_check(session: Session) -> None:
    user = create_user(session)
    first_item = create_item(
        session,
        user=user,
        title="Never mounted",
        due_on=date(2026, 9, 10),
        saved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    second_item = create_item(
        session,
        user=user,
        title="Mounted without action",
        due_on=date(2026, 9, 11),
        saved_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    repository = EventRepository()
    pending = repository.create_resurfacing_event(
        session,
        user_id=user.id,
        item_id=first_item.id,
        scheduled_on_snapshot=first_item.next_due_on,
        local_date_slot=date(2026, 9, 13),
        timezone_at_selection=user.timezone,
        eligible_count_snapshot=2,
    )
    shown = repository.create_resurfacing_event(
        session,
        user_id=user.id,
        item_id=second_item.id,
        scheduled_on_snapshot=second_item.next_due_on,
        local_date_slot=date(2026, 9, 14),
        timezone_at_selection=user.timezone,
        eligible_count_snapshot=2,
    )
    shown.state = ResurfacingState.SHOWN
    shown.shown_at = datetime(2026, 9, 14, 4, tzinfo=timezone.utc)
    user.resurfacing_paused_at = datetime(2026, 9, 14, 5, tzinfo=timezone.utc)
    session.flush()

    result = ResurfacingEngine(clock=FrozenClock()).select_for_today(
        session,
        user_id=user.id,
    )

    assert result.status is SelectionStatus.PAUSED
    assert result.event is None
    assert pending.state is ResurfacingState.CLOSED
    assert pending.close_reason is ResurfacingCloseReason.STALE_PENDING
    assert pending.closed_at == NOW
    assert shown.state is ResurfacingState.CLOSED
    assert shown.close_reason is ResurfacingCloseReason.NO_ACTION
    assert shown.closed_at == NOW


def test_impression_watermark_blocks_replacement_after_selected_item_deletion(
    session: Session,
) -> None:
    user = create_user(session)
    selected = create_item(
        session,
        user=user,
        title="Selected then deleted",
        due_on=date(2026, 9, 14),
        saved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    create_item(
        session,
        user=user,
        title="Must not replace it",
        due_on=date(2026, 9, 14),
        saved_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    first = ResurfacingEngine(clock=FrozenClock()).select_for_today(
        session,
        user_id=user.id,
    )
    assert first.event is not None and first.event.saved_item_id == selected.id
    user.last_new_impression_local_date = date(2026, 9, 15)
    SavedItemRepository().hard_delete(session, user_id=user.id, item_id=selected.id)
    session.flush()

    replacement = ResurfacingEngine(clock=FrozenClock()).select_for_today(
        session,
        user_id=user.id,
    )

    assert replacement.status is SelectionStatus.WATERMARK_USED
    assert replacement.event is None
    assert session.scalar(select(func.count()).select_from(ResurfacingEvent)) == 0


def test_eligibility_uses_each_users_local_date(session: Session) -> None:
    shanghai = create_user(session, suffix="shanghai", timezone_name="Asia/Shanghai")
    los_angeles = create_user(
        session,
        suffix="los-angeles",
        timezone_name="America/Los_Angeles",
    )
    for user in (shanghai, los_angeles):
        create_item(
            session,
            user=user,
            title=f"Due on local September 15 ({user.timezone})",
            due_on=date(2026, 9, 15),
            saved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

    shanghai_result = ResurfacingEngine(clock=FrozenClock()).select_for_today(
        session,
        user_id=shanghai.id,
    )
    los_angeles_result = ResurfacingEngine(clock=FrozenClock()).select_for_today(
        session,
        user_id=los_angeles.id,
    )

    assert shanghai_result.local_today == date(2026, 9, 15)
    assert shanghai_result.status is SelectionStatus.RESERVED
    assert los_angeles_result.local_today == date(2026, 9, 14)
    assert los_angeles_result.status is SelectionStatus.NO_ELIGIBLE_ITEM


def test_missing_user_and_invalid_timezone_fail_closed(session: Session) -> None:
    engine = ResurfacingEngine(clock=FrozenClock())
    with pytest.raises(ResurfacingRejected) as missing:
        engine.select_for_today(session, user_id="missing")
    assert missing.value.code is ResurfacingErrorCode.USER_NOT_FOUND

    user = create_user(session)
    user.timezone = "Not/A_Timezone"
    session.flush()
    with pytest.raises(ResurfacingRejected) as invalid_timezone:
        engine.select_for_today(session, user_id=user.id)
    assert invalid_timezone.value.code is ResurfacingErrorCode.INVALID_TIMEZONE


def test_concurrent_selection_commits_one_daily_reservation(tmp_path) -> None:
    database_path = tmp_path / "concurrent-resurfacing.db"
    engine = build_engine(f"sqlite:///{database_path}")
    initialize_database(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as setup:
        user = create_user(setup)
        create_item(
            setup,
            user=user,
            title="One due item",
            due_on=date(2026, 9, 15),
            saved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        setup.commit()
        user_id = user.id

    barrier = Barrier(2)

    def reserve() -> str:
        with factory() as database:
            barrier.wait(timeout=5)
            result = ResurfacingEngine(clock=FrozenClock()).select_for_today(
                database,
                user_id=user_id,
            )
            database.commit()
            assert result.event is not None
            return result.event.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: reserve(), range(2)))

    with factory() as verify:
        rows = list(verify.scalars(select(ResurfacingEvent)))
    engine.dispose()

    assert len(set(ids)) == 1
    assert len(rows) == 1
    assert rows[0].state is ResurfacingState.PENDING

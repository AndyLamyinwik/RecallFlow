from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import re
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import build_engine, initialize_database
from app.main import create_app
from app.models import (
    ActionActor,
    ActionEvent,
    ActionEventName,
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
from app.security import DismissUndoClaims, DismissUndoTokenManager
from app.services import (
    ActionApplyStatus,
    ActionErrorCode,
    ActionRejected,
    CaptureService,
    LaterChoice,
    OpenActionStatus,
    ReachabilityErrorCode,
    ResurfacingActionService,
    ResurfacingEngine,
    SafeSourceReachabilityChecker,
    SourceReachability,
    SourceReachabilityError,
)
from app.services.extraction import ArticleExtraction


NOW = datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc)
LOCAL_TODAY = date(2026, 9, 15)
ACCESS_CODE = "resurfacing-actions-pilot-code-123"


class FrozenClock:
    def now(self) -> datetime:
        return NOW


class NextDayClock:
    def now(self) -> datetime:
        return NOW + timedelta(days=1)


class MutableClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class FakeChecker:
    def __init__(
        self,
        result: SourceReachability | None = None,
        error: SourceReachabilityError | None = None,
    ) -> None:
        self.result = result or SourceReachability(
            final_url="https://publisher.example/final",
            method="head",
            status_class="2xx",
        )
        self.error = error
        self.calls: list[str] = []

    def check(self, source_url: str) -> SourceReachability:
        self.calls.append(source_url)
        if self.error is not None:
            raise self.error
        return self.result


class UnusedExtractor:
    def extract(self, normalized_url: str) -> ArticleExtraction:
        raise AssertionError("Action tests do not capture a new source")


def _create_user(database: Session, *, suffix: str = "owner") -> User:
    return UserRepository().create(
        database,
        access_code_hash=f"test-access-hash-{suffix}",
        timezone_name="Asia/Shanghai",
    )


def _create_shown_card(
    database: Session,
    *,
    user: User,
    suffix: str = "one",
) -> tuple[SavedItem, ResurfacingEvent]:
    source_url = f"https://example.com/private/{suffix}?token=never-log"
    item = SavedItemRepository().create(
        database,
        user_id=user.id,
        source_url=source_url,
        source_url_hash=sha256(f"{user.id}:{suffix}".encode()).hexdigest(),
        title=f"Due article {suffix}",
        source_domain="example.com",
        source_text="private source snapshot",
        next_due_on=date(2026, 9, 13),
        schedule_timezone=user.timezone,
    ).item
    event = EventRepository().create_resurfacing_event(
        database,
        user_id=user.id,
        item_id=item.id,
        scheduled_on_snapshot=item.next_due_on,
        local_date_slot=LOCAL_TODAY,
        timezone_at_selection=user.timezone,
        eligible_count_snapshot=1,
    )
    event.state = ResurfacingState.SHOWN
    event.shown_at = NOW
    user.last_new_impression_local_date = LOCAL_TODAY
    database.flush()
    return item, event


@pytest.fixture
def action_store(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'actions.db'}")
    initialize_database(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as database:
        user = _create_user(database)
        item, event = _create_shown_card(database, user=user)
        database.commit()
        ids = SimpleNamespace(user=user.id, item=item.id, event=event.id)
    yield SimpleNamespace(factory=factory, ids=ids, engine=engine)
    engine.dispose()


def test_later_plus_three_is_atomic_and_identical_retry_replays(action_store) -> None:
    service = ResurfacingActionService(clock=FrozenClock(), checker=FakeChecker())
    with action_store.factory() as database:
        result = service.later(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            session_id="session-one",
            idempotency_key="later_action_12345",
            choice=LaterChoice.PLUS_3,
        )
        database.commit()
    with action_store.factory() as database:
        replay = service.later(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            session_id="session-one",
            idempotency_key="later_action_12345",
            choice=LaterChoice.PLUS_3,
        )
        database.commit()

    assert result.status is ActionApplyStatus.APPLIED
    assert replay.status is ActionApplyStatus.REPLAYED
    assert result.next_due_on == replay.next_due_on == date(2026, 9, 18)
    with action_store.factory() as database:
        item = database.get(SavedItem, action_store.ids.item)
        event = database.get(ResurfacingEvent, action_store.ids.event)
        actions = list(database.scalars(select(ActionEvent)))
        assert item.status is SavedItemStatus.ACTIVE
        assert item.schedule_kind is ScheduleKind.STANDARD
        assert item.next_due_on == date(2026, 9, 18)
        assert item.schedule_timezone == "Asia/Shanghai"
        assert event.state is ResurfacingState.RESOLVED
        assert event.outcome is ResurfacingOutcome.LATER_CONFIRMED
        assert event.outcome_at == event.closed_at == NOW
        assert len(actions) == 1
        assert actions[0].event_name is ActionEventName.LATER_CONFIRMED
        assert actions[0].properties == {
            "previous_due_on": "2026-09-13",
            "new_due_on": "2026-09-18",
            "choice": "plus_3",
            "resulting_schedule_kind": "STANDARD",
        }


def test_later_custom_must_be_strictly_future_and_key_binds_input(action_store) -> None:
    service = ResurfacingActionService(clock=FrozenClock(), checker=FakeChecker())
    for invalid_date in (date(2026, 9, 14), LOCAL_TODAY):
        with action_store.factory() as database:
            with pytest.raises(ActionRejected) as rejected:
                service.later(
                    database,
                    user_id=action_store.ids.user,
                    resurfacing_event_id=action_store.ids.event,
                    session_id="session-one",
                    idempotency_key="custom_later_12345",
                    choice=LaterChoice.CUSTOM,
                    custom_date=invalid_date,
                )
            assert rejected.value.code is ActionErrorCode.INVALID_DATE
            database.rollback()

    with action_store.factory() as database:
        applied = service.later(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            session_id="session-one",
            idempotency_key="custom_later_12345",
            choice=LaterChoice.CUSTOM,
            custom_date=date(2026, 10, 1),
        )
        database.commit()
    assert applied.next_due_on == date(2026, 10, 1)

    with action_store.factory() as database:
        with pytest.raises(ActionRejected) as conflict:
            service.later(
                database,
                user_id=action_store.ids.user,
                resurfacing_event_id=action_store.ids.event,
                session_id="session-one",
                idempotency_key="custom_later_12345",
                choice=LaterChoice.CUSTOM,
                custom_date=date(2026, 10, 2),
            )
        assert conflict.value.code is ActionErrorCode.IDEMPOTENCY_CONFLICT


def test_delayed_plus_choice_retry_replays_original_calendar_date(action_store) -> None:
    with action_store.factory() as database:
        first = ResurfacingActionService(
            clock=FrozenClock(), checker=FakeChecker()
        ).later(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            session_id="session-one",
            idempotency_key="delayed_later_12345",
            choice=LaterChoice.PLUS_3,
        )
        database.commit()
    with action_store.factory() as database:
        next_day_replay = ResurfacingActionService(
            clock=NextDayClock(), checker=FakeChecker()
        ).later(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            session_id="session-one",
            idempotency_key="delayed_later_12345",
            choice=LaterChoice.PLUS_3,
        )
        database.commit()

    assert first.next_due_on == date(2026, 9, 18)
    assert next_day_replay.status is ActionApplyStatus.REPLAYED
    assert next_day_replay.next_due_on == first.next_due_on


def test_concurrent_different_actions_commit_only_one_outcome(tmp_path) -> None:
    database_path = tmp_path / "concurrent-actions.db"
    engine = build_engine(f"sqlite:///{database_path}")
    initialize_database(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as setup:
        user = _create_user(setup)
        item, event = _create_shown_card(setup, user=user, suffix="race")
        setup.commit()
        ids = SimpleNamespace(user=user.id, item=item.id, event=event.id)

    barrier = Barrier(2)

    def act(action_name: str) -> str:
        service = ResurfacingActionService(clock=FrozenClock(), checker=FakeChecker())
        with factory() as database:
            barrier.wait(timeout=5)
            try:
                if action_name == "later":
                    service.later(
                        database,
                        user_id=ids.user,
                        resurfacing_event_id=ids.event,
                        session_id="later-session",
                        idempotency_key="concurrent_later_12345",
                        choice=LaterChoice.PLUS_3,
                    )
                else:
                    service.dismiss(
                        database,
                        user_id=ids.user,
                        resurfacing_event_id=ids.event,
                        session_id="dismiss-session",
                        idempotency_key="concurrent_dismiss_12345",
                    )
                database.commit()
                return "applied"
            except ActionRejected:
                database.rollback()
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(act, ("later", "dismiss")))

    with factory() as verify:
        event = verify.get(ResurfacingEvent, ids.event)
        item = verify.get(SavedItem, ids.item)
        actions = list(verify.scalars(select(ActionEvent)))
        assert event.state is ResurfacingState.RESOLVED
        assert len(actions) == 1
        if event.outcome is ResurfacingOutcome.LATER_CONFIRMED:
            assert item.status is SavedItemStatus.ACTIVE
            assert item.next_due_on == date(2026, 9, 18)
        else:
            assert event.outcome is ResurfacingOutcome.DISMISSED
            assert item.status is SavedItemStatus.DISMISSED
            assert item.next_due_on == date(2026, 9, 13)
    engine.dispose()

    assert sorted(outcomes) == ["applied", "rejected"]


@pytest.mark.parametrize("action_name", ["later", "dismiss"])
@pytest.mark.parametrize("iteration", range(5))
def test_concurrent_identical_action_replays_the_single_committed_result(
    tmp_path,
    action_name: str,
    iteration: int,
) -> None:
    database_path = tmp_path / f"concurrent-identical-{action_name}-{iteration}.db"
    engine = build_engine(f"sqlite:///{database_path}")
    initialize_database(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as setup:
        user = _create_user(setup)
        item, event = _create_shown_card(
            setup,
            user=user,
            suffix=f"identical-{action_name}",
        )
        setup.commit()
        ids = SimpleNamespace(user=user.id, item=item.id, event=event.id)

    barrier = Barrier(2)

    def act(_: int) -> str:
        service = ResurfacingActionService(clock=FrozenClock(), checker=FakeChecker())
        with factory() as database:
            barrier.wait(timeout=5)
            if action_name == "later":
                result = service.later(
                    database,
                    user_id=ids.user,
                    resurfacing_event_id=ids.event,
                    session_id="identical-session",
                    idempotency_key="identical_action_12345",
                    choice=LaterChoice.PLUS_3,
                )
            else:
                result = service.dismiss(
                    database,
                    user_id=ids.user,
                    resurfacing_event_id=ids.event,
                    session_id="identical-session",
                    idempotency_key="identical_action_12345",
                )
            database.commit()
            return result.status.value

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(act, range(2)))

    with factory() as verify:
        actions = list(verify.scalars(select(ActionEvent)))
        persisted_event = verify.get(ResurfacingEvent, ids.event)
        assert len(actions) == 1
        assert persisted_event.state is ResurfacingState.RESOLVED
    engine.dispose()

    assert sorted(outcomes) == ["applied", "replayed"]


def test_dismiss_preserves_schedule_is_not_delete_and_replays(action_store) -> None:
    service = ResurfacingActionService(clock=FrozenClock(), checker=FakeChecker())
    with action_store.factory() as database:
        applied = service.dismiss(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            session_id="session-one",
            idempotency_key="dismiss_action_12345",
        )
        database.commit()
    with action_store.factory() as database:
        replay = service.dismiss(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            session_id="session-one",
            idempotency_key="dismiss_action_12345",
        )
        database.commit()

    assert applied.status is ActionApplyStatus.APPLIED
    assert replay.status is ActionApplyStatus.REPLAYED
    with action_store.factory() as database:
        item = database.get(SavedItem, action_store.ids.item)
        event = database.get(ResurfacingEvent, action_store.ids.event)
        assert item is not None
        assert item.status is SavedItemStatus.DISMISSED
        assert item.schedule_kind is ScheduleKind.STANDARD
        assert item.next_due_on == date(2026, 9, 13)
        assert event.state is ResurfacingState.RESOLVED
        assert event.outcome is ResurfacingOutcome.DISMISSED
        action = database.scalar(select(ActionEvent))
        assert action.event_name is ActionEventName.DISMISSED
        assert action.properties == {
            "previous_due_on": "2026-09-13",
            "previous_schedule_kind": "STANDARD",
            "undo_expires_at": "2026-09-15T03:00:30+00:00",
        }


def test_dismiss_undo_token_is_signed_scoped_and_expires_at_exact_boundary() -> None:
    manager = DismissUndoTokenManager("undo-token-test-secret-long-enough")
    claims = DismissUndoClaims(
        user_id="user-one",
        event_id="event-one",
        item_id="item-one",
        dismiss_action_event_id="dismiss-one",
        original_session_id="session-one",
        local_date_slot=LOCAL_TODAY,
        expires_at=NOW + timedelta(seconds=30),
    )
    token = manager.issue(claims)

    assert manager.read(token, now=NOW) == claims
    assert manager.read(token + "tampered", now=NOW) is None
    assert manager.read(token, now=claims.expires_at) is None


def test_dismiss_undo_restores_same_card_without_changing_schedule(action_store) -> None:
    service = ResurfacingActionService(clock=FrozenClock(), checker=FakeChecker())
    with action_store.factory() as database:
        dismissed = service.dismiss(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            session_id="same-browser-session",
            idempotency_key="undo_source_dismiss_12345",
        )
        database.commit()

    with action_store.factory() as database:
        assert service.dismiss_undo_available(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            item_id=action_store.ids.item,
            dismiss_action_event_id=dismissed.dismiss_action_event_id,
            original_session_id=dismissed.original_session_id,
            dismissal_local_date=dismissed.local_date_slot,
            undo_expires_at=dismissed.undo_expires_at,
        )
        undone = service.undo_dismiss(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            item_id=action_store.ids.item,
            dismiss_action_event_id=dismissed.dismiss_action_event_id,
            original_session_id=dismissed.original_session_id,
            dismissal_local_date=dismissed.local_date_slot,
            undo_expires_at=dismissed.undo_expires_at,
            session_id="same-browser-session",
            idempotency_key="undo_dismiss_action_12345",
        )
        database.commit()

    with action_store.factory() as database:
        replay = service.undo_dismiss(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            item_id=action_store.ids.item,
            dismiss_action_event_id=dismissed.dismiss_action_event_id,
            original_session_id=dismissed.original_session_id,
            dismissal_local_date=dismissed.local_date_slot,
            undo_expires_at=dismissed.undo_expires_at,
            session_id="same-browser-session",
            idempotency_key="undo_dismiss_action_12345",
        )
        database.commit()
        item = database.get(SavedItem, action_store.ids.item)
        event = database.get(ResurfacingEvent, action_store.ids.event)
        undo_events = list(
            database.scalars(
                select(ActionEvent).where(
                    ActionEvent.event_name == ActionEventName.DISMISS_UNDONE
                )
            )
        )

    assert undone.status is ActionApplyStatus.APPLIED
    assert undone.event_state is ResurfacingState.SHOWN
    assert replay.status is ActionApplyStatus.REPLAYED
    assert item is not None
    assert item.status is SavedItemStatus.ACTIVE
    assert item.schedule_kind is ScheduleKind.STANDARD
    assert item.next_due_on == date(2026, 9, 13)
    assert item.schedule_timezone == "Asia/Shanghai"
    assert event is not None
    assert event.state is ResurfacingState.SHOWN
    assert event.shown_at == NOW
    assert event.outcome is None
    assert event.outcome_at is None
    assert event.closed_at is None
    assert event.close_reason is None
    assert len(undo_events) == 1
    assert undo_events[0].properties == {
        "dismiss_action_event_id": dismissed.dismiss_action_event_id
    }


def test_dismiss_undo_from_other_session_closes_card_but_reactivates_item(
    action_store,
) -> None:
    service = ResurfacingActionService(clock=FrozenClock(), checker=FakeChecker())
    with action_store.factory() as database:
        dismissed = service.dismiss(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            session_id="original-browser",
            idempotency_key="cross_session_dismiss_12345",
        )
        database.commit()
    with action_store.factory() as database:
        result = service.undo_dismiss(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            item_id=action_store.ids.item,
            dismiss_action_event_id=dismissed.dismiss_action_event_id,
            original_session_id=dismissed.original_session_id,
            dismissal_local_date=dismissed.local_date_slot,
            undo_expires_at=dismissed.undo_expires_at,
            session_id="different-browser",
            idempotency_key="cross_session_undo_12345",
        )
        database.commit()
        item = database.get(SavedItem, action_store.ids.item)
        event = database.get(ResurfacingEvent, action_store.ids.event)

    assert result.event_state is ResurfacingState.CLOSED
    assert item is not None and item.status is SavedItemStatus.ACTIVE
    assert item.next_due_on == date(2026, 9, 13)
    assert event is not None
    assert event.state is ResurfacingState.CLOSED
    assert event.outcome is None
    assert event.close_reason is ResurfacingCloseReason.DISMISS_UNDONE


def test_expired_undo_and_old_undo_after_redismiss_cannot_mutate_state(
    action_store,
) -> None:
    service = ResurfacingActionService(clock=FrozenClock(), checker=FakeChecker())
    with action_store.factory() as database:
        first = service.dismiss(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            session_id="browser-session",
            idempotency_key="aba_first_dismiss_12345",
        )
        database.commit()

    expired_service = ResurfacingActionService(clock=NextDayClock(), checker=FakeChecker())
    with action_store.factory() as database:
        with pytest.raises(ActionRejected) as expired:
            expired_service.undo_dismiss(
                database,
                user_id=action_store.ids.user,
                resurfacing_event_id=action_store.ids.event,
                item_id=action_store.ids.item,
                dismiss_action_event_id=first.dismiss_action_event_id,
                original_session_id=first.original_session_id,
                dismissal_local_date=first.local_date_slot,
                undo_expires_at=first.undo_expires_at,
                session_id="browser-session",
                idempotency_key="expired_undo_action_12345",
            )
        assert expired.value.code is ActionErrorCode.UNDO_EXPIRED
        database.rollback()

    with action_store.factory() as database:
        service.undo_dismiss(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            item_id=action_store.ids.item,
            dismiss_action_event_id=first.dismiss_action_event_id,
            original_session_id=first.original_session_id,
            dismissal_local_date=first.local_date_slot,
            undo_expires_at=first.undo_expires_at,
            session_id="browser-session",
            idempotency_key="valid_first_undo_12345",
        )
        database.commit()
    with action_store.factory() as database:
        second = service.dismiss(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            session_id="browser-session",
            idempotency_key="aba_second_dismiss_12345",
        )
        database.commit()
    with action_store.factory() as database:
        with pytest.raises(ActionRejected) as stale:
            service.undo_dismiss(
                database,
                user_id=action_store.ids.user,
                resurfacing_event_id=action_store.ids.event,
                item_id=action_store.ids.item,
                dismiss_action_event_id=first.dismiss_action_event_id,
                original_session_id=first.original_session_id,
                dismissal_local_date=first.local_date_slot,
                undo_expires_at=first.undo_expires_at,
                session_id="browser-session",
                idempotency_key="valid_first_undo_12345",
            )
        assert stale.value.code is ActionErrorCode.INVALID_UNDO
        database.rollback()
        item = database.get(SavedItem, action_store.ids.item)
        event = database.get(ResurfacingEvent, action_store.ids.event)
        assert item is not None and item.status is SavedItemStatus.DISMISSED
        assert event is not None and event.outcome is ResurfacingOutcome.DISMISSED
        assert second.dismiss_action_event_id != first.dismiss_action_event_id


def test_pause_and_resume_preserve_dates_close_card_and_are_idempotent(
    action_store,
) -> None:
    clock = MutableClock()
    service = ResurfacingActionService(clock=clock, checker=FakeChecker())
    with action_store.factory() as database:
        pause = service.pause(
            database,
            user_id=action_store.ids.user,
            session_id="pause-session",
            idempotency_key="pause_action_12345",
        )
        database.commit()
    with action_store.factory() as database:
        pause_replay = service.pause(
            database,
            user_id=action_store.ids.user,
            session_id="pause-session",
            idempotency_key="pause_action_12345",
        )
        pause_noop = service.pause(
            database,
            user_id=action_store.ids.user,
            session_id="pause-session",
            idempotency_key="pause_action_other_12345",
        )
        database.commit()
        user = database.get(User, action_store.ids.user)
        item = database.get(SavedItem, action_store.ids.item)
        event = database.get(ResurfacingEvent, action_store.ids.event)

    assert pause.status is ActionApplyStatus.APPLIED
    assert pause.had_active_card is True
    assert pause_replay.status is ActionApplyStatus.REPLAYED
    assert pause_noop.status is ActionApplyStatus.UNCHANGED
    assert user is not None and user.resurfacing_paused_at == NOW
    assert item is not None
    assert item.status is SavedItemStatus.ACTIVE
    assert item.schedule_kind is ScheduleKind.STANDARD
    assert item.next_due_on == date(2026, 9, 13)
    assert event is not None
    assert event.state is ResurfacingState.CLOSED
    assert event.close_reason is ResurfacingCloseReason.PAUSED
    assert event.outcome is None
    assert event.open_in_flight_until is None

    clock.current = NOW + timedelta(seconds=75)
    with action_store.factory() as database:
        resumed = service.resume(
            database,
            user_id=action_store.ids.user,
            session_id="pause-session",
            idempotency_key="resume_action_12345",
        )
        database.commit()
    with action_store.factory() as database:
        resume_replay = service.resume(
            database,
            user_id=action_store.ids.user,
            session_id="pause-session",
            idempotency_key="resume_action_12345",
        )
        resume_noop = service.resume(
            database,
            user_id=action_store.ids.user,
            session_id="pause-session",
            idempotency_key="resume_action_other_12345",
        )
        database.commit()
        user = database.get(User, action_store.ids.user)
        item = database.get(SavedItem, action_store.ids.item)
        event = database.get(ResurfacingEvent, action_store.ids.event)
        logs = list(
            database.scalars(
                select(ActionEvent).where(
                    ActionEvent.event_name.in_(
                        (
                            ActionEventName.PAUSE_ENABLED,
                            ActionEventName.PAUSE_DISABLED,
                        )
                    )
                )
            )
        )

    assert resumed.status is ActionApplyStatus.APPLIED
    assert resumed.paused_duration_seconds == 75
    assert resume_replay.status is ActionApplyStatus.REPLAYED
    assert resume_replay.paused_duration_seconds == 75
    assert resume_noop.status is ActionApplyStatus.UNCHANGED
    assert user is not None and user.resurfacing_paused_at is None
    assert item is not None and item.next_due_on == date(2026, 9, 13)
    assert event is not None
    assert event.state is ResurfacingState.CLOSED
    assert event.close_reason is ResurfacingCloseReason.PAUSED
    assert len(logs) == 2
    assert logs[0].properties == {"had_active_card": True}
    assert logs[1].properties == {"paused_duration_seconds": 75}


def test_concurrent_identical_pause_records_one_transition(action_store) -> None:
    barrier = Barrier(2)

    def pause(_: int) -> ActionApplyStatus:
        service = ResurfacingActionService(clock=FrozenClock(), checker=FakeChecker())
        with action_store.factory() as database:
            barrier.wait(timeout=5)
            result = service.pause(
                database,
                user_id=action_store.ids.user,
                session_id="concurrent-pause-session",
                idempotency_key="concurrent_pause_action_12345",
            )
            database.commit()
            return result.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(pause, range(2)))

    with action_store.factory() as database:
        count = database.scalar(
            select(func.count())
            .select_from(ActionEvent)
            .where(ActionEvent.event_name == ActionEventName.PAUSE_ENABLED)
        )
    assert sorted(statuses) == sorted(
        (ActionApplyStatus.APPLIED, ActionApplyStatus.REPLAYED)
    )
    assert count == 1


def test_undo_while_paused_reactivates_item_without_showing_card(action_store) -> None:
    service = ResurfacingActionService(clock=FrozenClock(), checker=FakeChecker())
    with action_store.factory() as database:
        dismissed = service.dismiss(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            session_id="paused-undo-session",
            idempotency_key="paused_undo_dismiss_12345",
        )
        database.commit()
    with action_store.factory() as database:
        service.pause(
            database,
            user_id=action_store.ids.user,
            session_id="paused-undo-session",
            idempotency_key="pause_before_undo_12345",
        )
        database.commit()
    with action_store.factory() as database:
        result = service.undo_dismiss(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            item_id=action_store.ids.item,
            dismiss_action_event_id=dismissed.dismiss_action_event_id,
            original_session_id=dismissed.original_session_id,
            dismissal_local_date=dismissed.local_date_slot,
            undo_expires_at=dismissed.undo_expires_at,
            session_id="paused-undo-session",
            idempotency_key="paused_undo_action_12345",
        )
        database.commit()
        item = database.get(SavedItem, action_store.ids.item)
        event = database.get(ResurfacingEvent, action_store.ids.event)

    assert result.event_state is ResurfacingState.CLOSED
    assert item is not None and item.status is SavedItemStatus.ACTIVE
    assert event is not None
    assert event.state is ResurfacingState.CLOSED
    assert event.close_reason is ResurfacingCloseReason.DISMISS_UNDONE


def test_open_success_uses_two_events_clears_schedule_and_does_not_claim_reading(
    action_store,
) -> None:
    checker = FakeChecker()
    service = ResurfacingActionService(clock=FrozenClock(), checker=checker)
    result = service.open_source(
        action_store.factory,
        user_id=action_store.ids.user,
        resurfacing_event_id=action_store.ids.event,
        session_id="session-one",
        idempotency_key="open_action_12345",
    )
    replay = service.open_source(
        action_store.factory,
        user_id=action_store.ids.user,
        resurfacing_event_id=action_store.ids.event,
        session_id="session-one",
        idempotency_key="open_action_12345",
    )

    assert result.status is OpenActionStatus.OPENED
    assert result.destination_url == "https://example.com/private/one?token=never-log"
    assert replay.status is OpenActionStatus.REPLAYED_OPENED
    assert replay.destination_url == result.destination_url
    assert len(checker.calls) == 1
    with action_store.factory() as database:
        item = database.get(SavedItem, action_store.ids.item)
        event = database.get(ResurfacingEvent, action_store.ids.event)
        actions = list(database.scalars(select(ActionEvent).order_by(ActionEvent.occurred_at, ActionEvent.id)))
        assert item.status is SavedItemStatus.ACTIVE
        assert item.schedule_kind is ScheduleKind.NONE
        assert item.next_due_on is None
        assert item.schedule_timezone is None
        assert item.last_source_opened_at == NOW
        assert item.source_text == "private source snapshot"
        assert event.state is ResurfacingState.RESOLVED
        assert event.outcome is ResurfacingOutcome.OPENED
        assert event.open_in_flight_until is None
        assert {action.event_name for action in actions} == {
            ActionEventName.OPEN_ATTEMPT,
            ActionEventName.SOURCE_OPENED,
        }
        attempt = next(a for a in actions if a.event_name is ActionEventName.OPEN_ATTEMPT)
        opened = next(a for a in actions if a.event_name is ActionEventName.SOURCE_OPENED)
        assert attempt.actor is ActionActor.USER
        assert opened.actor is ActionActor.SYSTEM
        assert attempt.idempotency_key != opened.idempotency_key
        assert attempt.correlation_id == opened.correlation_id
        assert opened.properties == {
            "open_attempt_id": attempt.properties["open_attempt_id"],
            "reachability_method": "head",
            "status_class": "2xx",
            "handoff": "external_redirect",
        }
        serialized = repr([action.properties for action in actions]).lower()
        assert "read" not in serialized
        assert "token=never-log" not in serialized


def test_open_failure_clears_lease_keeps_card_actionable_and_replays(action_store) -> None:
    checker = FakeChecker(
        error=SourceReachabilityError(
            ReachabilityErrorCode.HTTP_STATUS,
            "unavailable",
            status_code=503,
            retryable=True,
        )
    )
    service = ResurfacingActionService(clock=FrozenClock(), checker=checker)
    first = service.open_source(
        action_store.factory,
        user_id=action_store.ids.user,
        resurfacing_event_id=action_store.ids.event,
        session_id="session-one",
        idempotency_key="failed_open_12345",
    )
    replay = service.open_source(
        action_store.factory,
        user_id=action_store.ids.user,
        resurfacing_event_id=action_store.ids.event,
        session_id="session-one",
        idempotency_key="failed_open_12345",
    )

    assert first.status is OpenActionStatus.FAILED
    assert replay.status is OpenActionStatus.REPLAYED_FAILED
    assert len(checker.calls) == 1
    with action_store.factory() as database:
        item = database.get(SavedItem, action_store.ids.item)
        event = database.get(ResurfacingEvent, action_store.ids.event)
        assert item.schedule_kind is ScheduleKind.STANDARD
        assert item.next_due_on == date(2026, 9, 13)
        assert item.last_source_opened_at is None
        assert event.state is ResurfacingState.SHOWN
        assert event.outcome is None
        assert event.open_in_flight_until is None
        failed = database.scalar(
            select(ActionEvent).where(ActionEvent.event_name == ActionEventName.OPEN_FAILED)
        )
        assert failed.properties["error_type"] == "http_status"
        assert failed.properties["status_class"] == "5xx"
        assert failed.properties["retryable"] is True

        later = service.later(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            session_id="session-one",
            idempotency_key="later_after_failure_12345",
            choice=LaterChoice.PLUS_7,
        )
        database.commit()
        assert later.next_due_on == date(2026, 9, 22)


def test_stale_open_completion_cannot_replace_another_resolved_outcome(
    action_store,
) -> None:
    class StaleDuringCheck:
        def check(self, source_url: str) -> SourceReachability:
            with action_store.factory() as competing:
                event = competing.get(ResurfacingEvent, action_store.ids.event)
                item = competing.get(SavedItem, action_store.ids.item)
                event.state = ResurfacingState.RESOLVED
                event.outcome = ResurfacingOutcome.DISMISSED
                event.outcome_at = NOW
                event.closed_at = NOW
                event.open_in_flight_until = None
                item.status = SavedItemStatus.DISMISSED
                competing.commit()
            return SourceReachability(
                final_url="https://publisher.example/final",
                method="head",
                status_class="2xx",
            )

    result = ResurfacingActionService(
        clock=FrozenClock(), checker=StaleDuringCheck()
    ).open_source(
        action_store.factory,
        user_id=action_store.ids.user,
        resurfacing_event_id=action_store.ids.event,
        session_id="session-one",
        idempotency_key="stale_open_action_12345",
    )

    assert result.status is OpenActionStatus.FAILED
    assert result.error_type == "stale_state"
    with action_store.factory() as database:
        event = database.get(ResurfacingEvent, action_store.ids.event)
        item = database.get(SavedItem, action_store.ids.item)
        assert event.outcome is ResurfacingOutcome.DISMISSED
        assert item.status is SavedItemStatus.DISMISSED
        assert item.schedule_kind is ScheduleKind.STANDARD
        names = list(database.scalars(select(ActionEvent.event_name)))
        assert names.count(ActionEventName.OPEN_ATTEMPT) == 1
        assert names.count(ActionEventName.OPEN_FAILED) == 1
        assert ActionEventName.SOURCE_OPENED not in names


def test_item_deletion_during_open_check_returns_safe_failure(action_store) -> None:
    class DeleteDuringCheck:
        def check(self, source_url: str) -> SourceReachability:
            with action_store.factory() as competing:
                assert SavedItemRepository().hard_delete(
                    competing,
                    user_id=action_store.ids.user,
                    item_id=action_store.ids.item,
                )
                competing.commit()
            return SourceReachability(
                final_url="https://publisher.example/final",
                method="head",
                status_class="2xx",
            )

    result = ResurfacingActionService(
        clock=FrozenClock(), checker=DeleteDuringCheck()
    ).open_source(
        action_store.factory,
        user_id=action_store.ids.user,
        resurfacing_event_id=action_store.ids.event,
        session_id="session-one",
        idempotency_key="deleted_open_action_12345",
    )

    assert result.status is OpenActionStatus.FAILED
    assert result.error_type == "stale_state"
    with action_store.factory() as database:
        assert database.get(SavedItem, action_store.ids.item) is None
        assert database.get(ResurfacingEvent, action_store.ids.event) is None
        assert database.scalar(select(func.count()).select_from(ActionEvent)) == 0


@pytest.mark.parametrize("method_name", ["later", "dismiss"])
def test_later_and_dismiss_reject_an_unexpired_open_lease(
    action_store,
    method_name: str,
) -> None:
    service = ResurfacingActionService(clock=FrozenClock(), checker=FakeChecker())
    with action_store.factory() as database:
        event = database.get(ResurfacingEvent, action_store.ids.event)
        event.open_in_flight_until = NOW + timedelta(seconds=10)
        database.commit()
    with action_store.factory() as database:
        kwargs = {
            "user_id": action_store.ids.user,
            "resurfacing_event_id": action_store.ids.event,
            "session_id": "session-one",
            "idempotency_key": f"{method_name}_lease_12345",
        }
        if method_name == "later":
            kwargs["choice"] = LaterChoice.PLUS_3
        with pytest.raises(ActionRejected) as rejected:
            getattr(service, method_name)(database, **kwargs)
        assert rejected.value.code is ActionErrorCode.OPEN_IN_FLIGHT
        database.rollback()
    with action_store.factory() as database:
        assert database.get(ResurfacingEvent, action_store.ids.event).state is ResurfacingState.SHOWN
        assert database.get(SavedItem, action_store.ids.item).status is SavedItemStatus.ACTIVE
        assert database.scalar(select(func.count()).select_from(ActionEvent)) == 0


def test_action_owner_scope_and_first_resolved_outcome_win(action_store) -> None:
    service = ResurfacingActionService(clock=FrozenClock(), checker=FakeChecker())
    with action_store.factory() as database:
        foreign = _create_user(database, suffix="foreign")
        database.commit()
        with pytest.raises(ActionRejected) as hidden:
            service.dismiss(
                database,
                user_id=foreign.id,
                resurfacing_event_id=action_store.ids.event,
                session_id="foreign-session",
                idempotency_key="foreign_action_12345",
            )
        assert hidden.value.code is ActionErrorCode.NOT_FOUND

    with action_store.factory() as database:
        service.dismiss(
            database,
            user_id=action_store.ids.user,
            resurfacing_event_id=action_store.ids.event,
            session_id="session-one",
            idempotency_key="winner_action_12345",
        )
        database.commit()
    with action_store.factory() as database:
        with pytest.raises(ActionRejected) as stale:
            service.later(
                database,
                user_id=action_store.ids.user,
                resurfacing_event_id=action_store.ids.event,
                session_id="session-one",
                idempotency_key="loser_action_12345",
                choice=LaterChoice.PLUS_3,
            )
        assert stale.value.code is ActionErrorCode.NOT_ACTIONABLE
    with action_store.factory() as database:
        event = database.get(ResurfacingEvent, action_store.ids.event)
        assert event.outcome is ResurfacingOutcome.DISMISSED
        assert database.scalar(select(func.count()).select_from(ActionEvent)) == 1


def test_failed_action_event_insert_rolls_back_domain_state(action_store) -> None:
    class FailingRepository(EventRepository):
        def create_action_event(self, session: Session, **kwargs):
            raise RuntimeError("simulated event write failure")

    service = ResurfacingActionService(
        repository=FailingRepository(),
        clock=FrozenClock(),
        checker=FakeChecker(),
    )
    with action_store.factory() as database:
        with pytest.raises(RuntimeError, match="simulated event write failure"):
            service.later(
                database,
                user_id=action_store.ids.user,
                resurfacing_event_id=action_store.ids.event,
                session_id="session-one",
                idempotency_key="atomic_failure_12345",
                choice=LaterChoice.PLUS_3,
            )
        database.rollback()
    with action_store.factory() as database:
        assert database.get(ResurfacingEvent, action_store.ids.event).state is ResurfacingState.SHOWN
        assert database.get(SavedItem, action_store.ids.item).next_due_on == date(2026, 9, 13)
        assert database.scalar(select(func.count()).select_from(ActionEvent)) == 0


def test_safe_reachability_uses_head_then_bounded_get_fallback_without_network() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(405, request=request)
        return httpx.Response(206, content=b"small body", request=request)

    checker = SafeSourceReachabilityChecker(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=lambda host, port: {"93.184.216.34"},
        peer_ip_getter=lambda response: "93.184.216.34",
        max_bytes=2048,
    )
    result = checker.check("https://example.com/article#fragment")

    assert result == SourceReachability(
        final_url="https://example.com/article",
        method="get",
        status_class="2xx",
    )
    assert [request.method for request in requests] == ["HEAD", "GET"]
    assert requests[1].headers["range"] == "bytes=0-2047"


def test_safe_reachability_never_replays_remote_cookies_between_checks() -> None:
    seen_cookies: list[str | None] = []
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        seen_cookies.append(request.headers.get("cookie"))
        headers = {"set-cookie": "probe=sticky; Path=/"} if request_count == 1 else {}
        return httpx.Response(204, headers=headers, request=request)

    checker = SafeSourceReachabilityChecker(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=lambda host, port: {"93.184.216.34"},
        peer_ip_getter=lambda response: "93.184.216.34",
    )

    checker.check("https://example.com/first")
    checker.check("https://example.com/second")

    assert seen_cookies == [None, None]


def test_safe_reachability_revalidates_redirect_dns_and_connected_peer() -> None:
    resolved: list[str] = []

    def resolver(host: str, port: int) -> set[str]:
        resolved.append(host)
        return {"93.184.216.34"} if host == "example.com" else {"151.101.1.69"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(
                302,
                headers={"location": "https://publisher.example/final"},
                request=request,
            )
        return httpx.Response(204, request=request)

    checker = SafeSourceReachabilityChecker(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=resolver,
        peer_ip_getter=lambda response: (
            "93.184.216.34"
            if response.request.url.host == "example.com"
            else "151.101.1.69"
        ),
    )
    result = checker.check("https://example.com/start")

    assert result.final_url == "https://publisher.example/final"
    assert resolved == ["example.com", "publisher.example"]

    mismatched = SafeSourceReachabilityChecker(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=lambda host, port: {"93.184.216.34"},
        peer_ip_getter=lambda response: "127.0.0.1",
    )
    with pytest.raises(SourceReachabilityError) as rejected:
        mismatched.check("https://example.com/start")
    assert rejected.value.code is ReachabilityErrorCode.PEER_IP_MISMATCH


@pytest.fixture
def action_ui(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'action-ui.db'}"
    engine = build_engine(database_url)
    initialize_database(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    checker = FakeChecker()
    clock = FrozenClock()
    settings = Settings(
        app_env="development",
        app_secret="action-ui-session-secret-that-is-long-enough",
        database_url=database_url,
        access_codes=ACCESS_CODE,
    )
    app = create_app(
        database_initializer=lambda: None,
        session_factory=factory,
        capture_service=CaptureService(
            UnusedExtractor(),
            fingerprint_secret="unused-fingerprint-secret",
            clock=clock,
        ),
        app_settings=settings,
        clock=clock,
        resurfacing_engine=ResurfacingEngine(clock=clock),
        action_service=ResurfacingActionService(clock=clock, checker=checker),
    )
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, factory=factory, checker=checker)
    engine.dispose()


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _login_and_due_card(action_ui) -> tuple[User, SavedItem]:
    home = action_ui.client.get("/")
    response = action_ui.client.post(
        "/session",
        data={
            "access_code": ACCESS_CODE,
            "timezone_name": "Asia/Shanghai",
            "csrf_token": _csrf(home.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with action_ui.factory() as database:
        user = database.scalar(select(User))
        assert user is not None
        item = SavedItemRepository().create(
            database,
            user_id=user.id,
            source_url="https://example.com/private/path?token=not-in-card",
            source_url_hash=sha256(f"{user.id}:ui".encode()).hexdigest(),
            title="Actionable saved article",
            source_domain="example.com",
            next_due_on=date(2026, 9, 14),
            schedule_timezone=user.timezone,
        ).item
        database.commit()
        database.expunge(user)
        database.expunge(item)
        return user, item


def _event_id(html: str) -> str:
    match = re.search(r'action="/resurfacing/([^/]+)/open"', html)
    assert match is not None
    return match.group(1)


def _item_version(html: str) -> str:
    match = re.search(r'name="expected_updated_at" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _mount(action_ui, page_text: str, event_id: str) -> str:
    csrf = _csrf(page_text)
    response = action_ui.client.post(
        f"/resurfacing/{event_id}/impression",
        data={"csrf_token": csrf},
    )
    assert response.status_code == 204
    return csrf


def test_card_exposes_fixed_action_routes_but_no_source_url_and_starts_disabled(
    action_ui,
) -> None:
    _login_and_due_card(action_ui)
    page = action_ui.client.get("/items")
    event_id = _event_id(page.text)

    assert "data-resurfacing-actions" in page.text
    assert "data-resurfacing-actions\n        aria-label" in page.text
    action_section = page.text.split("data-resurfacing-actions", 1)[1].split("</fieldset>", 1)[0]
    assert "disabled" in action_section
    assert f'/resurfacing/{event_id}/open' in page.text
    assert f'/resurfacing/{event_id}/later' in page.text
    assert f'/resurfacing/{event_id}/dismiss' in page.text
    assert "3 days · 2026-09-18" in page.text
    assert "7 days · 2026-09-22" in page.text
    assert 'min="2026-09-16"' in page.text
    assert "token=not-in-card" not in page.text


def test_active_resurfacing_cycle_locks_the_separate_date_editor(action_ui) -> None:
    _, item = _login_and_due_card(action_ui)
    page = action_ui.client.get("/items")
    event_id = _event_id(page.text)

    pending_detail = action_ui.client.get(f"/items/{item.id}")
    assert "temporarily locked" in pending_detail.text
    assert f'/items/{item.id}/date' not in pending_detail.text

    csrf = _mount(action_ui, page.text, event_id)
    shown_detail = action_ui.client.get(f"/items/{item.id}")
    assert "temporarily locked" in shown_detail.text
    blocked = action_ui.client.post(
        f"/items/{item.id}/date",
        data={"csrf_token": csrf, "revisit_on": "2026-09-30"},
    )

    assert blocked.status_code == 409
    assert "resurfacing decision is active" in blocked.text
    with action_ui.factory() as database:
        persisted = database.get(SavedItem, item.id)
        event = database.get(ResurfacingEvent, event_id)
        assert persisted.next_due_on == date(2026, 9, 14)
        assert event.state is ResurfacingState.SHOWN


def test_date_edit_compare_and_set_cannot_overwrite_a_later_result(action_store) -> None:
    with action_store.factory() as stale_editor:
        stale_item = stale_editor.get(SavedItem, action_store.ids.item)
        expected_updated_at = stale_item.updated_at

        with action_store.factory() as action_database:
            result = ResurfacingActionService(
                clock=FrozenClock(),
                checker=FakeChecker(),
            ).later(
                action_database,
                user_id=action_store.ids.user,
                resurfacing_event_id=action_store.ids.event,
                session_id="later-winner",
                idempotency_key="later_before_stale_edit_12345",
                choice=LaterChoice.PLUS_7,
            )
            action_database.commit()

        updated = SavedItemRepository().update_schedule_if_no_active_cycle(
            stale_editor,
            user_id=action_store.ids.user,
            item_id=action_store.ids.item,
            expected_updated_at=expected_updated_at,
            schedule_kind=ScheduleKind.STANDARD,
            next_due_on=date(2026, 10, 1),
            schedule_timezone="Asia/Shanghai",
        )
        stale_editor.commit()

    assert result.next_due_on == date(2026, 9, 22)
    assert updated is False
    with action_store.factory() as verify:
        item = verify.get(SavedItem, action_store.ids.item)
        event = verify.get(ResurfacingEvent, action_store.ids.event)
        assert item.next_due_on == date(2026, 9, 22)
        assert event.outcome is ResurfacingOutcome.LATER_CONFIRMED


def test_stale_http_date_form_cannot_overwrite_a_later_result(action_ui) -> None:
    _, item = _login_and_due_card(action_ui)
    old_detail = action_ui.client.get(f"/items/{item.id}")
    old_csrf = _csrf(old_detail.text)
    old_version = _item_version(old_detail.text)

    card_page = action_ui.client.get("/items")
    event_id = _event_id(card_page.text)
    card_csrf = _mount(action_ui, card_page.text, event_id)
    later = action_ui.client.post(
        f"/resurfacing/{event_id}/later",
        data={
            "csrf_token": card_csrf,
            "idempotency_key": "later_before_http_stale_edit_12345",
            "choice": "plus_3",
            "custom_date": "",
        },
        follow_redirects=False,
    )
    assert later.status_code == 303

    stale_update = action_ui.client.post(
        f"/items/{item.id}/date",
        data={
            "csrf_token": old_csrf,
            "expected_updated_at": old_version,
            "revisit_on": "2026-10-01",
        },
    )

    assert stale_update.status_code == 409
    assert "item changed before the date could be saved" in stale_update.text
    with action_ui.factory() as database:
        persisted = database.get(SavedItem, item.id)
        event = database.get(ResurfacingEvent, event_id)
        assert persisted.next_due_on == date(2026, 9, 18)
        assert event.outcome is ResurfacingOutcome.LATER_CONFIRMED


def test_http_later_flow_resolves_once_and_refresh_does_not_duplicate(action_ui) -> None:
    _, item = _login_and_due_card(action_ui)
    page = action_ui.client.get("/items")
    event_id = _event_id(page.text)
    csrf = _mount(action_ui, page.text, event_id)
    data = {
        "csrf_token": csrf,
        "idempotency_key": "http_later_action_12345",
        "choice": "plus_3",
        "custom_date": "",
    }
    first = action_ui.client.post(
        f"/resurfacing/{event_id}/later",
        data=data,
        follow_redirects=False,
    )
    replay = action_ui.client.post(
        f"/resurfacing/{event_id}/later",
        data=data,
        follow_redirects=False,
    )

    assert first.status_code == replay.status_code == 303
    assert first.headers["location"] == "/items?notice=later"
    result_page = action_ui.client.get(first.headers["location"])
    assert "rescheduled to the date shown below" in result_page.text
    assert "Revisit 2026-09-18" in result_page.text
    assert "data-resurfacing-card" not in result_page.text
    with action_ui.factory() as database:
        persisted = database.get(SavedItem, item.id)
        event = database.get(ResurfacingEvent, event_id)
        assert persisted.next_due_on == date(2026, 9, 18)
        assert event.outcome is ResurfacingOutcome.LATER_CONFIRMED
        assert database.scalar(
            select(func.count()).select_from(ActionEvent).where(
                ActionEvent.event_name == ActionEventName.LATER_CONFIRMED
            )
        ) == 1


def test_http_dismiss_flow_preserves_item_and_rejects_missing_csrf(action_ui) -> None:
    _, item = _login_and_due_card(action_ui)
    page = action_ui.client.get("/items")
    event_id = _event_id(page.text)
    csrf = _mount(action_ui, page.text, event_id)
    path = f"/resurfacing/{event_id}/dismiss"
    missing_csrf = action_ui.client.post(
        path,
        data={"idempotency_key": "http_dismiss_action_12345"},
    )
    assert missing_csrf.status_code == 403

    response = action_ui.client.post(
        path,
        data={
            "csrf_token": csrf,
            "idempotency_key": "http_dismiss_action_12345",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/items?notice=dismissed"
    result_page = action_ui.client.get(response.headers["location"])
    assert "Dismissed · Will not resurface · Retained date 2026-09-14" in result_page.text
    refreshed = action_ui.client.get("/items")
    assert "Dismissed · Will not resurface · Retained date 2026-09-14" in refreshed.text
    assert "data-resurfacing-card" not in refreshed.text
    with action_ui.factory() as database:
        persisted = database.get(SavedItem, item.id)
        assert persisted is not None
        assert persisted.status is SavedItemStatus.DISMISSED
        assert persisted.next_due_on == date(2026, 9, 14)


def test_http_open_success_issues_external_handoff_and_failure_restores_card(
    action_ui,
) -> None:
    _, item = _login_and_due_card(action_ui)
    page = action_ui.client.get("/items")
    event_id = _event_id(page.text)
    csrf = _mount(action_ui, page.text, event_id)
    response = action_ui.client.post(
        f"/resurfacing/{event_id}/open",
        data={"csrf_token": csrf, "idempotency_key": "http_open_action_12345"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "https://example.com/private/path?token=not-in-card"
    assert response.headers["referrer-policy"] == "no-referrer"
    with action_ui.factory() as database:
        persisted = database.get(SavedItem, item.id)
        assert persisted.schedule_kind is ScheduleKind.NONE
        assert persisted.last_source_opened_at == NOW


def test_http_open_failure_returns_same_shown_card_with_actions_enabled(action_ui) -> None:
    action_ui.checker.error = SourceReachabilityError(
        ReachabilityErrorCode.TIMEOUT,
        "timeout",
        retryable=True,
    )
    _, item = _login_and_due_card(action_ui)
    page = action_ui.client.get("/items")
    event_id = _event_id(page.text)
    csrf = _mount(action_ui, page.text, event_id)
    failed = action_ui.client.post(
        f"/resurfacing/{event_id}/open",
        data={"csrf_token": csrf, "idempotency_key": "http_failed_open_12345"},
        follow_redirects=False,
    )
    assert failed.status_code == 303
    retry_page = action_ui.client.get(failed.headers["location"])
    assert "source could not be reached safely" in retry_page.text
    assert "message message-error" in retry_page.text
    assert "data-resurfacing-card" in retry_page.text
    action_section = retry_page.text.split("data-resurfacing-actions", 1)[1].split("</fieldset>", 1)[0]
    assert "disabled" not in action_section
    assert "data-resurfacing-mount" not in retry_page.text
    with action_ui.factory() as database:
        event = database.get(ResurfacingEvent, event_id)
        persisted = database.get(SavedItem, item.id)
        assert event.state is ResurfacingState.SHOWN
        assert event.open_in_flight_until is None
        assert persisted.schedule_kind is ScheduleKind.STANDARD


@pytest.mark.parametrize("resolving_action", ["later", "dismiss"])
def test_stale_failed_open_replay_does_not_claim_resolved_card_is_actionable(
    action_ui,
    resolving_action: str,
) -> None:
    action_ui.checker.error = SourceReachabilityError(
        ReachabilityErrorCode.TIMEOUT,
        "timeout",
        retryable=True,
    )
    _login_and_due_card(action_ui)
    page = action_ui.client.get("/items")
    event_id = _event_id(page.text)
    csrf = _mount(action_ui, page.text, event_id)
    open_data = {
        "csrf_token": csrf,
        "idempotency_key": "stale_http_open_12345",
    }
    failed = action_ui.client.post(
        f"/resurfacing/{event_id}/open",
        data=open_data,
        follow_redirects=False,
    )
    assert failed.status_code == 303

    if resolving_action == "later":
        resolved = action_ui.client.post(
            f"/resurfacing/{event_id}/later",
            data={
                "csrf_token": csrf,
                "idempotency_key": "resolve_after_open_12345",
                "choice": "plus_3",
                "custom_date": "",
            },
            follow_redirects=False,
        )
    else:
        resolved = action_ui.client.post(
            f"/resurfacing/{event_id}/dismiss",
            data={
                "csrf_token": csrf,
                "idempotency_key": "resolve_after_open_12345",
            },
            follow_redirects=False,
        )
    assert resolved.status_code == 303

    replay = action_ui.client.post(
        f"/resurfacing/{event_id}/open",
        data=open_data,
        follow_redirects=False,
    )
    replay_page = action_ui.client.get(replay.headers["location"])

    assert "card is still actionable" not in replay_page.text
    assert "The earlier Open attempt did not complete" in replay_page.text
    assert "current state" in replay_page.text
    assert "data-resurfacing-card" not in replay_page.text
    assert len(action_ui.checker.calls) == 1


def test_http_dismiss_undo_uses_private_cookie_and_restores_same_card_once(
    action_ui,
) -> None:
    _, item = _login_and_due_card(action_ui)
    page = action_ui.client.get("/items")
    event_id = _event_id(page.text)
    csrf = _mount(action_ui, page.text, event_id)

    dismissed = action_ui.client.post(
        f"/resurfacing/{event_id}/dismiss",
        data={
            "csrf_token": csrf,
            "idempotency_key": "http_dismiss_for_undo_12345",
        },
        follow_redirects=False,
    )

    assert dismissed.status_code == 303
    assert dismissed.headers["location"] == "/items?notice=dismissed"
    cookie_header = dismissed.headers["set-cookie"].lower()
    assert "recallflow_dismiss_undo=" in cookie_header
    assert "httponly" in cookie_header
    assert "samesite=lax" in cookie_header
    assert "path=/" in cookie_header
    assert "recallflow_dismiss_undo" not in dismissed.headers["location"]

    dismissed_page = action_ui.client.get(dismissed.headers["location"])
    assert "Undo Dismiss" in dismissed_page.text
    assert f'action="/resurfacing/{event_id}/dismiss/undo"' in dismissed_page.text
    assert "recallflow_dismiss_undo" not in dismissed_page.text

    undone = action_ui.client.post(
        f"/resurfacing/{event_id}/dismiss/undo",
        data={
            "csrf_token": _csrf(dismissed_page.text),
            "idempotency_key": "http_undo_dismiss_12345",
        },
        follow_redirects=False,
    )

    assert undone.status_code == 303
    assert undone.headers["location"] == (
        f"/items?notice=dismiss-undone&event_id={event_id}"
    )
    assert "recallflow_dismiss_undo=" in undone.headers["set-cookie"].lower()
    assert "max-age=0" in undone.headers["set-cookie"].lower()

    restored_page = action_ui.client.get(undone.headers["location"])
    assert "Dismiss undone. The card is ready again." in restored_page.text
    assert "data-resurfacing-card" in restored_page.text
    assert _event_id(restored_page.text) == event_id
    action_section = restored_page.text.split("data-resurfacing-actions", 1)[1].split(
        "</fieldset>", 1
    )[0]
    assert "disabled" not in action_section
    assert "data-resurfacing-mount" not in restored_page.text

    refreshed = action_ui.client.get(undone.headers["location"])
    assert "data-resurfacing-card" in refreshed.text
    assert "data-resurfacing-mount" not in refreshed.text
    missing_cookie_retry = action_ui.client.post(
        f"/resurfacing/{event_id}/dismiss/undo",
        data={
            "csrf_token": _csrf(refreshed.text),
            "idempotency_key": "http_missing_cookie_undo_12345",
        },
        follow_redirects=False,
    )
    assert missing_cookie_retry.status_code == 409
    assert "max-age=0" in missing_cookie_retry.headers["set-cookie"].lower()
    with action_ui.factory() as database:
        persisted = database.get(SavedItem, item.id)
        event = database.get(ResurfacingEvent, event_id)
        impression_count = database.scalar(
            select(func.count()).select_from(ActionEvent).where(
                ActionEvent.event_name == ActionEventName.RESURFACING_IMPRESSION
            )
        )
        undo_count = database.scalar(
            select(func.count()).select_from(ActionEvent).where(
                ActionEvent.event_name == ActionEventName.DISMISS_UNDONE
            )
        )
        assert persisted is not None
        assert persisted.status is SavedItemStatus.ACTIVE
        assert persisted.schedule_kind is ScheduleKind.STANDARD
        assert persisted.next_due_on == date(2026, 9, 14)
        assert event is not None
        assert event.state is ResurfacingState.SHOWN
        assert event.outcome is None
        assert impression_count == 1
        assert undo_count == 1


def test_http_pause_resume_controls_require_csrf_and_preserve_schedule(
    action_ui,
) -> None:
    user, item = _login_and_due_card(action_ui)
    active_page = action_ui.client.get("/items")
    event_id = _event_id(active_page.text)
    assert "Resurfacing is active" in active_page.text
    assert 'action="/resurfacing/pause"' in active_page.text
    assert "Pause resurfacing" in active_page.text
    assert "/static/resurfacing.js" in active_page.text

    rejected_pause = action_ui.client.post(
        "/resurfacing/pause",
        data={"idempotency_key": "http_pause_no_csrf_12345"},
        follow_redirects=False,
    )
    assert rejected_pause.status_code == 403

    paused = action_ui.client.post(
        "/resurfacing/pause",
        data={
            "csrf_token": _csrf(active_page.text),
            "idempotency_key": "http_pause_action_12345",
        },
        follow_redirects=False,
    )
    assert paused.status_code == 303
    assert paused.headers["location"] == "/items?notice=paused"

    paused_page = action_ui.client.get(paused.headers["location"])
    assert "Resurfacing is paused" in paused_page.text
    assert "Saved dates are unchanged" in paused_page.text
    assert 'action="/resurfacing/resume"' in paused_page.text
    assert "Resume resurfacing" in paused_page.text
    assert "data-resurfacing-card" not in paused_page.text
    assert "/static/resurfacing.js" in paused_page.text

    rejected_resume = action_ui.client.post(
        "/resurfacing/resume",
        data={"idempotency_key": "http_resume_no_csrf_12345"},
        follow_redirects=False,
    )
    assert rejected_resume.status_code == 403

    resumed = action_ui.client.post(
        "/resurfacing/resume",
        data={
            "csrf_token": _csrf(paused_page.text),
            "idempotency_key": "http_resume_action_12345",
        },
        follow_redirects=False,
    )
    assert resumed.status_code == 303
    assert resumed.headers["location"] == "/items?notice=resumed"

    resumed_page = action_ui.client.get(resumed.headers["location"])
    assert "Resurfacing is active again." in resumed_page.text
    assert "Pause resurfacing" in resumed_page.text
    assert "data-resurfacing-card" not in resumed_page.text
    with action_ui.factory() as database:
        persisted_user = database.get(User, user.id)
        persisted_item = database.get(SavedItem, item.id)
        event = database.get(ResurfacingEvent, event_id)
        control_events = list(
            database.scalars(
                select(ActionEvent).where(
                    ActionEvent.event_name.in_(
                        (
                            ActionEventName.PAUSE_ENABLED,
                            ActionEventName.PAUSE_DISABLED,
                        )
                    )
                )
            )
        )
        assert persisted_user is not None
        assert persisted_user.resurfacing_paused_at is None
        assert persisted_item is not None
        assert persisted_item.status is SavedItemStatus.ACTIVE
        assert persisted_item.schedule_kind is ScheduleKind.STANDARD
        assert persisted_item.next_due_on == date(2026, 9, 14)
        assert event is not None
        assert event.state is ResurfacingState.CLOSED
        assert event.close_reason is ResurfacingCloseReason.PAUSED
        assert event.outcome is None
        assert len(control_events) == 2

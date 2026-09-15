from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.database import build_engine, initialize_database, schema_version
from app.models import (
    ActionActor,
    ActionEvent,
    ActionEventName,
    ExtractionStatus,
    ResurfacingEvent,
    ResurfacingState,
    SavedItem,
    SavedItemStatus,
    ScheduleKind,
    User,
)
from app.repositories import (
    EventRepository,
    IdempotencyConflict,
    OwnershipError,
    SavedItemRepository,
    UserRepository,
)


@pytest.fixture
def session(tmp_path) -> Session:
    engine = build_engine(f"sqlite:///{tmp_path / 'model-test.db'}")
    initialize_database(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as database_session:
        yield database_session
    engine.dispose()


def create_user(session: Session, suffix: str = "one") -> User:
    return UserRepository().create(
        session,
        access_code_hash=f"pilot-code-hash-{suffix}",
        timezone_name="Asia/Shanghai",
    )


def create_item(
    session: Session,
    *,
    user: User,
    url_hash: str = "a" * 64,
    title: str = "A useful article",
) -> SavedItem:
    result = SavedItemRepository().create(
        session,
        user_id=user.id,
        source_url="https://example.com/article",
        source_url_hash=url_hash,
        title=title,
        source_domain="example.com",
        source_text="A bounded private source snapshot.",
        source_excerpt="A bounded private source snapshot.",
        extraction_status=ExtractionStatus.SUCCESS,
        next_due_on=date(2026, 9, 21),
    )
    assert result.created is True
    return result.item


def test_schema_initialization_is_numbered_and_idempotent(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'schema-test.db'}")

    initialize_database(engine)
    initialize_database(engine)

    with engine.connect() as connection:
        versions = connection.execute(
            select(schema_version.c.version, schema_version.c.name)
        ).all()
        tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
        }

    assert versions == [
        (1, "minimum_data_model"),
        (2, "capture_v1_fields"),
        (3, "unique_pilot_access_code"),
        (4, "resurfacing_actions"),
    ]
    assert {"users", "saved_items", "resurfacing_events", "action_events"} <= tables
    engine.dispose()


def test_create_user_and_item_then_query_with_owner_scope(session: Session) -> None:
    owner = create_user(session)
    other_user = create_user(session, "two")
    item = create_item(session, user=owner)

    repository = SavedItemRepository()
    found = repository.get(session, user_id=owner.id, item_id=item.id)

    assert found is item
    assert found.intent.value == "READ"
    assert found.status is SavedItemStatus.ACTIVE
    assert repository.get(session, user_id=other_user.id, item_id=item.id) is None
    assert repository.list_for_user(session, user_id=owner.id) == [item]


def test_update_schedule_enforces_date_shape(session: Session) -> None:
    user = create_user(session)
    item = create_item(session, user=user)
    repository = SavedItemRepository()

    updated = repository.update_schedule(
        session,
        user_id=user.id,
        item_id=item.id,
        schedule_kind=ScheduleKind.STANDARD,
        next_due_on=date(2026, 10, 1),
    )

    assert updated is not None
    assert updated.next_due_on == date(2026, 10, 1)
    with pytest.raises(ValueError, match="require a due date"):
        repository.update_schedule(
            session,
            user_id=user.id,
            item_id=item.id,
            schedule_kind=ScheduleKind.STANDARD,
            next_due_on=None,
        )


def test_dismiss_preserves_item_and_schedule(session: Session) -> None:
    user = create_user(session)
    item = create_item(session, user=user)
    original_due_date = item.next_due_on

    dismissed = SavedItemRepository().dismiss(
        session,
        user_id=user.id,
        item_id=item.id,
    )

    assert dismissed is not None
    assert dismissed.status is SavedItemStatus.DISMISSED
    assert dismissed.next_due_on == original_due_date
    assert session.get(SavedItem, item.id) is not None


def test_hard_delete_item_cascades_private_events(session: Session) -> None:
    user = create_user(session)
    item = create_item(session, user=user)
    event_repository = EventRepository()
    resurfacing = event_repository.create_resurfacing_event(
        session,
        user_id=user.id,
        item_id=item.id,
        scheduled_on_snapshot=item.next_due_on,
        local_date_slot=item.next_due_on,
        timezone_at_selection=user.timezone,
        eligible_count_snapshot=1,
    )
    action = event_repository.create_action_event(
        session,
        event_name=ActionEventName.RESURFACING_IMPRESSION,
        user_id=user.id,
        item_id=item.id,
        resurfacing_event_id=resurfacing.id,
        idempotency_key="mount-one",
        actor=ActionActor.CLIENT,
    )
    item_id, resurfacing_id, action_id = item.id, resurfacing.id, action.id
    session.commit()

    deleted = SavedItemRepository().hard_delete(
        session,
        user_id=user.id,
        item_id=item_id,
    )
    session.commit()
    session.expire_all()

    assert deleted is True
    assert session.get(SavedItem, item_id) is None
    assert session.get(ResurfacingEvent, resurfacing_id) is None
    assert session.get(ActionEvent, action_id) is None
    assert session.get(User, user.id) is not None


def test_duplicate_url_returns_existing_item_only_for_same_user(session: Session) -> None:
    first_user = create_user(session, "one")
    second_user = create_user(session, "two")
    repository = SavedItemRepository()
    original = create_item(session, user=first_user)

    duplicate = repository.create(
        session,
        user_id=first_user.id,
        source_url="https://example.com/article",
        source_url_hash="a" * 64,
        title="Duplicate title should not replace original",
        source_domain="example.com",
        next_due_on=date(2026, 10, 2),
    )
    other_owner = repository.create(
        session,
        user_id=second_user.id,
        source_url="https://example.com/article",
        source_url_hash="a" * 64,
        title="Same URL, separate user",
        source_domain="example.com",
        next_due_on=date(2026, 10, 2),
    )

    assert duplicate.created is False
    assert duplicate.item.id == original.id
    assert duplicate.item.title == "A useful article"
    assert other_owner.created is True
    assert session.scalar(select(func.count()).select_from(SavedItem)) == 2


def test_sqlite_foreign_keys_are_enabled_and_enforced(session: Session) -> None:
    assert session.scalar(text("PRAGMA foreign_keys")) == 1

    invalid_item = SavedItem(
        user_id="00000000-0000-0000-0000-000000000000",
        source_url="https://example.com/missing-owner",
        source_url_hash="f" * 64,
        title="Missing owner",
        source_domain="example.com",
        next_due_on=date(2026, 9, 21),
    )
    session.add(invalid_item)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_sqlite_datetime_round_trip_remains_aware_utc(session: Session) -> None:
    user = create_user(session)
    item = create_item(session, user=user)
    supplied = datetime(2026, 9, 14, 18, 30, tzinfo=timezone(timedelta(hours=8)))
    item.saved_at = supplied
    session.commit()
    session.expire_all()

    stored = session.get(SavedItem, item.id)

    assert stored is not None
    assert stored.saved_at == datetime(2026, 9, 14, 10, 30, tzinfo=timezone.utc)
    assert stored.saved_at.tzinfo is timezone.utc


def test_resurfacing_state_shape_requires_mount_for_shown(session: Session) -> None:
    user = create_user(session)
    item = create_item(session, user=user)
    session.commit()
    invalid = ResurfacingEvent(
        user_id=user.id,
        saved_item_id=item.id,
        scheduled_on_snapshot=item.next_due_on,
        local_date_slot=item.next_due_on,
        timezone_at_selection=user.timezone,
        eligible_count_snapshot=1,
        state=ResurfacingState.SHOWN,
        shown_at=None,
    )
    session.add(invalid)

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_unknown_schema_version_fails_closed(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'future-schema.db'}")
    initialize_database(engine)
    with engine.begin() as connection:
        connection.execute(
            schema_version.insert().values(
                version=999,
                name="future",
                applied_at=datetime.now(timezone.utc),
            )
        )

    with pytest.raises(RuntimeError, match="newer than this application"):
        initialize_database(engine)
    engine.dispose()


def test_owner_scope_blocks_cross_user_mutations_and_events(session: Session) -> None:
    owner = create_user(session, "owner")
    other = create_user(session, "other")
    item = create_item(session, user=owner)
    session.commit()

    items = SavedItemRepository()
    assert items.dismiss(session, user_id=other.id, item_id=item.id) is None
    assert items.hard_delete(session, user_id=other.id, item_id=item.id) is False
    with pytest.raises(OwnershipError):
        EventRepository().create_resurfacing_event(
            session,
            user_id=other.id,
            item_id=item.id,
            scheduled_on_snapshot=item.next_due_on,
            local_date_slot=item.next_due_on,
            timezone_at_selection=other.timezone,
            eligible_count_snapshot=1,
        )


def test_action_event_idempotency_replays_only_identical_input(session: Session) -> None:
    user = create_user(session)
    item = create_item(session, user=user)
    repository = EventRepository()
    first = repository.create_action_event(
        session,
        event_name=ActionEventName.CAPTURE_SUCCEEDED,
        user_id=user.id,
        item_id=item.id,
        idempotency_key="capture-result-1",
        actor=ActionActor.SYSTEM,
        properties={"status": "saved"},
    )
    replay = repository.create_action_event(
        session,
        event_name=ActionEventName.CAPTURE_SUCCEEDED,
        user_id=user.id,
        item_id=item.id,
        idempotency_key="capture-result-1",
        actor=ActionActor.SYSTEM,
        properties={"status": "saved"},
    )

    assert replay.id == first.id
    with pytest.raises(IdempotencyConflict):
        repository.create_action_event(
            session,
            event_name=ActionEventName.CAPTURE_FAILED,
            user_id=user.id,
            idempotency_key="capture-result-1",
            actor=ActionActor.SYSTEM,
            properties={"status": "failed"},
        )


def test_user_hard_delete_cascades_all_private_rows(session: Session) -> None:
    user = create_user(session)
    item = create_item(session, user=user)
    event = EventRepository().create_resurfacing_event(
        session,
        user_id=user.id,
        item_id=item.id,
        scheduled_on_snapshot=item.next_due_on,
        local_date_slot=item.next_due_on,
        timezone_at_selection=user.timezone,
        eligible_count_snapshot=1,
    )
    session.commit()
    item_id, event_id = item.id, event.id

    assert UserRepository().hard_delete(session, user.id) is True
    session.commit()
    session.expire_all()

    assert session.get(SavedItem, item_id) is None
    assert session.get(ResurfacingEvent, event_id) is None

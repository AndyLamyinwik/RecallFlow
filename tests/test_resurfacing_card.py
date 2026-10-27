from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from hashlib import sha256
import re
from threading import Barrier
from types import SimpleNamespace

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
    AIValueLineStatus,
    EffortBand,
    ExtractionStatus,
    ResurfacingEvent,
    ResurfacingCloseReason,
    ResurfacingOutcome,
    ResurfacingState,
    SavedItem,
    User,
)
from app.repositories import EventRepository, SavedItemRepository, UserRepository
from app.security import hash_access_code
from app.services import CaptureService, ImpressionMountStatus, ResurfacingEngine
from app.services.extraction import ArticleExtraction


ACCESS_CODE = "resurfacing-card-pilot-code-123"
NOW = datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc)
LOCAL_TODAY = date(2026, 9, 15)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


class UnusedExtractor:
    def extract(self, normalized_url: str) -> ArticleExtraction:
        raise AssertionError("The resurfacing-card tests do not fetch articles")


@pytest.fixture
def card_ui(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'resurfacing-card.db'}"
    engine = build_engine(database_url)
    initialize_database(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = Settings(
        app_env="development",
        app_secret="resurfacing-card-session-secret-long-enough",
        database_url=database_url,
        access_codes=ACCESS_CODE,
    )
    clock = FrozenClock()
    app = create_app(
        database_initializer=lambda: None,
        session_factory=factory,
        capture_service=CaptureService(
            UnusedExtractor(),
            fingerprint_secret="unused-capture-fingerprint-secret",
            clock=clock,
        ),
        app_settings=settings,
        clock=clock,
        resurfacing_engine=ResurfacingEngine(clock=clock),
    )
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, factory=factory)
    engine.dispose()


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _mount_path(html: str) -> str:
    match = re.search(r'action="(/resurfacing/[^"]+/impression)"', html)
    assert match is not None
    return match.group(1)


def _login(card_ui) -> User:
    home = card_ui.client.get("/")
    response = card_ui.client.post(
        "/session",
        data={
            "access_code": ACCESS_CODE,
            "timezone_name": "Asia/Shanghai",
            "csrf_token": _csrf(home.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with card_ui.factory() as database:
        user = database.scalar(select(User))
        assert user is not None
        database.expunge(user)
        return user


def _create_due_item(
    database: Session,
    *,
    user: User,
    suffix: str,
    due_on: date = date(2026, 9, 13),
    note: str | None = "Remember the concrete trade-off",
    excerpt: str | None = "A bounded source excerpt for restoring article context.",
) -> SavedItem:
    private_snapshot = f"PRIVATE FULL SOURCE {suffix} " + ("body " * 200)
    result = SavedItemRepository().create(
        database,
        user_id=user.id,
        source_url=f"https://example.com/{suffix}?private=do-not-render",
        source_url_hash=sha256(f"{user.id}:{suffix}".encode()).hexdigest(),
        title=f"Useful article {suffix}",
        source_domain="example.com",
        source_text=private_snapshot,
        source_text_hash=sha256(private_snapshot.encode()).hexdigest(),
        source_char_count=len(private_snapshot),
        source_excerpt=excerpt,
        extraction_status=ExtractionStatus.SUCCESS,
        saved_at=datetime(2026, 9, 1, 1, tzinfo=timezone.utc),
        why_saved_note=note,
        ai_value_line="PRIVATE AI LINE MUST NOT APPEAR",
        ai_value_line_status=AIValueLineStatus.NOT_ATTEMPTED,
        effort_band=EffortBand.SHORT,
        next_due_on=due_on,
        schedule_timezone=user.timezone,
    )
    assert result.created
    return result.item


def test_items_entry_renders_one_safe_pending_card_without_counting_impression(card_ui) -> None:
    user = _login(card_ui)
    with card_ui.factory() as database:
        selected = _create_due_item(
            database,
            user=user,
            suffix="first",
            note='<script>alert("note")</script>',
        )
        _create_due_item(
            database,
            user=user,
            suffix="second",
            due_on=date(2026, 9, 14),
        )
        database.commit()

    first_page = card_ui.client.get("/items")
    second_tab = card_ui.client.get("/items")

    assert first_page.status_code == second_tab.status_code == 200
    assert first_page.text.count("data-resurfacing-card") == 1
    assert "Useful article first" in first_page.text
    assert "example.com" in first_page.text
    assert "Your note" in first_page.text
    assert "&lt;script&gt;" in first_page.text
    assert "PRIVATE FULL SOURCE" not in first_page.text
    assert "PRIVATE AI LINE" not in first_page.text
    assert "Short effort" in first_page.text
    assert "v1 estimate" in first_page.text
    assert '<time datetime="2026-09-13">2026-09-13</time>' in first_page.text
    assert "ready now" in first_page.text
    assert "overdue" not in first_page.text.lower()
    assert "Saved 14 days ago" in first_page.text
    assert first_page.headers["cache-control"] == "no-store"
    assert _mount_path(first_page.text) == _mount_path(second_tab.text)

    with card_ui.factory() as database:
        events = list(database.scalars(select(ResurfacingEvent)))
        assert len(events) == 1
        assert events[0].saved_item_id == selected.id
        assert events[0].state is ResurfacingState.PENDING
        assert database.scalar(select(func.count()).select_from(ActionEvent)) == 0
        assert database.get(User, user.id).last_new_impression_local_date is None


def test_card_uses_labeled_excerpt_when_user_note_is_absent(card_ui) -> None:
    user = _login(card_ui)
    with card_ui.factory() as database:
        _create_due_item(
            database,
            user=user,
            suffix="excerpt",
            due_on=LOCAL_TODAY,
            note=None,
            excerpt="The deterministic excerpt is the safe non-AI fallback.",
        )
        database.commit()

    page = card_ui.client.get("/items")

    assert "Source excerpt" in page.text
    assert "The deterministic excerpt is the safe non-AI fallback." in page.text
    assert '<time datetime="2026-09-15">2026-09-15</time>' in page.text
    assert "scheduled for today" in page.text


def test_mount_is_once_only_and_refresh_does_not_show_a_replacement(card_ui) -> None:
    user = _login(card_ui)
    with card_ui.factory() as database:
        selected = _create_due_item(database, user=user, suffix="selected")
        _create_due_item(
            database,
            user=user,
            suffix="still-due",
            due_on=date(2026, 9, 14),
        )
        database.commit()

    first_tab = card_ui.client.get("/items")
    second_tab = card_ui.client.get("/items")
    path = _mount_path(first_tab.text)
    csrf_token = _csrf(first_tab.text)

    first_mount = card_ui.client.post(path, data={"csrf_token": csrf_token})
    repeated_mount = card_ui.client.post(path, data={"csrf_token": csrf_token})
    refreshed = card_ui.client.get("/items")

    assert first_mount.status_code == repeated_mount.status_code == 204
    assert refreshed.text.count("data-resurfacing-card") == 1
    assert "data-resurfacing-mount" not in refreshed.text
    card_markup = re.search(
        r'<article class="resurfacing-card".*?</article>', refreshed.text, re.DOTALL
    ).group(0)
    assert f'/items/{selected.id}' in card_markup
    assert "Useful article selected" in card_markup
    assert "Useful article still-due" not in card_markup
    event_path = path.removesuffix("/impression")
    assert f'action="{event_path}/open"' in card_markup
    assert f'action="{event_path}/later"' in card_markup
    assert f'action="{event_path}/dismiss"' in card_markup
    fieldset = re.search(r'<fieldset\s+class="resurfacing-actions"[^>]*>', card_markup).group(0)
    assert 'aria-busy="false"' in fieldset and "disabled" not in fieldset
    with card_ui.factory() as database:
        event = database.scalar(select(ResurfacingEvent))
        assert event is not None
        assert event.saved_item_id == selected.id
        assert event.state is ResurfacingState.SHOWN
        assert event.shown_at == NOW
        persisted_user = database.get(User, user.id)
        assert persisted_user.last_new_impression_local_date == LOCAL_TODAY
        actions = list(database.scalars(select(ActionEvent)))
        assert len(actions) == 1
        action = actions[0]
        assert action.event_name is ActionEventName.RESURFACING_IMPRESSION
        assert action.actor is ActionActor.CLIENT
        assert action.item_id == selected.id
        assert action.resurfacing_event_id == event.id
        assert action.occurred_at == NOW
        assert action.properties == {
            "scheduled_on": "2026-09-13",
            "shown_local_date": "2026-09-15",
            "timezone": "Asia/Shanghai",
            "kind": "STANDARD",
            "selection_rule_version": "earliest_due_oldest_saved/v1",
            "eligible_count_snapshot": 2,
        }


@pytest.mark.parametrize("condition", ["paused", "closed", "resolved", "dismissed-item"])
def test_refresh_only_restores_active_unresolved_unpaused_card(card_ui, condition) -> None:
    user = _login(card_ui)
    with card_ui.factory() as database:
        _create_due_item(database, user=user, suffix="no-longer-actionable")
        _create_due_item(database, user=user, suffix="replacement-must-not-appear")
        database.commit()
    page = card_ui.client.get("/items")
    mount = card_ui.client.post(_mount_path(page.text), data={"csrf_token": _csrf(page.text)})
    assert mount.status_code == 204
    with card_ui.factory() as database:
        event = database.scalar(select(ResurfacingEvent))
        if condition == "paused":
            database.get(User, user.id).resurfacing_paused_at = NOW
        elif condition == "closed":
            event.state = ResurfacingState.CLOSED
            event.closed_at = NOW
            event.close_reason = ResurfacingCloseReason.PAUSED
        elif condition == "resolved":
            event.state = ResurfacingState.RESOLVED
            event.outcome = ResurfacingOutcome.LATER_CONFIRMED
            event.outcome_at = NOW
            event.closed_at = NOW
        else:
            from app.models import SavedItemStatus

            database.get(SavedItem, event.saved_item_id).status = SavedItemStatus.DISMISSED
        database.commit()

    refreshed = card_ui.client.get("/items")
    assert "data-resurfacing-card" not in refreshed.text
    with card_ui.factory() as database:
        assert database.scalar(select(func.count()).select_from(ResurfacingEvent)) == 1
        assert database.scalar(select(func.count()).select_from(ActionEvent)) == 1


def test_mount_rejects_missing_foreign_and_stale_ids_without_disclosing_difference(card_ui) -> None:
    user = _login(card_ui)
    csrf_token = _csrf(card_ui.client.get("/items/new").text)
    with card_ui.factory() as database:
        own_item = _create_due_item(database, user=user, suffix="stale")
        stale = EventRepository().create_resurfacing_event(
            database,
            user_id=user.id,
            item_id=own_item.id,
            scheduled_on_snapshot=own_item.next_due_on,
            local_date_slot=date(2026, 9, 14),
            timezone_at_selection=user.timezone,
            eligible_count_snapshot=1,
        )
        foreign_user = UserRepository().create(
            database,
            access_code_hash=hash_access_code("foreign-pilot-code-long-enough-456"),
            timezone_name="Asia/Shanghai",
        )
        foreign_item = _create_due_item(database, user=foreign_user, suffix="foreign")
        foreign = EventRepository().create_resurfacing_event(
            database,
            user_id=foreign_user.id,
            item_id=foreign_item.id,
            scheduled_on_snapshot=foreign_item.next_due_on,
            local_date_slot=LOCAL_TODAY,
            timezone_at_selection=foreign_user.timezone,
            eligible_count_snapshot=1,
        )
        stale_id = stale.id
        foreign_id = foreign.id
        database.commit()

    responses = [
        card_ui.client.post(
            f"/resurfacing/{event_id}/impression",
            data={"csrf_token": csrf_token},
        )
        for event_id in ("missing-event", foreign_id, stale_id)
    ]

    assert [response.status_code for response in responses] == [404, 404, 404]
    assert len({response.text for response in responses}) == 1
    with card_ui.factory() as database:
        assert database.get(ResurfacingEvent, stale_id).state is ResurfacingState.PENDING
        assert database.get(ResurfacingEvent, foreign_id).state is ResurfacingState.PENDING
        assert database.scalar(select(func.count()).select_from(ActionEvent)) == 0
        assert database.get(User, user.id).last_new_impression_local_date is None
        assert database.get(User, foreign_user.id).last_new_impression_local_date is None


def test_mount_requires_authenticated_csrf(card_ui) -> None:
    user = _login(card_ui)
    with card_ui.factory() as database:
        _create_due_item(database, user=user, suffix="csrf")
        database.commit()
    path = _mount_path(card_ui.client.get("/items").text)

    response = card_ui.client.post(path, data={})

    assert response.status_code == 403
    with card_ui.factory() as database:
        assert database.scalar(select(ResurfacingEvent)).state is ResurfacingState.PENDING
        assert database.scalar(select(func.count()).select_from(ActionEvent)) == 0


def test_mount_rejects_closed_card_that_was_never_shown(card_ui) -> None:
    user = _login(card_ui)
    with card_ui.factory() as database:
        _create_due_item(database, user=user, suffix="never-shown")
        database.commit()
    page = card_ui.client.get("/items")
    path = _mount_path(page.text)
    csrf_token = _csrf(page.text)
    with card_ui.factory() as database:
        event = database.scalar(select(ResurfacingEvent))
        event.state = ResurfacingState.CLOSED
        event.closed_at = NOW
        event.close_reason = ResurfacingCloseReason.PAUSED
        database.commit()

    response = card_ui.client.post(path, data={"csrf_token": csrf_token})

    assert response.status_code == 404
    with card_ui.factory() as database:
        assert database.scalar(select(func.count()).select_from(ActionEvent)) == 0
        assert database.get(User, user.id).last_new_impression_local_date is None


def test_mount_returns_401_when_session_is_missing_instead_of_redirecting(card_ui) -> None:
    user = _login(card_ui)
    with card_ui.factory() as database:
        _create_due_item(database, user=user, suffix="expired-session")
        database.commit()
    page = card_ui.client.get("/items")
    path = _mount_path(page.text)
    csrf_token = _csrf(page.text)
    card_ui.client.cookies.clear()

    response = card_ui.client.post(
        path,
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 401
    with card_ui.factory() as database:
        assert database.scalar(select(ResurfacingEvent)).state is ResurfacingState.PENDING
        assert database.scalar(select(func.count()).select_from(ActionEvent)) == 0


def test_concurrent_mount_callbacks_create_one_impression(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'concurrent-mount.db'}"
    engine = build_engine(database_url)
    initialize_database(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as setup:
        user = UserRepository().create(
            setup,
            access_code_hash="test-hash",
            timezone_name="Asia/Shanghai",
        )
        item = _create_due_item(setup, user=user, suffix="concurrent")
        reservation = EventRepository().create_resurfacing_event(
            setup,
            user_id=user.id,
            item_id=item.id,
            scheduled_on_snapshot=item.next_due_on,
            local_date_slot=LOCAL_TODAY,
            timezone_at_selection=user.timezone,
            eligible_count_snapshot=1,
        )
        setup.commit()
        user_id = user.id
        event_id = reservation.id

    barrier = Barrier(2)

    def mount(index: int) -> ImpressionMountStatus:
        with factory() as database:
            barrier.wait(timeout=5)
            result = ResurfacingEngine(clock=FrozenClock()).acknowledge_mount(
                database,
                user_id=user_id,
                resurfacing_event_id=event_id,
                session_id=f"browser-session-{index}",
            )
            database.commit()
            return result.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(mount, range(2)))

    with factory() as verify:
        event = verify.get(ResurfacingEvent, event_id)
        assert event.state is ResurfacingState.SHOWN
        assert verify.get(User, user_id).last_new_impression_local_date == LOCAL_TODAY
        assert verify.scalar(select(func.count()).select_from(ActionEvent)) == 1
    engine.dispose()

    assert sorted(statuses) == sorted(
        [ImpressionMountStatus.RECORDED, ImpressionMountStatus.ALREADY_RECORDED]
    )


def test_failed_impression_event_write_rolls_back_state_and_watermark(tmp_path) -> None:
    class FailingRepository(EventRepository):
        def create_action_event(self, session: Session, **kwargs):
            raise RuntimeError("simulated event persistence failure")

    database_url = f"sqlite:///{tmp_path / 'atomic-mount.db'}"
    engine = build_engine(database_url)
    initialize_database(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as setup:
        user = UserRepository().create(
            setup,
            access_code_hash="test-hash",
            timezone_name="Asia/Shanghai",
        )
        item = _create_due_item(setup, user=user, suffix="atomic")
        event = EventRepository().create_resurfacing_event(
            setup,
            user_id=user.id,
            item_id=item.id,
            scheduled_on_snapshot=item.next_due_on,
            local_date_slot=LOCAL_TODAY,
            timezone_at_selection=user.timezone,
            eligible_count_snapshot=1,
        )
        setup.commit()
        user_id = user.id
        event_id = event.id

    with factory() as database:
        with pytest.raises(RuntimeError, match="simulated event persistence failure"):
            ResurfacingEngine(
                repository=FailingRepository(),
                clock=FrozenClock(),
            ).acknowledge_mount(
                database,
                user_id=user_id,
                resurfacing_event_id=event_id,
                session_id="browser-session",
            )
        database.rollback()

    with factory() as verify:
        assert verify.get(ResurfacingEvent, event_id).state is ResurfacingState.PENDING
        assert verify.get(User, user_id).last_new_impression_local_date is None
        assert verify.scalar(select(func.count()).select_from(ActionEvent)) == 0
    engine.dispose()

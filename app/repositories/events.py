"""Minimum persistence helpers for resurfacing and append-only action events."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ActionActor,
    ActionEvent,
    ActionEventName,
    ResurfacingEvent,
    ResurfacingCloseReason,
    ResurfacingKind,
    ResurfacingState,
    SavedItem,
    SavedItemStatus,
    ScheduleKind,
    User,
)


class OwnershipError(ValueError):
    """Raised when a referenced entity does not belong to the acting user."""


class IdempotencyConflict(ValueError):
    """Raised when one idempotency key is reused for different event input."""


@dataclass(frozen=True)
class DailyReservation:
    """The effective daily reservation and whether this call inserted it."""

    event: ResurfacingEvent
    created: bool


class EventRepository:
    def mark_pending_impression_shown(
        self,
        session: Session,
        *,
        user_id: str,
        resurfacing_event_id: str,
        local_date_slot: date,
        shown_at: datetime,
    ) -> ResurfacingEvent | None:
        """Atomically claim one current-day Pending reservation for display.

        The state predicate is part of the write so concurrent browser tabs
        cannot both turn the same reservation into a verified impression.
        """

        result = session.execute(
            update(ResurfacingEvent)
            .where(
                ResurfacingEvent.id == resurfacing_event_id,
                ResurfacingEvent.user_id == user_id,
                ResurfacingEvent.local_date_slot == local_date_slot,
                ResurfacingEvent.state == ResurfacingState.PENDING,
                ResurfacingEvent.shown_at.is_(None),
                exists().where(
                    User.id == user_id,
                    User.resurfacing_paused_at.is_(None),
                ),
            )
            .values(state=ResurfacingState.SHOWN, shown_at=shown_at)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        return session.scalar(
            select(ResurfacingEvent).where(
                ResurfacingEvent.id == resurfacing_event_id,
                ResurfacingEvent.user_id == user_id,
            )
        )

    def get_owned_resurfacing_event(
        self,
        session: Session,
        *,
        user_id: str,
        resurfacing_event_id: str,
    ) -> ResurfacingEvent | None:
        return session.scalar(
            select(ResurfacingEvent).where(
                ResurfacingEvent.id == resurfacing_event_id,
                ResurfacingEvent.user_id == user_id,
            )
        )

    def has_active_cycle_for_item(
        self,
        session: Session,
        *,
        user_id: str,
        item_id: str,
    ) -> bool:
        """Return whether date edits must defer to an active card decision."""

        return (
            session.scalar(
                select(ResurfacingEvent.id)
                .where(
                    ResurfacingEvent.user_id == user_id,
                    ResurfacingEvent.saved_item_id == item_id,
                    ResurfacingEvent.state.in_(
                        (ResurfacingState.PENDING, ResurfacingState.SHOWN)
                    ),
                )
                .limit(1)
            )
            is not None
        )

    def create_resurfacing_event(
        self,
        session: Session,
        *,
        user_id: str,
        item_id: str,
        scheduled_on_snapshot: date,
        local_date_slot: date,
        timezone_at_selection: str,
        eligible_count_snapshot: int,
        kind: ResurfacingKind = ResurfacingKind.STANDARD,
    ) -> ResurfacingEvent:
        self._require_owned_item(session, user_id=user_id, item_id=item_id)
        event = ResurfacingEvent(
            user_id=user_id,
            saved_item_id=item_id,
            kind=kind,
            scheduled_on_snapshot=scheduled_on_snapshot,
            local_date_slot=local_date_slot,
            timezone_at_selection=timezone_at_selection,
            eligible_count_snapshot=eligible_count_snapshot,
        )
        session.add(event)
        session.flush()
        return event

    def close_prior_day_events(
        self,
        session: Session,
        *,
        user_id: str,
        before_local_date: date,
        closed_at: datetime,
    ) -> list[ResurfacingEvent]:
        """Close only unfinished reservations from an earlier user-local day.

        A reservation that never mounted is stale rather than an impression. A
        mounted card without a decision is No Action. Product-event emission is
        intentionally left for the later logging milestone.
        """

        events = list(
            session.scalars(
                select(ResurfacingEvent)
                .where(
                    ResurfacingEvent.user_id == user_id,
                    ResurfacingEvent.local_date_slot < before_local_date,
                    ResurfacingEvent.state.in_(
                        (ResurfacingState.PENDING, ResurfacingState.SHOWN)
                    ),
                    (
                        ResurfacingEvent.open_in_flight_until.is_(None)
                        | (ResurfacingEvent.open_in_flight_until <= closed_at)
                    ),
                )
                .order_by(ResurfacingEvent.local_date_slot, ResurfacingEvent.id)
            )
        )
        for event in events:
            previous_state = event.state
            event.state = ResurfacingState.CLOSED
            event.closed_at = closed_at
            event.close_reason = (
                ResurfacingCloseReason.STALE_PENDING
                if previous_state is ResurfacingState.PENDING
                else ResurfacingCloseReason.NO_ACTION
            )
        if events:
            session.flush()
        return events

    def get_daily_event(
        self,
        session: Session,
        *,
        user_id: str,
        local_date_slot: date,
    ) -> ResurfacingEvent | None:
        return session.scalar(
            select(ResurfacingEvent).where(
                ResurfacingEvent.user_id == user_id,
                ResurfacingEvent.local_date_slot == local_date_slot,
            )
        )

    def count_due_standard_items(
        self,
        session: Session,
        *,
        user_id: str,
        local_today: date,
    ) -> int:
        """Count non-AI V1 candidates without admitting future Recall rows."""

        return int(
            session.scalar(
                select(func.count())
                .select_from(SavedItem)
                .where(*self._due_standard_filters(user_id, local_today))
            )
            or 0
        )

    def first_due_standard_item(
        self,
        session: Session,
        *,
        user_id: str,
        local_today: date,
    ) -> SavedItem | None:
        return session.scalar(
            select(SavedItem)
            .where(*self._due_standard_filters(user_id, local_today))
            .order_by(SavedItem.next_due_on, SavedItem.saved_at, SavedItem.id)
            .limit(1)
        )

    def reserve_daily_event(
        self,
        session: Session,
        *,
        user_id: str,
        item_id: str,
        scheduled_on_snapshot: date,
        local_date_slot: date,
        timezone_at_selection: str,
        eligible_count_snapshot: int,
    ) -> DailyReservation:
        """Insert one daily PENDING slot, or reuse the concurrently inserted row."""

        self._require_owned_item(session, user_id=user_id, item_id=item_id)
        event = ResurfacingEvent(
            user_id=user_id,
            saved_item_id=item_id,
            kind=ResurfacingKind.STANDARD,
            scheduled_on_snapshot=scheduled_on_snapshot,
            local_date_slot=local_date_slot,
            timezone_at_selection=timezone_at_selection,
            eligible_count_snapshot=eligible_count_snapshot,
        )
        try:
            with session.begin_nested():
                session.add(event)
                session.flush()
        except IntegrityError:
            existing = self.get_daily_event(
                session,
                user_id=user_id,
                local_date_slot=local_date_slot,
            )
            if existing is None:
                raise
            return DailyReservation(event=existing, created=False)
        return DailyReservation(event=event, created=True)

    def create_action_event(
        self,
        session: Session,
        *,
        event_name: ActionEventName,
        user_id: str,
        idempotency_key: str,
        actor: ActionActor,
        item_id: str | None = None,
        resurfacing_event_id: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
        occurred_at: datetime | None = None,
        properties: dict[str, Any] | None = None,
    ) -> ActionEvent:
        if item_id is not None:
            self._require_owned_item(session, user_id=user_id, item_id=item_id)
        if resurfacing_event_id is not None:
            owned_event = session.scalar(
                select(ResurfacingEvent).where(
                    ResurfacingEvent.id == resurfacing_event_id,
                    ResurfacingEvent.user_id == user_id,
                )
            )
            if owned_event is None:
                raise OwnershipError("resurfacing event does not belong to user")
            if item_id is not None and owned_event.saved_item_id != item_id:
                raise OwnershipError("resurfacing event does not belong to item")

        values: dict[str, Any] = {
            "event_name": event_name,
            "user_id": user_id,
            "idempotency_key": idempotency_key,
            "actor": actor,
            "item_id": item_id,
            "resurfacing_event_id": resurfacing_event_id,
            "session_id": session_id,
            "properties": properties or {},
        }
        if correlation_id is not None:
            values["correlation_id"] = correlation_id
        if occurred_at is not None:
            values["occurred_at"] = occurred_at
        action = ActionEvent(**values)
        try:
            with session.begin_nested():
                session.add(action)
                session.flush()
        except IntegrityError:
            existing = session.scalar(
                select(ActionEvent).where(
                    ActionEvent.user_id == user_id,
                    ActionEvent.idempotency_key == idempotency_key,
                )
            )
            if existing is None:
                raise
            same_input = (
                existing.event_name == event_name
                and existing.item_id == item_id
                and existing.resurfacing_event_id == resurfacing_event_id
                and existing.actor == actor
                and existing.session_id == session_id
                and existing.properties == (properties or {})
                and (correlation_id is None or existing.correlation_id == correlation_id)
            )
            if not same_input:
                raise IdempotencyConflict("idempotency key was reused for different input")
            return existing
        return action

    @staticmethod
    def _require_owned_item(session: Session, *, user_id: str, item_id: str) -> SavedItem:
        item = session.scalar(
            select(SavedItem).where(
                SavedItem.id == item_id,
                SavedItem.user_id == user_id,
            )
        )
        if item is None:
            raise OwnershipError("saved item does not belong to user")
        return item

    @staticmethod
    def _due_standard_filters(user_id: str, local_today: date) -> tuple[Any, ...]:
        return (
            SavedItem.user_id == user_id,
            SavedItem.status == SavedItemStatus.ACTIVE,
            SavedItem.schedule_kind == ScheduleKind.STANDARD,
            SavedItem.next_due_on.is_not(None),
            SavedItem.next_due_on <= local_today,
        )

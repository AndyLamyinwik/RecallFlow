"""Deterministic, non-AI daily resurfacing selection."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.models import (
    ActionActor,
    ActionEventName,
    ResurfacingEvent,
    ResurfacingState,
    User,
)
from app.repositories import EventRepository
from app.services.clock import Clock, SystemClock


class ResurfacingErrorCode(StrEnum):
    USER_NOT_FOUND = "USER_NOT_FOUND"
    INVALID_TIMEZONE = "INVALID_TIMEZONE"


class ResurfacingRejected(ValueError):
    def __init__(self, code: ResurfacingErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class SelectionStatus(StrEnum):
    RESERVED = "RESERVED"
    REUSED_PENDING = "REUSED_PENDING"
    PAUSED = "PAUSED"
    DAILY_SLOT_USED = "DAILY_SLOT_USED"
    WATERMARK_USED = "WATERMARK_USED"
    NO_ELIGIBLE_ITEM = "NO_ELIGIBLE_ITEM"


class ImpressionMountStatus(StrEnum):
    RECORDED = "RECORDED"
    ALREADY_RECORDED = "ALREADY_RECORDED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ResurfacingSelection:
    """Result of evaluating one user's user-local daily slot."""

    status: SelectionStatus
    local_today: date
    event: ResurfacingEvent | None = None

    @property
    def has_card(self) -> bool:
        return self.event is not None


@dataclass(frozen=True)
class ImpressionMountResult:
    """Outcome of a browser's verified card-mount acknowledgement."""

    status: ImpressionMountStatus
    local_today: date


class ResurfacingEngine:
    """Reserve at most one transparent, deterministic STANDARD card per day."""

    def __init__(
        self,
        *,
        repository: EventRepository | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository or EventRepository()
        self._clock = clock or SystemClock()

    def select_for_today(self, session: Session, *, user_id: str) -> ResurfacingSelection:
        user = session.get(User, user_id)
        if user is None:
            raise ResurfacingRejected(
                ResurfacingErrorCode.USER_NOT_FOUND,
                "The pilot user does not exist",
            )

        now, local_today = self._now_and_local_date(user)

        self._repository.close_prior_day_events(
            session,
            user_id=user.id,
            before_local_date=local_today,
            closed_at=now,
        )

        if user.resurfacing_paused_at is not None:
            return ResurfacingSelection(SelectionStatus.PAUSED, local_today)

        today_event = self._repository.get_daily_event(
            session,
            user_id=user.id,
            local_date_slot=local_today,
        )
        if today_event is not None:
            if today_event.state is ResurfacingState.PENDING:
                return ResurfacingSelection(
                    SelectionStatus.REUSED_PENDING,
                    local_today,
                    today_event,
                )
            return ResurfacingSelection(SelectionStatus.DAILY_SLOT_USED, local_today)

        if user.last_new_impression_local_date == local_today:
            return ResurfacingSelection(SelectionStatus.WATERMARK_USED, local_today)

        eligible_count = self._repository.count_due_standard_items(
            session,
            user_id=user.id,
            local_today=local_today,
        )
        if eligible_count == 0:
            return ResurfacingSelection(SelectionStatus.NO_ELIGIBLE_ITEM, local_today)

        item = self._repository.first_due_standard_item(
            session,
            user_id=user.id,
            local_today=local_today,
        )
        if item is None:
            # A concurrent schedule change may invalidate the count before the
            # selection query. No empty reservation is created.
            return ResurfacingSelection(SelectionStatus.NO_ELIGIBLE_ITEM, local_today)

        reservation = self._repository.reserve_daily_event(
            session,
            user_id=user.id,
            item_id=item.id,
            scheduled_on_snapshot=item.next_due_on,
            local_date_slot=local_today,
            timezone_at_selection=user.timezone,
            eligible_count_snapshot=eligible_count,
        )
        if not reservation.created and reservation.event.state is not ResurfacingState.PENDING:
            return ResurfacingSelection(SelectionStatus.DAILY_SLOT_USED, local_today)
        status = (
            SelectionStatus.RESERVED
            if reservation.created
            else SelectionStatus.REUSED_PENDING
        )
        return ResurfacingSelection(status, local_today, reservation.event)

    def acknowledge_mount(
        self,
        session: Session,
        *,
        user_id: str,
        resurfacing_event_id: str,
        session_id: str,
    ) -> ImpressionMountResult:
        """Record one real browser mount, or safely ignore its replay.

        State, watermark, and the single product event are flushed together and
        committed by the caller as one transaction. Missing, foreign, and stale
        event IDs all share the same rejected outcome.
        """

        user = session.get(User, user_id)
        if user is None:
            raise ResurfacingRejected(
                ResurfacingErrorCode.USER_NOT_FOUND,
                "The pilot user does not exist",
            )
        now, local_today = self._now_and_local_date(user)
        event = self._repository.mark_pending_impression_shown(
            session,
            user_id=user.id,
            resurfacing_event_id=resurfacing_event_id,
            local_date_slot=local_today,
            shown_at=now,
        )
        if event is None:
            existing = self._repository.get_owned_resurfacing_event(
                session,
                user_id=user.id,
                resurfacing_event_id=resurfacing_event_id,
            )
            if (
                existing is not None
                and existing.local_date_slot == local_today
                and existing.state is not ResurfacingState.PENDING
                and existing.shown_at is not None
            ):
                return ImpressionMountResult(
                    ImpressionMountStatus.ALREADY_RECORDED,
                    local_today,
                )
            return ImpressionMountResult(ImpressionMountStatus.REJECTED, local_today)

        if (
            user.last_new_impression_local_date is None
            or user.last_new_impression_local_date < local_today
        ):
            user.last_new_impression_local_date = local_today
        user.updated_at = now
        self._repository.create_action_event(
            session,
            event_name=ActionEventName.RESURFACING_IMPRESSION,
            user_id=user.id,
            idempotency_key=f"resurfacing-impression:{event.id}",
            actor=ActionActor.CLIENT,
            item_id=event.saved_item_id,
            resurfacing_event_id=event.id,
            session_id=session_id,
            occurred_at=now,
            properties={
                "scheduled_on": event.scheduled_on_snapshot.isoformat(),
                "shown_local_date": local_today.isoformat(),
                "timezone": event.timezone_at_selection,
                "kind": event.kind.value,
                "selection_rule_version": event.selection_rule_version,
                "eligible_count_snapshot": event.eligible_count_snapshot,
            },
        )
        session.flush()
        return ImpressionMountResult(ImpressionMountStatus.RECORDED, local_today)

    def _now_and_local_date(self, user: User) -> tuple[datetime, date]:
        now = self._clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        try:
            local_today = now.astimezone(ZoneInfo(user.timezone)).date()
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ResurfacingRejected(
                ResurfacingErrorCode.INVALID_TIMEZONE,
                "The user's timezone is invalid",
            ) from exc
        return now, local_today

"""Deterministic non-AI card actions plus Dismiss Undo and global pause."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
import re
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

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
from app.repositories import EventRepository
from app.services.clock import Clock, SystemClock
from app.services.reachability import (
    ReachabilityErrorCode,
    SafeSourceReachabilityChecker,
    SourceReachability,
    SourceReachabilityError,
)


SessionFactory = Callable[[], Session]
_ACTION_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


class LaterChoice(StrEnum):
    PLUS_3 = "plus_3"
    PLUS_7 = "plus_7"
    CUSTOM = "custom"


class ActionErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    NOT_ACTIONABLE = "not_actionable"
    OPEN_IN_FLIGHT = "open_in_flight"
    INVALID_DATE = "invalid_date"
    INVALID_IDEMPOTENCY_KEY = "invalid_idempotency_key"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INVALID_UNDO = "invalid_undo"
    UNDO_EXPIRED = "undo_expired"


class ActionRejected(ValueError):
    def __init__(self, code: ActionErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class ActionApplyStatus(StrEnum):
    APPLIED = "applied"
    REPLAYED = "replayed"
    UNCHANGED = "unchanged"


class OpenActionStatus(StrEnum):
    OPENED = "opened"
    FAILED = "failed"
    REPLAYED_OPENED = "replayed_opened"
    REPLAYED_FAILED = "replayed_failed"


@dataclass(frozen=True)
class LaterActionResult:
    status: ActionApplyStatus
    next_due_on: date


@dataclass(frozen=True)
class DismissActionResult:
    status: ActionApplyStatus
    item_id: str
    dismiss_action_event_id: str
    original_session_id: str
    local_date_slot: date
    undo_expires_at: datetime


@dataclass(frozen=True)
class DismissUndoResult:
    status: ActionApplyStatus
    event_state: ResurfacingState


@dataclass(frozen=True)
class PauseActionResult:
    status: ActionApplyStatus
    had_active_card: bool


@dataclass(frozen=True)
class ResumeActionResult:
    status: ActionApplyStatus
    paused_duration_seconds: int


@dataclass(frozen=True)
class OpenActionResult:
    status: OpenActionStatus
    destination_url: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class _OpenReservation:
    user_id: str
    event_id: str
    item_id: str
    source_url: str
    lease_until: datetime
    attempt_key: str
    result_key: str
    correlation_id: str
    open_attempt_id: str


class ResurfacingActionService:
    """Apply the first legal choice and replay identical client retries."""

    def __init__(
        self,
        *,
        repository: EventRepository | None = None,
        checker: SafeSourceReachabilityChecker | None = None,
        clock: Clock | None = None,
        open_lease_seconds: int = 15,
        dismiss_undo_window_seconds: int = 30,
    ) -> None:
        self._repository = repository or EventRepository()
        self._checker = checker or SafeSourceReachabilityChecker()
        self._clock = clock or SystemClock()
        self._open_lease_seconds = open_lease_seconds
        self._dismiss_undo_window_seconds = dismiss_undo_window_seconds

    def later(
        self,
        session: Session,
        *,
        user_id: str,
        resurfacing_event_id: str,
        session_id: str,
        idempotency_key: str,
        choice: LaterChoice | str,
        custom_date: date | None = None,
    ) -> LaterActionResult:
        client_key = self._validate_client_key(idempotency_key)
        selected_choice = self._parse_later_choice(choice)
        action_key = self._derived_key(
            user_id, resurfacing_event_id, client_key, "later-confirmed"
        )
        existing = self._action_by_key(session, user_id=user_id, key=action_key)
        if existing is not None:
            stored_due_on = self._stored_later_date(existing)
            if (
                existing.event_name is ActionEventName.LATER_CONFIRMED
                and existing.resurfacing_event_id == resurfacing_event_id
                and existing.properties.get("choice") == selected_choice.value
                and (
                    selected_choice is not LaterChoice.CUSTOM
                    or custom_date == stored_due_on
                )
            ):
                return LaterActionResult(ActionApplyStatus.REPLAYED, stored_due_on)
            raise ActionRejected(
                ActionErrorCode.IDEMPOTENCY_CONFLICT,
                "This action key was already used for different input",
            )

        now, local_today, timezone_name = self._user_time(
            session, user_id=user_id
        )
        new_due_on = self._later_date(
            selected_choice,
            local_today=local_today,
            custom_date=custom_date,
        )

        try:
            event, item = self._shown_event_and_item(
                session,
                user_id=user_id,
                resurfacing_event_id=resurfacing_event_id,
                now=now,
            )
        except ActionRejected as exc:
            if exc.code is not ActionErrorCode.NOT_ACTIONABLE:
                raise
            session.rollback()
            return self._replay_later_or_reject(
                session,
                user_id=user_id,
                key=action_key,
                event_id=resurfacing_event_id,
                choice=selected_choice,
                new_due_on=new_due_on,
            )
        previous_due_on = item.next_due_on
        claimed = session.execute(
            update(ResurfacingEvent)
            .where(
                ResurfacingEvent.id == event.id,
                ResurfacingEvent.user_id == user_id,
                ResurfacingEvent.state == ResurfacingState.SHOWN,
                exists().where(
                    User.id == user_id,
                    User.resurfacing_paused_at.is_(None),
                ),
                (
                    ResurfacingEvent.open_in_flight_until.is_(None)
                    | (ResurfacingEvent.open_in_flight_until <= now)
                ),
            )
            .values(
                state=ResurfacingState.RESOLVED,
                outcome=ResurfacingOutcome.LATER_CONFIRMED,
                outcome_at=now,
                closed_at=now,
                close_reason=None,
                open_in_flight_until=None,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            # SQLite may keep the pre-lock read snapshot after a competing
            # identical request commits. End that read transaction before
            # looking up the winner's idempotent result.
            session.rollback()
            return self._replay_later_or_reject(
                session,
                user_id=user_id,
                key=action_key,
                event_id=resurfacing_event_id,
                choice=selected_choice,
                new_due_on=new_due_on,
            )
        item_update = session.execute(
            update(SavedItem)
            .where(
                SavedItem.id == item.id,
                SavedItem.user_id == user_id,
                SavedItem.status == SavedItemStatus.ACTIVE,
            )
            .values(
                schedule_kind=ScheduleKind.STANDARD,
                next_due_on=new_due_on,
                schedule_timezone=timezone_name,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if item_update.rowcount != 1:
            raise ActionRejected(
                ActionErrorCode.NOT_ACTIONABLE,
                "This resurfacing card has already been handled",
            )
        self._repository.create_action_event(
            session,
            event_name=ActionEventName.LATER_CONFIRMED,
            user_id=user_id,
            idempotency_key=action_key,
            actor=ActionActor.USER,
            item_id=item.id,
            resurfacing_event_id=event.id,
            session_id=session_id,
            occurred_at=now,
            properties={
                "previous_due_on": (
                    previous_due_on.isoformat() if previous_due_on else None
                ),
                "new_due_on": new_due_on.isoformat(),
                "choice": selected_choice.value,
                "resulting_schedule_kind": ScheduleKind.STANDARD.value,
            },
        )
        session.flush()
        return LaterActionResult(ActionApplyStatus.APPLIED, new_due_on)

    def dismiss(
        self,
        session: Session,
        *,
        user_id: str,
        resurfacing_event_id: str,
        session_id: str,
        idempotency_key: str,
    ) -> DismissActionResult:
        client_key = self._validate_client_key(idempotency_key)
        action_key = self._derived_key(
            user_id, resurfacing_event_id, client_key, "dismissed"
        )
        existing = self._action_by_key(session, user_id=user_id, key=action_key)
        if existing is not None:
            if (
                existing.event_name is ActionEventName.DISMISSED
                and existing.resurfacing_event_id == resurfacing_event_id
            ):
                return self._dismiss_result_from_action(
                    session,
                    user_id=user_id,
                    event_id=resurfacing_event_id,
                    action=existing,
                    status=ActionApplyStatus.REPLAYED,
                )
            raise ActionRejected(
                ActionErrorCode.IDEMPOTENCY_CONFLICT,
                "This action key was already used for different input",
            )

        now, _, _ = self._user_time(session, user_id=user_id)
        try:
            event, item = self._shown_event_and_item(
                session,
                user_id=user_id,
                resurfacing_event_id=resurfacing_event_id,
                now=now,
            )
        except ActionRejected as exc:
            if exc.code is not ActionErrorCode.NOT_ACTIONABLE:
                raise
            session.rollback()
            return self._replay_dismiss_or_reject(
                session,
                user_id=user_id,
                key=action_key,
                event_id=resurfacing_event_id,
            )
        previous_due_on = item.next_due_on
        previous_schedule_kind = item.schedule_kind
        undo_expires_at = now + timedelta(
            seconds=self._dismiss_undo_window_seconds
        )
        claimed = session.execute(
            update(ResurfacingEvent)
            .where(
                ResurfacingEvent.id == event.id,
                ResurfacingEvent.user_id == user_id,
                ResurfacingEvent.state == ResurfacingState.SHOWN,
                exists().where(
                    User.id == user_id,
                    User.resurfacing_paused_at.is_(None),
                ),
                (
                    ResurfacingEvent.open_in_flight_until.is_(None)
                    | (ResurfacingEvent.open_in_flight_until <= now)
                ),
            )
            .values(
                state=ResurfacingState.RESOLVED,
                outcome=ResurfacingOutcome.DISMISSED,
                outcome_at=now,
                closed_at=now,
                close_reason=None,
                open_in_flight_until=None,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            # Refresh a potentially stale SQLite snapshot after a concurrent
            # request won the state compare-and-set.
            session.rollback()
            return self._replay_dismiss_or_reject(
                session,
                user_id=user_id,
                key=action_key,
                event_id=resurfacing_event_id,
            )
        item_update = session.execute(
            update(SavedItem)
            .where(
                SavedItem.id == item.id,
                SavedItem.user_id == user_id,
                SavedItem.status == SavedItemStatus.ACTIVE,
            )
            .values(status=SavedItemStatus.DISMISSED, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if item_update.rowcount != 1:
            raise ActionRejected(
                ActionErrorCode.NOT_ACTIONABLE,
                "This resurfacing card has already been handled",
            )
        action = self._repository.create_action_event(
            session,
            event_name=ActionEventName.DISMISSED,
            user_id=user_id,
            idempotency_key=action_key,
            actor=ActionActor.USER,
            item_id=item.id,
            resurfacing_event_id=event.id,
            session_id=session_id,
            occurred_at=now,
            properties={
                "previous_due_on": (
                    previous_due_on.isoformat() if previous_due_on else None
                ),
                "previous_schedule_kind": previous_schedule_kind.value,
                "undo_expires_at": undo_expires_at.isoformat(),
            },
        )
        session.flush()
        return DismissActionResult(
            status=ActionApplyStatus.APPLIED,
            item_id=item.id,
            dismiss_action_event_id=action.id,
            original_session_id=session_id,
            local_date_slot=event.local_date_slot,
            undo_expires_at=undo_expires_at,
        )

    def dismiss_undo_available(
        self,
        session: Session,
        *,
        user_id: str,
        resurfacing_event_id: str,
        item_id: str,
        dismiss_action_event_id: str,
        original_session_id: str,
        dismissal_local_date: date,
        undo_expires_at: datetime,
    ) -> bool:
        """Return whether a signed Dismiss capability still names the live dismissal."""

        now = self._utc_now()
        if now >= self._as_utc(undo_expires_at):
            return False
        try:
            self._validated_dismissal_reference(
                session,
                user_id=user_id,
                event_id=resurfacing_event_id,
                item_id=item_id,
                dismiss_action_event_id=dismiss_action_event_id,
                original_session_id=original_session_id,
                dismissal_local_date=dismissal_local_date,
                undo_expires_at=undo_expires_at,
            )
        except ActionRejected:
            return False
        return True

    def undo_dismiss(
        self,
        session: Session,
        *,
        user_id: str,
        resurfacing_event_id: str,
        item_id: str,
        dismiss_action_event_id: str,
        original_session_id: str,
        dismissal_local_date: date,
        undo_expires_at: datetime,
        session_id: str,
        idempotency_key: str,
    ) -> DismissUndoResult:
        """Undo exactly one still-current Dismiss without changing its schedule."""

        client_key = self._validate_client_key(idempotency_key)
        action_key = self._derived_key(
            user_id,
            dismiss_action_event_id,
            client_key,
            "dismiss-undone",
        )
        existing = self._action_by_key(session, user_id=user_id, key=action_key)
        if existing is not None:
            return self._undo_replay(
                session,
                user_id=user_id,
                event_id=resurfacing_event_id,
                item_id=item_id,
                dismiss_action_event_id=dismiss_action_event_id,
                action=existing,
            )

        now = self._utc_now()
        if now >= self._as_utc(undo_expires_at):
            raise ActionRejected(
                ActionErrorCode.UNDO_EXPIRED,
                "The Dismiss undo window has expired",
            )

        # Pause and Undo both lock the user row first. This gives their event
        # transitions one serialization order and prevents a paused account
        # from being left with a visible SHOWN card.
        self._lock_user(session, user_id=user_id)
        existing = self._action_by_key(session, user_id=user_id, key=action_key)
        if existing is not None:
            return self._undo_replay(
                session,
                user_id=user_id,
                event_id=resurfacing_event_id,
                item_id=item_id,
                dismiss_action_event_id=dismiss_action_event_id,
                action=existing,
            )
        now, local_today, _ = self._user_time(session, user_id=user_id)
        if now >= self._as_utc(undo_expires_at):
            raise ActionRejected(
                ActionErrorCode.UNDO_EXPIRED,
                "The Dismiss undo window has expired",
            )
        dismissal, event, item = self._validated_dismissal_reference(
            session,
            user_id=user_id,
            event_id=resurfacing_event_id,
            item_id=item_id,
            dismiss_action_event_id=dismiss_action_event_id,
            original_session_id=original_session_id,
            dismissal_local_date=dismissal_local_date,
            undo_expires_at=undo_expires_at,
        )
        prior_undo = self._undo_for_dismissal(
            session,
            user_id=user_id,
            event_id=event.id,
            dismiss_action_event_id=dismissal.id,
        )
        if prior_undo is not None:
            return self._undo_replay(
                session,
                user_id=user_id,
                event_id=event.id,
                item_id=item.id,
                dismiss_action_event_id=dismissal.id,
                action=prior_undo,
            )

        user = session.get(User, user_id)
        if user is None:
            raise ActionRejected(ActionErrorCode.NOT_FOUND, "Card not found")
        restore_card = (
            original_session_id == session_id
            and event.local_date_slot == local_today
            and user.resurfacing_paused_at is None
        )
        event_values: dict[str, object | None]
        if restore_card:
            event_values = {
                "state": ResurfacingState.SHOWN,
                "outcome": None,
                "outcome_at": None,
                "closed_at": None,
                "close_reason": None,
                "open_in_flight_until": None,
            }
        else:
            event_values = {
                "state": ResurfacingState.CLOSED,
                "outcome": None,
                "outcome_at": None,
                "closed_at": now,
                "close_reason": ResurfacingCloseReason.DISMISS_UNDONE,
                "open_in_flight_until": None,
            }
        event_update = session.execute(
            update(ResurfacingEvent)
            .where(
                ResurfacingEvent.id == event.id,
                ResurfacingEvent.user_id == user_id,
                ResurfacingEvent.saved_item_id == item.id,
                ResurfacingEvent.state == ResurfacingState.RESOLVED,
                ResurfacingEvent.outcome == ResurfacingOutcome.DISMISSED,
            )
            .values(**event_values)
            .execution_options(synchronize_session=False)
        )
        if event_update.rowcount != 1:
            session.rollback()
            prior_undo = self._undo_for_dismissal(
                session,
                user_id=user_id,
                event_id=resurfacing_event_id,
                dismiss_action_event_id=dismiss_action_event_id,
            )
            if prior_undo is not None:
                return self._undo_replay(
                    session,
                    user_id=user_id,
                    event_id=resurfacing_event_id,
                    item_id=item_id,
                    dismiss_action_event_id=dismiss_action_event_id,
                    action=prior_undo,
                )
            raise ActionRejected(
                ActionErrorCode.INVALID_UNDO,
                "This Dismiss can no longer be undone",
            )
        item_update = session.execute(
            update(SavedItem)
            .where(
                SavedItem.id == item.id,
                SavedItem.user_id == user_id,
                SavedItem.status == SavedItemStatus.DISMISSED,
            )
            .values(status=SavedItemStatus.ACTIVE, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if item_update.rowcount != 1:
            raise ActionRejected(
                ActionErrorCode.INVALID_UNDO,
                "This Dismiss can no longer be undone",
            )
        self._repository.create_action_event(
            session,
            event_name=ActionEventName.DISMISS_UNDONE,
            user_id=user_id,
            idempotency_key=action_key,
            actor=ActionActor.USER,
            item_id=item.id,
            resurfacing_event_id=event.id,
            session_id=session_id,
            occurred_at=now,
            properties={"dismiss_action_event_id": dismissal.id},
        )
        session.flush()
        return DismissUndoResult(
            status=ActionApplyStatus.APPLIED,
            event_state=(
                ResurfacingState.SHOWN if restore_card else ResurfacingState.CLOSED
            ),
        )

    def pause(
        self,
        session: Session,
        *,
        user_id: str,
        session_id: str,
        idempotency_key: str,
    ) -> PauseActionResult:
        """Pause all resurfacing while preserving every item's schedule."""

        client_key = self._validate_client_key(idempotency_key)
        action_key = self._derived_key(
            user_id, "global-resurfacing", client_key, "pause-enabled"
        )
        existing = self._action_by_key(session, user_id=user_id, key=action_key)
        if existing is not None:
            return self._pause_replay(existing)
        now = self._utc_now()
        claimed = session.execute(
            update(User)
            .where(
                User.id == user_id,
                User.resurfacing_paused_at.is_(None),
            )
            .values(resurfacing_paused_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            session.rollback()
            existing = self._action_by_key(
                session, user_id=user_id, key=action_key
            )
            if existing is not None:
                return self._pause_replay(existing)
            user = session.get(User, user_id)
            if user is None:
                raise ActionRejected(ActionErrorCode.NOT_FOUND, "User not found")
            if user.resurfacing_paused_at is not None:
                return PauseActionResult(ActionApplyStatus.UNCHANGED, False)
            # A concurrent Resume may have made the user active after our
            # failed compare-and-set. Retry against the fresh transaction.
            return self.pause(
                session,
                user_id=user_id,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )
        closed = session.execute(
            update(ResurfacingEvent)
            .where(
                ResurfacingEvent.user_id == user_id,
                ResurfacingEvent.state.in_(
                    (ResurfacingState.PENDING, ResurfacingState.SHOWN)
                ),
            )
            .values(
                state=ResurfacingState.CLOSED,
                outcome=None,
                outcome_at=None,
                closed_at=now,
                close_reason=ResurfacingCloseReason.PAUSED,
                open_in_flight_until=None,
            )
            .execution_options(synchronize_session=False)
        )
        had_active_card = closed.rowcount > 0
        self._repository.create_action_event(
            session,
            event_name=ActionEventName.PAUSE_ENABLED,
            user_id=user_id,
            idempotency_key=action_key,
            actor=ActionActor.USER,
            session_id=session_id,
            occurred_at=now,
            properties={"had_active_card": had_active_card},
        )
        session.flush()
        return PauseActionResult(ActionApplyStatus.APPLIED, had_active_card)

    def resume(
        self,
        session: Session,
        *,
        user_id: str,
        session_id: str,
        idempotency_key: str,
    ) -> ResumeActionResult:
        """Resume selection without rewriting dates, events, or daily slots."""

        client_key = self._validate_client_key(idempotency_key)
        action_key = self._derived_key(
            user_id, "global-resurfacing", client_key, "pause-disabled"
        )
        existing = self._action_by_key(session, user_id=user_id, key=action_key)
        if existing is not None:
            return self._resume_replay(existing)
        now = self._utc_now()
        user = session.get(User, user_id)
        if user is None:
            raise ActionRejected(ActionErrorCode.NOT_FOUND, "User not found")
        paused_at = user.resurfacing_paused_at
        if paused_at is None:
            return ResumeActionResult(ActionApplyStatus.UNCHANGED, 0)
        paused_at = self._as_utc(paused_at)
        duration = max(0, int((now - paused_at).total_seconds()))
        claimed = session.execute(
            update(User)
            .where(
                User.id == user_id,
                User.resurfacing_paused_at == paused_at,
            )
            .values(resurfacing_paused_at=None, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            session.rollback()
            existing = self._action_by_key(
                session, user_id=user_id, key=action_key
            )
            if existing is not None:
                return self._resume_replay(existing)
            user = session.get(User, user_id)
            if user is None:
                raise ActionRejected(ActionErrorCode.NOT_FOUND, "User not found")
            if user.resurfacing_paused_at is None:
                return ResumeActionResult(ActionApplyStatus.UNCHANGED, 0)
            return self.resume(
                session,
                user_id=user_id,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )
        self._repository.create_action_event(
            session,
            event_name=ActionEventName.PAUSE_DISABLED,
            user_id=user_id,
            idempotency_key=action_key,
            actor=ActionActor.USER,
            session_id=session_id,
            occurred_at=now,
            properties={"paused_duration_seconds": duration},
        )
        session.flush()
        return ResumeActionResult(ActionApplyStatus.APPLIED, duration)

    def open_source(
        self,
        session_factory: SessionFactory,
        *,
        user_id: str,
        resurfacing_event_id: str,
        session_id: str,
        idempotency_key: str,
    ) -> OpenActionResult:
        """Check reachability between two transactions, then issue a handoff result.

        `source_opened` means only safe reachability plus an issued navigation
        handoff. It must never be interpreted or presented as article reading.
        """

        client_key = self._validate_client_key(idempotency_key)
        with session_factory() as session:
            started = self._begin_open(
                session,
                user_id=user_id,
                resurfacing_event_id=resurfacing_event_id,
                session_id=session_id,
                client_key=client_key,
            )
            if isinstance(started, OpenActionResult):
                return started
            session.commit()

        try:
            reachability = self._checker.check(started.source_url)
        except SourceReachabilityError as exc:
            with session_factory() as session:
                result = self._complete_open_failure(
                    session,
                    reservation=started,
                    session_id=session_id,
                    failure=exc,
                )
                session.commit()
            return result

        with session_factory() as session:
            result = self._complete_open_success(
                session,
                reservation=started,
                session_id=session_id,
                reachability=reachability,
            )
            session.commit()
        return result

    def _begin_open(
        self,
        session: Session,
        *,
        user_id: str,
        resurfacing_event_id: str,
        session_id: str,
        client_key: str,
    ) -> _OpenReservation | OpenActionResult:
        attempt_key = self._derived_key(
            user_id, resurfacing_event_id, client_key, "open-attempt"
        )
        result_key = self._derived_key(
            user_id, resurfacing_event_id, client_key, "open-result"
        )
        correlation_id = str(
            uuid5(
                NAMESPACE_URL,
                f"recallflow:{user_id}:{resurfacing_event_id}:{client_key}:open",
            )
        )
        open_attempt_id = str(
            uuid5(
                NAMESPACE_URL,
                f"recallflow:{user_id}:{resurfacing_event_id}:{client_key}:attempt-id",
            )
        )
        prior_result = self._action_by_key(
            session, user_id=user_id, key=result_key
        )
        if prior_result is not None:
            return self._open_replay(
                session,
                user_id=user_id,
                event_id=resurfacing_event_id,
                result=prior_result,
            )

        now, _, _ = self._user_time(session, user_id=user_id)
        event = self._repository.get_owned_resurfacing_event(
            session,
            user_id=user_id,
            resurfacing_event_id=resurfacing_event_id,
        )
        if event is None:
            raise ActionRejected(ActionErrorCode.NOT_FOUND, "Card not found")
        item = session.scalar(
            select(SavedItem).where(
                SavedItem.id == event.saved_item_id,
                SavedItem.user_id == user_id,
            )
        )
        if item is None:
            raise ActionRejected(ActionErrorCode.NOT_FOUND, "Card not found")
        if event.state is not ResurfacingState.SHOWN or item.status is not SavedItemStatus.ACTIVE:
            raise ActionRejected(
                ActionErrorCode.NOT_ACTIONABLE,
                "This resurfacing card has already been handled",
            )

        prior_attempt = self._action_by_key(
            session, user_id=user_id, key=attempt_key
        )
        if event.open_in_flight_until is not None and event.open_in_flight_until > now:
            raise ActionRejected(
                ActionErrorCode.OPEN_IN_FLIGHT,
                "The source check is already in progress",
            )
        lease_until = now + timedelta(seconds=self._open_lease_seconds)
        claimed = session.execute(
            update(ResurfacingEvent)
            .where(
                ResurfacingEvent.id == event.id,
                ResurfacingEvent.user_id == user_id,
                ResurfacingEvent.state == ResurfacingState.SHOWN,
                exists().where(
                    User.id == user_id,
                    User.resurfacing_paused_at.is_(None),
                ),
                (
                    ResurfacingEvent.open_in_flight_until.is_(None)
                    | (ResurfacingEvent.open_in_flight_until <= now)
                ),
            )
            .values(open_in_flight_until=lease_until)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise ActionRejected(
                ActionErrorCode.OPEN_IN_FLIGHT,
                "The source check is already in progress",
            )
        if prior_attempt is None:
            self._repository.create_action_event(
                session,
                event_name=ActionEventName.OPEN_ATTEMPT,
                user_id=user_id,
                idempotency_key=attempt_key,
                actor=ActionActor.USER,
                item_id=item.id,
                resurfacing_event_id=event.id,
                session_id=session_id,
                correlation_id=correlation_id,
                occurred_at=now,
                properties={
                    "open_attempt_id": open_attempt_id,
                    "surface": "resurfacing_card",
                },
            )
        session.flush()
        return _OpenReservation(
            user_id=user_id,
            event_id=event.id,
            item_id=item.id,
            source_url=item.source_url,
            lease_until=lease_until,
            attempt_key=attempt_key,
            result_key=result_key,
            correlation_id=correlation_id,
            open_attempt_id=open_attempt_id,
        )

    def _complete_open_success(
        self,
        session: Session,
        *,
        reservation: _OpenReservation,
        session_id: str,
        reachability: SourceReachability,
    ) -> OpenActionResult:
        existing = self._action_by_key(
            session, user_id=reservation.user_id, key=reservation.result_key
        )
        if existing is not None:
            return self._open_replay(
                session,
                user_id=reservation.user_id,
                event_id=reservation.event_id,
                result=existing,
            )
        now = self._utc_now()
        claimed = session.execute(
            update(ResurfacingEvent)
            .where(
                ResurfacingEvent.id == reservation.event_id,
                ResurfacingEvent.user_id == reservation.user_id,
                ResurfacingEvent.state == ResurfacingState.SHOWN,
                exists().where(
                    User.id == reservation.user_id,
                    User.resurfacing_paused_at.is_(None),
                ),
                ResurfacingEvent.open_in_flight_until == reservation.lease_until,
            )
            .values(
                state=ResurfacingState.RESOLVED,
                outcome=ResurfacingOutcome.OPENED,
                outcome_at=now,
                closed_at=now,
                close_reason=None,
                open_in_flight_until=None,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            return self._complete_open_failure(
                session,
                reservation=reservation,
                session_id=session_id,
                failure=SourceReachabilityError(
                    ReachabilityErrorCode.STALE_STATE,
                    "The card was handled before the source check completed",
                    retryable=False,
                ),
            )
        item_update = session.execute(
            update(SavedItem)
            .where(
                SavedItem.id == reservation.item_id,
                SavedItem.user_id == reservation.user_id,
                SavedItem.status == SavedItemStatus.ACTIVE,
            )
            .values(
                schedule_kind=ScheduleKind.NONE,
                next_due_on=None,
                schedule_timezone=None,
                last_source_opened_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if item_update.rowcount != 1:
            raise ActionRejected(
                ActionErrorCode.NOT_ACTIONABLE,
                "This resurfacing card has already been handled",
            )
        self._repository.create_action_event(
            session,
            event_name=ActionEventName.SOURCE_OPENED,
            user_id=reservation.user_id,
            idempotency_key=reservation.result_key,
            actor=ActionActor.SYSTEM,
            item_id=reservation.item_id,
            resurfacing_event_id=reservation.event_id,
            session_id=session_id,
            correlation_id=reservation.correlation_id,
            occurred_at=now,
            properties={
                "open_attempt_id": reservation.open_attempt_id,
                "reachability_method": reachability.method,
                "status_class": reachability.status_class,
                "handoff": "external_redirect",
            },
        )
        session.flush()
        return OpenActionResult(
            OpenActionStatus.OPENED,
            # Return the original saved address after the complete redirect
            # chain has passed the safe check. This keeps an idempotent replay
            # identical without persisting a second full URL in analytics.
            destination_url=reservation.source_url,
        )

    def _complete_open_failure(
        self,
        session: Session,
        *,
        reservation: _OpenReservation,
        session_id: str,
        failure: SourceReachabilityError,
    ) -> OpenActionResult:
        existing = self._action_by_key(
            session, user_id=reservation.user_id, key=reservation.result_key
        )
        if existing is not None:
            return self._open_replay(
                session,
                user_id=reservation.user_id,
                event_id=reservation.event_id,
                result=existing,
            )
        now = self._utc_now()
        event = session.scalar(
            select(ResurfacingEvent).where(
                ResurfacingEvent.id == reservation.event_id,
                ResurfacingEvent.user_id == reservation.user_id,
            )
        )
        item = session.scalar(
            select(SavedItem).where(
                SavedItem.id == reservation.item_id,
                SavedItem.user_id == reservation.user_id,
            )
        )
        if event is None or item is None:
            # Hard deletion intentionally cascades action history, so there is
            # no private row on which a result can or should be retained.
            return OpenActionResult(
                OpenActionStatus.FAILED,
                error_type=ReachabilityErrorCode.STALE_STATE.value,
            )
        session.execute(
            update(ResurfacingEvent)
            .where(
                ResurfacingEvent.id == reservation.event_id,
                ResurfacingEvent.user_id == reservation.user_id,
                ResurfacingEvent.state == ResurfacingState.SHOWN,
                ResurfacingEvent.open_in_flight_until == reservation.lease_until,
            )
            .values(open_in_flight_until=None)
            .execution_options(synchronize_session=False)
        )
        properties: dict[str, object] = {
            "open_attempt_id": reservation.open_attempt_id,
            "error_type": failure.code.value,
            "retryable": failure.retryable,
        }
        if failure.status_code is not None:
            properties["status_class"] = f"{failure.status_code // 100}xx"
        self._repository.create_action_event(
            session,
            event_name=ActionEventName.OPEN_FAILED,
            user_id=reservation.user_id,
            idempotency_key=reservation.result_key,
            actor=ActionActor.SYSTEM,
            item_id=reservation.item_id,
            resurfacing_event_id=reservation.event_id,
            session_id=session_id,
            correlation_id=reservation.correlation_id,
            occurred_at=now,
            properties=properties,
        )
        session.flush()
        return OpenActionResult(
            OpenActionStatus.FAILED,
            error_type=failure.code.value,
        )

    def _shown_event_and_item(
        self,
        session: Session,
        *,
        user_id: str,
        resurfacing_event_id: str,
        now: datetime,
    ) -> tuple[ResurfacingEvent, SavedItem]:
        event = self._repository.get_owned_resurfacing_event(
            session,
            user_id=user_id,
            resurfacing_event_id=resurfacing_event_id,
        )
        if event is None:
            raise ActionRejected(ActionErrorCode.NOT_FOUND, "Card not found")
        item = session.scalar(
            select(SavedItem).where(
                SavedItem.id == event.saved_item_id,
                SavedItem.user_id == user_id,
            )
        )
        if item is None:
            raise ActionRejected(ActionErrorCode.NOT_FOUND, "Card not found")
        user = session.get(User, user_id)
        if user is None:
            raise ActionRejected(ActionErrorCode.NOT_FOUND, "Card not found")
        if user.resurfacing_paused_at is not None:
            raise ActionRejected(
                ActionErrorCode.NOT_ACTIONABLE,
                "Resurfacing is paused",
            )
        if event.state is not ResurfacingState.SHOWN or item.status is not SavedItemStatus.ACTIVE:
            raise ActionRejected(
                ActionErrorCode.NOT_ACTIONABLE,
                "This resurfacing card has already been handled",
            )
        if event.open_in_flight_until is not None and event.open_in_flight_until > now:
            raise ActionRejected(
                ActionErrorCode.OPEN_IN_FLIGHT,
                "A source check is still in progress",
            )
        return event, item

    def _user_time(
        self, session: Session, *, user_id: str
    ) -> tuple[datetime, date, str]:
        user = session.get(User, user_id)
        if user is None:
            raise ActionRejected(ActionErrorCode.NOT_FOUND, "Card not found")
        now = self._utc_now()
        try:
            local_today = now.astimezone(ZoneInfo(user.timezone)).date()
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ActionRejected(
                ActionErrorCode.NOT_ACTIONABLE,
                "The pilot timezone is invalid",
            ) from exc
        return now, local_today, user.timezone

    def _utc_now(self) -> datetime:
        now = self._clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    @staticmethod
    def _validate_client_key(value: str) -> str:
        candidate = value.strip()
        if not _ACTION_KEY_PATTERN.fullmatch(candidate):
            raise ActionRejected(
                ActionErrorCode.INVALID_IDEMPOTENCY_KEY,
                "A valid action key is required",
            )
        return candidate

    @staticmethod
    def _parse_later_choice(value: LaterChoice | str) -> LaterChoice:
        try:
            return LaterChoice(value)
        except ValueError as exc:
            raise ActionRejected(
                ActionErrorCode.INVALID_DATE,
                "Choose a valid Later option",
            ) from exc

    @staticmethod
    def _later_date(
        choice: LaterChoice,
        *,
        local_today: date,
        custom_date: date | None,
    ) -> date:
        if choice is LaterChoice.PLUS_3:
            return local_today + timedelta(days=3)
        if choice is LaterChoice.PLUS_7:
            return local_today + timedelta(days=7)
        if custom_date is None or custom_date <= local_today:
            raise ActionRejected(
                ActionErrorCode.INVALID_DATE,
                "Choose a date after today",
            )
        return custom_date

    @staticmethod
    def _derived_key(
        user_id: str,
        event_id: str,
        client_key: str,
        namespace: str,
    ) -> str:
        derived = uuid5(
            NAMESPACE_URL,
            f"recallflow:{user_id}:{event_id}:{client_key}:{namespace}",
        )
        return f"{namespace}:{derived}"

    @staticmethod
    def _action_by_key(
        session: Session, *, user_id: str, key: str
    ) -> ActionEvent | None:
        return session.scalar(
            select(ActionEvent).where(
                ActionEvent.user_id == user_id,
                ActionEvent.idempotency_key == key,
            )
        )

    @staticmethod
    def _stored_later_date(action: ActionEvent) -> date:
        raw_value = action.properties.get("new_due_on")
        try:
            if not isinstance(raw_value, str):
                raise ValueError
            return date.fromisoformat(raw_value)
        except ValueError as exc:
            raise ActionRejected(
                ActionErrorCode.IDEMPOTENCY_CONFLICT,
                "The stored action result is invalid",
            ) from exc

    def _replay_later_or_reject(
        self,
        session: Session,
        *,
        user_id: str,
        key: str,
        event_id: str,
        choice: LaterChoice,
        new_due_on: date,
    ) -> LaterActionResult:
        existing = self._action_by_key(session, user_id=user_id, key=key)
        if (
            existing is not None
            and existing.event_name is ActionEventName.LATER_CONFIRMED
            and existing.resurfacing_event_id == event_id
            and existing.properties.get("choice") == choice.value
        ):
            stored_due_on = self._stored_later_date(existing)
            if choice is not LaterChoice.CUSTOM or stored_due_on == new_due_on:
                return LaterActionResult(ActionApplyStatus.REPLAYED, stored_due_on)
        raise ActionRejected(
            ActionErrorCode.NOT_ACTIONABLE,
            "This resurfacing card has already been handled",
        )

    def _replay_dismiss_or_reject(
        self,
        session: Session,
        *,
        user_id: str,
        key: str,
        event_id: str,
    ) -> DismissActionResult:
        existing = self._action_by_key(session, user_id=user_id, key=key)
        if (
            existing is not None
            and existing.event_name is ActionEventName.DISMISSED
            and existing.resurfacing_event_id == event_id
        ):
            return self._dismiss_result_from_action(
                session,
                user_id=user_id,
                event_id=event_id,
                action=existing,
                status=ActionApplyStatus.REPLAYED,
            )
        raise ActionRejected(
            ActionErrorCode.NOT_ACTIONABLE,
            "This resurfacing card has already been handled",
        )

    def _dismiss_result_from_action(
        self,
        session: Session,
        *,
        user_id: str,
        event_id: str,
        action: ActionEvent,
        status: ActionApplyStatus,
    ) -> DismissActionResult:
        if (
            action.event_name is not ActionEventName.DISMISSED
            or action.user_id != user_id
            or action.resurfacing_event_id != event_id
            or action.item_id is None
            or action.session_id is None
        ):
            raise ActionRejected(
                ActionErrorCode.IDEMPOTENCY_CONFLICT,
                "The stored Dismiss result is invalid",
            )
        expires_at = self._stored_undo_expiry(action)
        event = session.scalar(
            select(ResurfacingEvent).where(
                ResurfacingEvent.id == event_id,
                ResurfacingEvent.user_id == user_id,
                ResurfacingEvent.saved_item_id == action.item_id,
            )
        )
        item = session.scalar(
            select(SavedItem).where(
                SavedItem.id == action.item_id,
                SavedItem.user_id == user_id,
            )
        )
        if (
            event is None
            or item is None
            or event.state is not ResurfacingState.RESOLVED
            or event.outcome is not ResurfacingOutcome.DISMISSED
            or item.status is not SavedItemStatus.DISMISSED
            or self._undo_for_dismissal(
                session,
                user_id=user_id,
                event_id=event_id,
                dismiss_action_event_id=action.id,
            )
            is not None
        ):
            # The exact Dismiss was already undone or superseded. Replaying
            # its old POST must not mint a fresh capability (ABA protection).
            raise ActionRejected(
                ActionErrorCode.NOT_ACTIONABLE,
                "This Dismiss is no longer the item's current state",
            )
        return DismissActionResult(
            status=status,
            item_id=item.id,
            dismiss_action_event_id=action.id,
            original_session_id=action.session_id,
            local_date_slot=event.local_date_slot,
            undo_expires_at=expires_at,
        )

    def _validated_dismissal_reference(
        self,
        session: Session,
        *,
        user_id: str,
        event_id: str,
        item_id: str,
        dismiss_action_event_id: str,
        original_session_id: str,
        dismissal_local_date: date,
        undo_expires_at: datetime,
    ) -> tuple[ActionEvent, ResurfacingEvent, SavedItem]:
        action = session.scalar(
            select(ActionEvent).where(
                ActionEvent.id == dismiss_action_event_id,
                ActionEvent.user_id == user_id,
                ActionEvent.event_name == ActionEventName.DISMISSED,
                ActionEvent.resurfacing_event_id == event_id,
                ActionEvent.item_id == item_id,
            )
        )
        event = session.scalar(
            select(ResurfacingEvent).where(
                ResurfacingEvent.id == event_id,
                ResurfacingEvent.user_id == user_id,
                ResurfacingEvent.saved_item_id == item_id,
            )
        )
        item = session.scalar(
            select(SavedItem).where(
                SavedItem.id == item_id,
                SavedItem.user_id == user_id,
            )
        )
        invalid = (
            action is None
            or event is None
            or item is None
            or action.session_id != original_session_id
            or event.local_date_slot != dismissal_local_date
            or event.state is not ResurfacingState.RESOLVED
            or event.outcome is not ResurfacingOutcome.DISMISSED
            or item.status is not SavedItemStatus.DISMISSED
        )
        if not invalid and action is not None:
            invalid = (
                self._stored_undo_expiry(action)
                != self._as_utc(undo_expires_at)
                or self._undo_for_dismissal(
                    session,
                    user_id=user_id,
                    event_id=event_id,
                    dismiss_action_event_id=dismiss_action_event_id,
                )
                is not None
            )
        if invalid or action is None or event is None or item is None:
            raise ActionRejected(
                ActionErrorCode.INVALID_UNDO,
                "This Dismiss can no longer be undone",
            )
        return action, event, item

    @staticmethod
    def _stored_undo_expiry(action: ActionEvent) -> datetime:
        raw_value = action.properties.get("undo_expires_at")
        try:
            if not isinstance(raw_value, str):
                raise ValueError
            parsed = datetime.fromisoformat(raw_value)
            if parsed.tzinfo is None:
                raise ValueError
            return parsed.astimezone(timezone.utc)
        except ValueError as exc:
            raise ActionRejected(
                ActionErrorCode.IDEMPOTENCY_CONFLICT,
                "The stored Dismiss result is invalid",
            ) from exc

    @staticmethod
    def _undo_for_dismissal(
        session: Session,
        *,
        user_id: str,
        event_id: str,
        dismiss_action_event_id: str,
    ) -> ActionEvent | None:
        candidates = session.scalars(
            select(ActionEvent).where(
                ActionEvent.user_id == user_id,
                ActionEvent.resurfacing_event_id == event_id,
                ActionEvent.event_name == ActionEventName.DISMISS_UNDONE,
            )
        )
        return next(
            (
                action
                for action in candidates
                if action.properties.get("dismiss_action_event_id")
                == dismiss_action_event_id
            ),
            None,
        )

    @staticmethod
    def _undo_replay(
        session: Session,
        *,
        user_id: str,
        event_id: str,
        item_id: str,
        dismiss_action_event_id: str,
        action: ActionEvent,
    ) -> DismissUndoResult:
        if (
            action.event_name is not ActionEventName.DISMISS_UNDONE
            or action.user_id != user_id
            or action.resurfacing_event_id != event_id
            or action.item_id != item_id
            or action.properties.get("dismiss_action_event_id")
            != dismiss_action_event_id
        ):
            raise ActionRejected(
                ActionErrorCode.IDEMPOTENCY_CONFLICT,
                "This action key was already used for different input",
            )
        event = session.scalar(
            select(ResurfacingEvent).where(
                ResurfacingEvent.id == event_id,
                ResurfacingEvent.user_id == user_id,
                ResurfacingEvent.saved_item_id == item_id,
            )
        )
        item = session.scalar(
            select(SavedItem).where(
                SavedItem.id == item_id,
                SavedItem.user_id == user_id,
            )
        )
        if event is None or item is None:
            raise ActionRejected(ActionErrorCode.INVALID_UNDO, "Invalid Undo request")
        if (
            item.status is SavedItemStatus.DISMISSED
            and event.state is ResurfacingState.RESOLVED
            and event.outcome is ResurfacingOutcome.DISMISSED
        ):
            raise ActionRejected(
                ActionErrorCode.INVALID_UNDO,
                "This Undo belongs to an earlier Dismiss",
            )
        return DismissUndoResult(ActionApplyStatus.REPLAYED, event.state)

    @staticmethod
    def _pause_replay(action: ActionEvent) -> PauseActionResult:
        value = action.properties.get("had_active_card")
        if action.event_name is not ActionEventName.PAUSE_ENABLED or not isinstance(
            value, bool
        ):
            raise ActionRejected(
                ActionErrorCode.IDEMPOTENCY_CONFLICT,
                "This action key was already used for different input",
            )
        return PauseActionResult(ActionApplyStatus.REPLAYED, value)

    @staticmethod
    def _resume_replay(action: ActionEvent) -> ResumeActionResult:
        value = action.properties.get("paused_duration_seconds")
        if (
            action.event_name is not ActionEventName.PAUSE_DISABLED
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise ActionRejected(
                ActionErrorCode.IDEMPOTENCY_CONFLICT,
                "This action key was already used for different input",
            )
        return ResumeActionResult(ActionApplyStatus.REPLAYED, value)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ActionRejected(
                ActionErrorCode.INVALID_UNDO,
                "Undo expiry must include a timezone",
            )
        return value.astimezone(timezone.utc)

    @staticmethod
    def _lock_user(session: Session, *, user_id: str) -> None:
        locked = session.execute(
            update(User)
            .where(User.id == user_id)
            .values(updated_at=User.updated_at)
            .execution_options(synchronize_session=False)
        )
        if locked.rowcount != 1:
            raise ActionRejected(ActionErrorCode.NOT_FOUND, "Card not found")
        session.expire_all()

    @staticmethod
    def _open_replay(
        session: Session,
        *,
        user_id: str,
        event_id: str,
        result: ActionEvent,
    ) -> OpenActionResult:
        event = session.scalar(
            select(ResurfacingEvent).where(
                ResurfacingEvent.id == event_id,
                ResurfacingEvent.user_id == user_id,
            )
        )
        if event is None or result.resurfacing_event_id != event_id:
            raise ActionRejected(ActionErrorCode.NOT_FOUND, "Card not found")
        item = session.scalar(
            select(SavedItem).where(
                SavedItem.id == event.saved_item_id,
                SavedItem.user_id == user_id,
            )
        )
        if item is None:
            raise ActionRejected(ActionErrorCode.NOT_FOUND, "Card not found")
        if result.event_name is ActionEventName.SOURCE_OPENED:
            return OpenActionResult(
                OpenActionStatus.REPLAYED_OPENED,
                destination_url=item.source_url,
            )
        if result.event_name is ActionEventName.OPEN_FAILED:
            return OpenActionResult(
                OpenActionStatus.REPLAYED_FAILED,
                error_type=str(result.properties.get("error_type", "network_error")),
            )
        raise ActionRejected(
            ActionErrorCode.IDEMPOTENCY_CONFLICT,
            "This action key was already used for a different result",
        )

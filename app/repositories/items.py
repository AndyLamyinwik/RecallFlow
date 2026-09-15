"""User-scoped SavedItem persistence and state changes."""

from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import hmac

from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AIValueLineStatus,
    EffortBand,
    ExtractionErrorType,
    ExtractionStatus,
    Intent,
    ResurfacingEvent,
    ResurfacingState,
    SavedItem,
    SavedItemStatus,
    ScheduleKind,
)
from app.models.entities import utc_now


@dataclass(frozen=True)
class CreateSavedItemResult:
    item: SavedItem
    created: bool


def source_url_fingerprint(*, user_id: str, normalized_url: str, secret: str) -> str:
    """Return the private per-user URL fingerprint defined by architecture.md."""

    message = f"{user_id}{normalized_url}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, sha256).hexdigest()


class SavedItemRepository:
    def create(
        self,
        session: Session,
        *,
        user_id: str,
        source_url: str,
        source_url_hash: str,
        title: str,
        source_domain: str,
        next_due_on: date,
        source_text: str | None = None,
        source_text_hash: str | None = None,
        source_char_count: int | None = None,
        source_truncated: bool = False,
        source_excerpt: str | None = None,
        extraction_status: ExtractionStatus = ExtractionStatus.PENDING,
        extraction_error_type: ExtractionErrorType | None = None,
        extraction_version: str = "extraction/v1",
        saved_at: datetime | None = None,
        intent: Intent = Intent.READ,
        why_saved_note: str | None = None,
        ai_value_line: str | None = None,
        ai_value_line_status: AIValueLineStatus = AIValueLineStatus.NOT_ATTEMPTED,
        effort_band: EffortBand = EffortBand.UNKNOWN,
        effort_rule_version: str = "effort_rule/v1",
        schedule_timezone: str | None = None,
    ) -> CreateSavedItemResult:
        """Create an item or return the existing per-user URL duplicate.

        A savepoint isolates the expected uniqueness race so callers do not lose
        other work in their outer transaction.
        """

        item = SavedItem(
            user_id=user_id,
            source_url=source_url,
            source_url_hash=source_url_hash,
            title=title,
            source_domain=source_domain,
            source_text=source_text,
            source_text_hash=source_text_hash,
            source_char_count=source_char_count,
            source_truncated=source_truncated,
            source_excerpt=source_excerpt,
            extraction_status=extraction_status,
            extraction_error_type=extraction_error_type,
            extraction_version=extraction_version,
            saved_at=saved_at or utc_now(),
            intent=intent,
            why_saved_note=why_saved_note,
            ai_value_line=ai_value_line,
            ai_value_line_status=ai_value_line_status,
            effort_band=effort_band,
            effort_rule_version=effort_rule_version,
            status=SavedItemStatus.ACTIVE,
            schedule_kind=ScheduleKind.STANDARD,
            next_due_on=next_due_on,
            schedule_timezone=schedule_timezone,
        )

        try:
            with session.begin_nested():
                session.add(item)
                session.flush()
        except IntegrityError:
            existing = self.find_duplicate(
                session,
                user_id=user_id,
                source_url_hash=source_url_hash,
            )
            if existing is None:
                raise
            return CreateSavedItemResult(item=existing, created=False)

        return CreateSavedItemResult(item=item, created=True)

    def get(self, session: Session, *, user_id: str, item_id: str) -> SavedItem | None:
        return session.scalar(
            select(SavedItem).where(
                SavedItem.id == item_id,
                SavedItem.user_id == user_id,
            )
        )

    def list_for_user(self, session: Session, *, user_id: str) -> list[SavedItem]:
        return list(
            session.scalars(
                select(SavedItem)
                .where(SavedItem.user_id == user_id)
                .order_by(SavedItem.saved_at.desc(), SavedItem.id)
            )
        )

    def find_duplicate(
        self,
        session: Session,
        *,
        user_id: str,
        source_url_hash: str,
    ) -> SavedItem | None:
        return session.scalar(
            select(SavedItem).where(
                SavedItem.user_id == user_id,
                SavedItem.source_url_hash == source_url_hash,
            )
        )

    def update_schedule(
        self,
        session: Session,
        *,
        user_id: str,
        item_id: str,
        schedule_kind: ScheduleKind,
        next_due_on: date | None,
        schedule_timezone: str | None = None,
    ) -> SavedItem | None:
        if schedule_kind is ScheduleKind.RECALL:
            raise ValueError("Recall schedules may only be created by RecallService")
        if schedule_kind is ScheduleKind.NONE and next_due_on is not None:
            raise ValueError("NONE schedules cannot have a due date")
        if schedule_kind is not ScheduleKind.NONE and next_due_on is None:
            raise ValueError("scheduled items require a due date")

        item = self.get(session, user_id=user_id, item_id=item_id)
        if item is None:
            return None
        item.schedule_kind = schedule_kind
        item.next_due_on = next_due_on
        if schedule_kind is ScheduleKind.NONE:
            item.schedule_timezone = None
        elif schedule_timezone is not None:
            item.schedule_timezone = schedule_timezone
        item.updated_at = utc_now()
        session.flush()
        return item

    def update_schedule_if_no_active_cycle(
        self,
        session: Session,
        *,
        user_id: str,
        item_id: str,
        expected_updated_at: datetime,
        schedule_kind: ScheduleKind,
        next_due_on: date | None,
        schedule_timezone: str | None = None,
    ) -> bool:
        """Atomically avoid overwriting a resurfacing decision or active card."""

        if schedule_kind is ScheduleKind.RECALL:
            raise ValueError("Recall schedules may only be created by RecallService")
        if schedule_kind is ScheduleKind.NONE and next_due_on is not None:
            raise ValueError("NONE schedules cannot have a due date")
        if schedule_kind is not ScheduleKind.NONE and next_due_on is None:
            raise ValueError("scheduled items require a due date")

        active_cycle = exists(
            select(ResurfacingEvent.id).where(
                ResurfacingEvent.user_id == user_id,
                ResurfacingEvent.saved_item_id == SavedItem.id,
                ResurfacingEvent.state.in_(
                    (ResurfacingState.PENDING, ResurfacingState.SHOWN)
                ),
            )
        )
        result = session.execute(
            update(SavedItem)
            .where(
                SavedItem.id == item_id,
                SavedItem.user_id == user_id,
                SavedItem.status == SavedItemStatus.ACTIVE,
                SavedItem.updated_at == expected_updated_at,
                ~active_cycle,
            )
            .values(
                schedule_kind=schedule_kind,
                next_due_on=next_due_on,
                schedule_timezone=(
                    None if schedule_kind is ScheduleKind.NONE else schedule_timezone
                ),
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def dismiss(self, session: Session, *, user_id: str, item_id: str) -> SavedItem | None:
        """Exclude an item from resurfacing while retaining its saved schedule."""

        item = self.get(session, user_id=user_id, item_id=item_id)
        if item is None:
            return None
        item.status = SavedItemStatus.DISMISSED
        item.updated_at = utc_now()
        session.flush()
        return item

    def hard_delete(self, session: Session, *, user_id: str, item_id: str) -> bool:
        item = self.get(session, user_id=user_id, item_id=item_id)
        if item is None:
            return False
        session.delete(item)
        session.flush()
        return True

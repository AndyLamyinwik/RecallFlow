"""SQLAlchemy models for the minimum RecallFlow data model."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    Enum as SqlEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.db_types import UTCDateTime
from app.models.enums import (
    AIValueLineStatus,
    ActionActor,
    ActionEventName,
    EffortBand,
    ExtractionErrorType,
    ExtractionStatus,
    Intent,
    ResurfacingCloseReason,
    ResurfacingKind,
    ResurfacingOutcome,
    ResurfacingState,
    SavedItemStatus,
    ScheduleKind,
)


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def enum_type(enum_class: type, name: str) -> SqlEnum:
    return SqlEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "access_code_fingerprint IS NULL OR length(access_code_fingerprint) = 64",
            name="ck_user_access_code_fingerprint_length",
        ),
        Index(
            "ux_user_access_code_fingerprint",
            "access_code_fingerprint",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    access_code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    access_code_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    resurfacing_paused_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    last_new_impression_local_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    saved_items: Mapped[list[SavedItem]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    resurfacing_events: Mapped[list[ResurfacingEvent]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    action_events: Mapped[list[ActionEvent]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )


class SavedItem(Base):
    __tablename__ = "saved_items"
    __table_args__ = (
        UniqueConstraint("user_id", "source_url_hash", name="uq_saved_item_user_url_hash"),
        CheckConstraint("length(source_url_hash) = 64", name="ck_saved_item_url_hash_length"),
        CheckConstraint(
            "source_text_hash IS NULL OR length(source_text_hash) = 64",
            name="ck_saved_item_text_hash_length",
        ),
        CheckConstraint(
            "source_char_count IS NULL OR source_char_count >= 0",
            name="ck_saved_item_source_char_count",
        ),
        CheckConstraint(
            "source_truncated = 0 OR (source_text IS NULL AND source_text_hash IS NULL)",
            name="ck_saved_item_truncated_body_discarded",
        ),
        CheckConstraint("length(source_text) <= 50000", name="ck_saved_item_source_text_length"),
        CheckConstraint("length(source_excerpt) <= 280", name="ck_saved_item_excerpt_length"),
        CheckConstraint("length(why_saved_note) <= 500", name="ck_saved_item_note_length"),
        CheckConstraint("length(ai_value_line) <= 180", name="ck_saved_item_value_line_length"),
        CheckConstraint(
            "(schedule_kind = 'NONE' AND next_due_on IS NULL) OR "
            "(schedule_kind IN ('STANDARD', 'RECALL') AND next_due_on IS NOT NULL)",
            name="ck_saved_item_schedule_date",
        ),
        Index("ix_saved_item_due", "user_id", "status", "next_due_on", "saved_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_text_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_char_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_truncated: Mapped[bool] = mapped_column(nullable=False, default=False)
    source_excerpt: Mapped[str | None] = mapped_column(String(280), nullable=True)
    extraction_status: Mapped[ExtractionStatus] = mapped_column(
        enum_type(ExtractionStatus, "extraction_status"),
        nullable=False,
        default=ExtractionStatus.PENDING,
    )
    extraction_error_type: Mapped[ExtractionErrorType | None] = mapped_column(
        enum_type(ExtractionErrorType, "extraction_error_type"), nullable=True
    )
    extraction_version: Mapped[str] = mapped_column(
        String(80), nullable=False, default="extraction/v1"
    )
    saved_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    intent: Mapped[Intent] = mapped_column(
        enum_type(Intent, "intent"), nullable=False, default=Intent.READ
    )
    why_saved_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ai_value_line: Mapped[str | None] = mapped_column(String(180), nullable=True)
    ai_value_line_status: Mapped[AIValueLineStatus] = mapped_column(
        enum_type(AIValueLineStatus, "ai_value_line_status"),
        nullable=False,
        default=AIValueLineStatus.NOT_ATTEMPTED,
    )
    effort_band: Mapped[EffortBand] = mapped_column(
        enum_type(EffortBand, "effort_band"), nullable=False, default=EffortBand.UNKNOWN
    )
    effort_rule_version: Mapped[str] = mapped_column(
        String(80), nullable=False, default="effort_rule/v1"
    )
    status: Mapped[SavedItemStatus] = mapped_column(
        enum_type(SavedItemStatus, "saved_item_status"),
        nullable=False,
        default=SavedItemStatus.ACTIVE,
    )
    schedule_kind: Mapped[ScheduleKind] = mapped_column(
        enum_type(ScheduleKind, "schedule_kind"),
        nullable=False,
        default=ScheduleKind.STANDARD,
    )
    next_due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    schedule_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_source_opened_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    user: Mapped[User] = relationship(back_populates="saved_items")
    resurfacing_events: Mapped[list[ResurfacingEvent]] = relationship(
        back_populates="saved_item", cascade="all, delete-orphan", passive_deletes=True
    )
    action_events: Mapped[list[ActionEvent]] = relationship(
        back_populates="item", cascade="all, delete-orphan", passive_deletes=True
    )


class ResurfacingEvent(Base):
    __tablename__ = "resurfacing_events"
    __table_args__ = (
        UniqueConstraint("user_id", "local_date_slot", name="uq_resurfacing_user_date"),
        CheckConstraint("eligible_count_snapshot >= 0", name="ck_resurfacing_eligible_count"),
        CheckConstraint(
            "(state = 'PENDING' AND shown_at IS NULL AND outcome IS NULL "
            "AND outcome_at IS NULL AND closed_at IS NULL AND close_reason IS NULL) OR "
            "(state = 'SHOWN' AND shown_at IS NOT NULL AND outcome IS NULL "
            "AND outcome_at IS NULL AND closed_at IS NULL AND close_reason IS NULL) OR "
            "(state = 'RESOLVED' AND shown_at IS NOT NULL AND outcome IS NOT NULL "
            "AND outcome_at IS NOT NULL AND closed_at IS NOT NULL AND close_reason IS NULL) OR "
            "(state = 'CLOSED' AND outcome IS NULL AND outcome_at IS NULL "
            "AND closed_at IS NOT NULL AND close_reason IS NOT NULL)",
            name="ck_resurfacing_state_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    saved_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("saved_items.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ResurfacingKind] = mapped_column(
        enum_type(ResurfacingKind, "resurfacing_kind"),
        nullable=False,
        default=ResurfacingKind.STANDARD,
    )
    scheduled_on_snapshot: Mapped[date] = mapped_column(Date, nullable=False)
    selection_rule_version: Mapped[str] = mapped_column(
        String(80), nullable=False, default="earliest_due_oldest_saved/v1"
    )
    eligible_count_snapshot: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    local_date_slot: Mapped[date] = mapped_column(Date, nullable=False)
    timezone_at_selection: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[ResurfacingState] = mapped_column(
        enum_type(ResurfacingState, "resurfacing_state"),
        nullable=False,
        default=ResurfacingState.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    shown_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    outcome: Mapped[ResurfacingOutcome | None] = mapped_column(
        enum_type(ResurfacingOutcome, "resurfacing_outcome"), nullable=True
    )
    outcome_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    close_reason: Mapped[ResurfacingCloseReason | None] = mapped_column(
        enum_type(ResurfacingCloseReason, "resurfacing_close_reason"), nullable=True
    )
    open_in_flight_until: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="resurfacing_events")
    saved_item: Mapped[SavedItem] = relationship(back_populates="resurfacing_events")
    action_events: Mapped[list[ActionEvent]] = relationship(
        back_populates="resurfacing_event", cascade="all, delete-orphan", passive_deletes=True
    )


class ActionEvent(Base):
    __tablename__ = "action_events"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_action_user_idempotency"),
        Index("ix_action_user_occurred", "user_id", "occurred_at"),
        Index("ix_action_resurfacing_name", "resurfacing_event_id", "event_name"),
        Index("ix_action_item_name", "item_id", "event_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_name: Mapped[ActionEventName] = mapped_column(
        enum_type(ActionEventName, "action_event_name"), nullable=False
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    occurred_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("saved_items.id", ondelete="CASCADE"), nullable=True
    )
    resurfacing_event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("resurfacing_events.id", ondelete="CASCADE"), nullable=True
    )
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    actor: Mapped[ActionActor] = mapped_column(
        enum_type(ActionActor, "action_actor"), nullable=False
    )
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    user: Mapped[User] = relationship(back_populates="action_events")
    item: Mapped[SavedItem | None] = relationship(back_populates="action_events")
    resurfacing_event: Mapped[ResurfacingEvent | None] = relationship(
        back_populates="action_events"
    )

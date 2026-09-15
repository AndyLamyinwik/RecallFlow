"""Capture one public article without AI enrichment."""

from dataclasses import dataclass
from datetime import timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.models import SavedItem, User
from app.repositories import SavedItemRepository, source_url_fingerprint
from app.schemas.capture import CaptureInput
from app.services.clock import Clock, SystemClock
from app.services.extraction import ArticleExtractor
from app.services.url_policy import URLPolicyError, normalize_url_v1


class CaptureErrorCode(StrEnum):
    USER_NOT_FOUND = "USER_NOT_FOUND"
    INVALID_TIMEZONE = "INVALID_TIMEZONE"
    INVALID_REVISIT_DATE = "INVALID_REVISIT_DATE"
    INVALID_URL = "INVALID_URL"
    PRIVATE_TARGET = "PRIVATE_TARGET"


class CaptureRejected(ValueError):
    def __init__(self, code: CaptureErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CaptureResult:
    item: SavedItem
    created: bool


class CaptureService:
    def __init__(
        self,
        extractor: ArticleExtractor,
        *,
        fingerprint_secret: str,
        repository: SavedItemRepository | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._extractor = extractor
        self._fingerprint_secret = fingerprint_secret
        self._repository = repository or SavedItemRepository()
        self._clock = clock or SystemClock()

    def capture(self, session: Session, *, user_id: str, payload: CaptureInput) -> CaptureResult:
        user = session.get(User, user_id)
        if user is None:
            raise CaptureRejected(CaptureErrorCode.USER_NOT_FOUND, "The pilot user does not exist")

        try:
            normalized_url = normalize_url_v1(payload.url)
        except URLPolicyError as exc:
            code = (
                CaptureErrorCode.PRIVATE_TARGET
                if exc.code.value == "PRIVATE_TARGET"
                else CaptureErrorCode.INVALID_URL
            )
            raise CaptureRejected(code, str(exc)) from exc

        url_hash = source_url_fingerprint(
            user_id=user_id,
            normalized_url=normalized_url,
            secret=self._fingerprint_secret,
        )
        duplicate = self._repository.find_duplicate(
            session,
            user_id=user_id,
            source_url_hash=url_hash,
        )
        if duplicate is not None:
            return CaptureResult(item=duplicate, created=False)

        now = self._clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        try:
            local_today = now.astimezone(ZoneInfo(user.timezone)).date()
        except ZoneInfoNotFoundError as exc:
            raise CaptureRejected(CaptureErrorCode.INVALID_TIMEZONE, "The user's timezone is invalid") from exc

        revisit_on = payload.revisit_on or (local_today + timedelta(days=7))
        if revisit_on < local_today:
            raise CaptureRejected(
                CaptureErrorCode.INVALID_REVISIT_DATE,
                "The revisit date cannot be before today",
            )

        try:
            extracted = self._extractor.extract(normalized_url)
        except URLPolicyError as exc:
            raise CaptureRejected(CaptureErrorCode.PRIVATE_TARGET, str(exc)) from exc

        created = self._repository.create(
            session,
            user_id=user_id,
            source_url=normalized_url,
            source_url_hash=url_hash,
            title=extracted.title,
            source_domain=extracted.source_domain,
            source_text=extracted.source_text,
            source_text_hash=extracted.source_text_hash,
            source_char_count=extracted.source_char_count,
            source_truncated=extracted.source_truncated,
            source_excerpt=extracted.source_excerpt,
            extraction_status=extracted.extraction_status,
            extraction_error_type=extracted.extraction_error_type,
            extraction_version=extracted.extraction_version,
            saved_at=now,
            intent=payload.intent,
            why_saved_note=payload.why_saved_note,
            effort_band=extracted.effort_band,
            effort_rule_version=extracted.effort_rule_version,
            next_due_on=revisit_on,
            schedule_timezone=user.timezone,
        )
        return CaptureResult(item=created.item, created=created.created)

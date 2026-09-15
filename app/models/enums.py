"""Database-backed domain enums for RecallFlow's primary V1 path."""

from enum import StrEnum


class Intent(StrEnum):
    READ = "READ"
    LEARN = "LEARN"


class ExtractionStatus(StrEnum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    INADEQUATE = "INADEQUATE"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class ExtractionErrorType(StrEnum):
    DNS_ERROR = "DNS_ERROR"
    TIMEOUT = "TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    HTTP_STATUS = "HTTP_STATUS"
    REDIRECT_LIMIT = "REDIRECT_LIMIT"
    REDIRECT_POLICY = "REDIRECT_POLICY"
    PEER_IP_UNAVAILABLE = "PEER_IP_UNAVAILABLE"
    PEER_IP_MISMATCH = "PEER_IP_MISMATCH"
    TOO_LARGE = "TOO_LARGE"
    UNSUPPORTED_MIME = "UNSUPPORTED_MIME"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    INADEQUATE_CONTENT = "INADEQUATE_CONTENT"


class AIValueLineStatus(StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FALLBACK_EXCERPT = "FALLBACK_EXCERPT"
    INSUFFICIENT_SOURCE = "INSUFFICIENT_SOURCE"
    OMITTED_AFTER_FAILURE = "OMITTED_AFTER_FAILURE"


class EffortBand(StrEnum):
    SHORT = "SHORT"
    MEDIUM = "MEDIUM"
    LONG = "LONG"
    UNKNOWN = "UNKNOWN"


class SavedItemStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISMISSED = "DISMISSED"


class ScheduleKind(StrEnum):
    STANDARD = "STANDARD"
    RECALL = "RECALL"
    NONE = "NONE"


class ResurfacingKind(StrEnum):
    STANDARD = "STANDARD"
    RECALL = "RECALL"


class ResurfacingState(StrEnum):
    PENDING = "PENDING"
    SHOWN = "SHOWN"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class ResurfacingOutcome(StrEnum):
    OPENED = "OPENED"
    LATER_CONFIRMED = "LATER_CONFIRMED"
    DISMISSED = "DISMISSED"
    RECALL_COMPLETED = "RECALL_COMPLETED"


class ResurfacingCloseReason(StrEnum):
    NO_ACTION = "NO_ACTION"
    PAUSED = "PAUSED"
    STALE_PENDING = "STALE_PENDING"
    DISMISS_UNDONE = "DISMISS_UNDONE"


class ActionActor(StrEnum):
    USER = "USER"
    CLIENT = "CLIENT"
    SYSTEM = "SYSTEM"


class ActionEventName(StrEnum):
    CAPTURE_STARTED = "capture_started"
    CAPTURE_SUCCEEDED = "capture_succeeded"
    CAPTURE_FAILED = "capture_failed"
    RESURFACING_IMPRESSION = "resurfacing_impression"
    OPEN_ATTEMPT = "open_attempt"
    SOURCE_OPENED = "source_opened"
    OPEN_FAILED = "open_failed"
    LATER_SELECTED = "later_selected"
    LATER_CONFIRMED = "later_confirmed"
    DISMISSED = "dismissed"
    DISMISS_UNDONE = "dismiss_undone"
    PAUSE_ENABLED = "pause_enabled"
    PAUSE_DISABLED = "pause_disabled"
    NO_ACTION_RECORDED = "no_action_recorded"

"""Versioned deterministic effort rules."""

from app.models import EffortBand, ExtractionStatus


def effort_band_v1(
    *,
    extraction_status: ExtractionStatus,
    source_char_count: int | None,
    short_max: int = 6_000,
    medium_max: int = 18_000,
) -> EffortBand:
    """Classify by normalized character count; this is a v1 rule, not a standard."""

    if extraction_status is not ExtractionStatus.SUCCESS or source_char_count is None:
        return EffortBand.UNKNOWN
    if source_char_count <= short_max:
        return EffortBand.SHORT
    if source_char_count <= medium_max:
        return EffortBand.MEDIUM
    return EffortBand.LONG

"""Validated input for article capture."""

from datetime import date

from pydantic import BaseModel, Field, field_validator

from app.models import Intent


class CaptureInput(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    intent: Intent = Intent.READ
    revisit_on: date | None = None
    why_saved_note: str | None = Field(default=None, max_length=500)

    @field_validator("url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("URL is required")
        return value

    @field_validator("why_saved_note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class RevisitDateInput(BaseModel):
    """Validated calendar date used by the Saved Item UI."""

    revisit_on: date

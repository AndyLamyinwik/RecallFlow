"""Environment-backed application configuration."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Small configuration surface shared by application modules."""

    app_name: str = "RecallFlow"
    app_env: str = "development"
    app_secret: str = "development-only-change-me"
    database_url: str = "sqlite:///./data/recallflow.db"
    openai_api_key: str = ""
    llm_model: str = "gpt-5.4-mini-2026-03-17"
    access_codes: str = ""
    pilot_session_max_age_seconds: int = 8 * 60 * 60
    pilot_login_csrf_max_age_seconds: int = 10 * 60

    # Capture/extraction v1 safety and product constants. These are explicit,
    # replaceable configuration values, not validated quality standards.
    extraction_version: str = "extraction/v1"
    effort_rule_version: str = "effort_rule/v1"
    extraction_min_chars: int = 800
    extraction_min_units: int = 3
    source_text_max_chars: int = 50_000
    source_excerpt_max_chars: int = 280
    effort_short_max_chars: int = 6_000
    effort_medium_max_chars: int = 18_000
    capture_fetch_connect_timeout_seconds: float = 3.0
    capture_fetch_read_budget_seconds: float = 10.0
    capture_fetch_max_bytes: int = 2 * 1024 * 1024
    capture_fetch_max_redirects: int = 3
    open_lease_seconds: int = 15
    open_reachability_max_bytes: int = 16 * 1024
    dismiss_undo_window_seconds: int = 30

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def secure_cookies(self) -> bool:
        return self.app_env.lower() in {"production", "staging"}

    @property
    def pilot_access_codes(self) -> tuple[str, ...]:
        """Return de-duplicated invitation codes without persisting them."""

        return tuple(dict.fromkeys(code.strip() for code in self.access_codes.split(",") if code.strip()))

    def validate_runtime(self) -> None:
        deployed = self.app_env.lower() in {"production", "staging"}
        if deployed:
            if len(self.app_secret.strip()) < 32:
                raise RuntimeError("APP_SECRET must contain at least 32 characters")
            if not self.pilot_access_codes:
                raise RuntimeError("ACCESS_CODES must contain at least one pilot code")
        if any(len(code) < 16 or len(code) > 256 for code in self.pilot_access_codes):
            raise RuntimeError("Each pilot access code must contain 16 to 256 characters")
        if self.pilot_session_max_age_seconds <= 0:
            raise RuntimeError("PILOT_SESSION_MAX_AGE_SECONDS must be positive")
        if self.pilot_login_csrf_max_age_seconds <= 0:
            raise RuntimeError("PILOT_LOGIN_CSRF_MAX_AGE_SECONDS must be positive")
        if self.open_lease_seconds <= 0:
            raise RuntimeError("OPEN_LEASE_SECONDS must be positive")
        if self.open_reachability_max_bytes <= 0:
            raise RuntimeError("OPEN_REACHABILITY_MAX_BYTES must be positive")
        if self.dismiss_undo_window_seconds <= 0:
            raise RuntimeError("DISMISS_UNDO_WINDOW_SECONDS must be positive")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_runtime()
    return settings

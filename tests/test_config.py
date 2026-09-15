import pytest

from app.config import Settings


@pytest.mark.parametrize("secret", ["", "   ", "short", "development-only-change-me"])
def test_deployed_environment_rejects_weak_secret(secret: str) -> None:
    settings = Settings(app_env="production", app_secret=secret)

    with pytest.raises(RuntimeError, match="at least 32 characters"):
        settings.validate_runtime()


def test_deployed_environment_accepts_long_secret() -> None:
    settings = Settings(
        app_env="production",
        app_secret="x" * 32,
        access_codes="a-high-entropy-pilot-code",
    )

    settings.validate_runtime()


def test_deployed_environment_requires_a_pilot_access_code() -> None:
    settings = Settings(app_env="production", app_secret="x" * 32, access_codes="")

    with pytest.raises(RuntimeError, match="ACCESS_CODES"):
        settings.validate_runtime()


def test_dismiss_undo_window_must_be_positive() -> None:
    settings = Settings(dismiss_undo_window_seconds=0)

    with pytest.raises(RuntimeError, match="DISMISS_UNDO_WINDOW_SECONDS"):
        settings.validate_runtime()

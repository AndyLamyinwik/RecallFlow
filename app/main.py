"""RecallFlow ASGI application."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.config import Settings, get_settings
from app.database import SessionLocal, initialize_database
from app.services import (
    CaptureService,
    ResurfacingActionService,
    ResurfacingEngine,
    SafeSourceReachabilityChecker,
)
from app.services.clock import Clock
from app.services.extraction import ArticleExtractor, ExtractionV1Config
from app.services.fetch import SafeHttpFetcher
from app.web import SessionFactory, create_web_router


APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=APP_DIR / "templates")


def _default_capture_service(settings: Settings) -> CaptureService:
    fetcher = SafeHttpFetcher(
        connect_timeout_seconds=settings.capture_fetch_connect_timeout_seconds,
        read_budget_seconds=settings.capture_fetch_read_budget_seconds,
        max_bytes=settings.capture_fetch_max_bytes,
        max_redirects=settings.capture_fetch_max_redirects,
    )
    extractor = ArticleExtractor(
        fetcher,
        config=ExtractionV1Config(
            extraction_version=settings.extraction_version,
            effort_rule_version=settings.effort_rule_version,
            minimum_chars=settings.extraction_min_chars,
            minimum_units=settings.extraction_min_units,
            source_text_max_chars=settings.source_text_max_chars,
            excerpt_max_chars=settings.source_excerpt_max_chars,
            effort_short_max_chars=settings.effort_short_max_chars,
            effort_medium_max_chars=settings.effort_medium_max_chars,
        ),
    )
    return CaptureService(extractor, fingerprint_secret=settings.app_secret)


def _default_action_service(
    settings: Settings,
    *,
    clock: Clock | None,
) -> ResurfacingActionService:
    checker = SafeSourceReachabilityChecker(
        connect_timeout_seconds=settings.capture_fetch_connect_timeout_seconds,
        total_budget_seconds=settings.capture_fetch_read_budget_seconds,
        max_bytes=settings.open_reachability_max_bytes,
        max_redirects=settings.capture_fetch_max_redirects,
    )
    return ResurfacingActionService(
        checker=checker,
        clock=clock,
        open_lease_seconds=settings.open_lease_seconds,
        dismiss_undo_window_seconds=settings.dismiss_undo_window_seconds,
    )


def create_app(
    *,
    database_initializer: Callable[[], None] = initialize_database,
    session_factory: SessionFactory = SessionLocal,
    capture_service: CaptureService | None = None,
    app_settings: Settings | None = None,
    clock: Clock | None = None,
    resurfacing_engine: ResurfacingEngine | None = None,
    action_service: ResurfacingActionService | None = None,
) -> FastAPI:
    """Create an app whose startup dependency can be isolated in tests."""

    selected_settings = app_settings or get_settings()
    selected_capture_service = capture_service or _default_capture_service(selected_settings)
    selected_action_service = action_service or _default_action_service(
        selected_settings,
        clock=clock,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database_initializer()
        yield

    recallflow_app = FastAPI(title=selected_settings.app_name, lifespan=lifespan)
    recallflow_app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    recallflow_app.include_router(
        create_web_router(
            settings=selected_settings,
            session_factory=session_factory,
            capture_service=selected_capture_service,
            templates=templates,
            clock=clock,
            resurfacing_engine=resurfacing_engine,
            action_service=selected_action_service,
        )
    )

    @recallflow_app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return recallflow_app


app = create_app()

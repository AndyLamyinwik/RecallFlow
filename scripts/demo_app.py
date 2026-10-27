"""Isolated local walkthrough harness; this is NOT the production app.

Only the normal RecallFlow routes generate product events. The extractor and
reachability transport are explicitly mocked; article text is authored DEMO
content. A fixed clock avoids changing the computer clock or editing schedules
directly. Product telemetry is emitted only by the production routes; this
harness never manufactures analytics event rows.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import secrets
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit
from unittest.mock import patch


DEMO_NOW = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
DEMO_CASES = ("open", "later", "dismiss")
DEMO_TEXT = (
    "DEMO ARTICLE — synthetic content written for the RecallFlow walkthrough. "
    "Saving a link preserves an intention, but a clear future date makes the next "
    "decision visible. A deterministic queue can bring one useful article back "
    "without asking a person to clear an overdue backlog. The person remains in "
    "control: open the source, move the date, or dismiss the reminder. "
) * 4


@dataclass
class DemoClock:
    current: datetime = DEMO_NOW

    def now(self) -> datetime:
        return self.current


class DemoExtractor:
    """No DNS or HTTP requests; only fixed, clearly synthetic fixtures."""

    def extract(self, normalized_url: str) -> Any:
        from app.models import EffortBand, ExtractionErrorType, ExtractionStatus
        from app.services.extraction import ArticleExtraction

        fallback = "fallback" in urlsplit(normalized_url).path
        case = urlsplit(normalized_url).path.rstrip("/").split("/")[-1]
        text = None if fallback else DEMO_TEXT
        return ArticleExtraction(
            title=f"DEMO — Reconnect with useful knowledge ({case})",
            source_domain=urlsplit(normalized_url).hostname or "example.com",
            source_text=text,
            source_text_hash=sha256(text.encode()).hexdigest() if text else None,
            source_char_count=len(text) if text else None,
            source_truncated=False,
            source_excerpt=("DEMO: choose when a saved article should return; keep the next decision in your hands." if text else None),
            extraction_status=ExtractionStatus.FAILED if fallback else ExtractionStatus.SUCCESS,
            extraction_error_type=ExtractionErrorType.TIMEOUT if fallback else None,
            extraction_version="extraction/v1",
            effort_band=EffortBand.UNKNOWN if fallback else EffortBand.SHORT,
            effort_rule_version="effort_rule/v1",
        )


class DemoReachabilityChecker:
    """Simulated network outcomes, never a real safety/reachability proof."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def check(self, source_url: str) -> Any:
        from app.services.reachability import (
            ReachabilityErrorCode,
            SourceReachability,
            SourceReachabilityError,
        )

        self.calls[source_url] = self.calls.get(source_url, 0) + 1
        if "fail-once" in source_url and self.calls[source_url] == 1:
            raise SourceReachabilityError(
                ReachabilityErrorCode.TIMEOUT,
                "DEMO simulated reachability timeout",
                retryable=True,
            )
        return SourceReachability(final_url=source_url, method="HEAD", status_class="2xx")


def create_demo_environment(
    database_path: Path,
    *,
    source_base_url: str = "https://example.com",
    show_banner: bool = True,
) -> SimpleNamespace:
    """Create a NEW demo DB; never open or overwrite an existing database.

    Caller owns engine disposal. Credentials are random per process and never
    written to a tracked file. Settings explicitly bypass the real .env file.
    """

    from app.config import Settings, get_settings as original_get_settings

    database_path = Path(database_path).resolve()
    if "demo" not in database_path.stem.lower() or database_path.suffix != ".db":
        raise ValueError("Use a separate database named *demo*.db")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidentally opening a real/local database.
    with database_path.open("xb"):
        pass
    access_codes = {case: secrets.token_urlsafe(24) for case in DEMO_CASES}
    settings = Settings(
        _env_file=None,
        app_env="development",
        app_secret=secrets.token_urlsafe(40),
        access_codes=",".join(access_codes.values()),
        database_url=f"sqlite:///{database_path.as_posix()}",
    )
    # These imports construct global defaults. During import, supply the demo
    # settings too, so even a fresh process cannot read .env or target real data.
    with patch("app.config.get_settings", return_value=settings):
        from app import database as database_module
        from app import main as main_module
        from app.database import build_engine, initialize_database
        from app.main import create_app
        from app.services.actions import ResurfacingActionService
        from app.services.capture import CaptureService
        from app.services.resurfacing import ResurfacingEngine
    # A fresh imported module binds `from ... import get_settings`; do not
    # leave that binding pointed at an expired unittest.mock object.
    database_module.get_settings = original_get_settings
    main_module.get_settings = original_get_settings
    from fastapi.responses import HTMLResponse
    from sqlalchemy.orm import sessionmaker

    engine = build_engine(settings.database_url)
    initialize_database(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    clock = DemoClock()
    checker = DemoReachabilityChecker()
    capture = CaptureService(DemoExtractor(), fingerprint_secret=settings.app_secret, clock=clock)
    app = create_app(
        database_initializer=lambda: None,
        session_factory=factory,
        app_settings=settings,
        capture_service=capture,
        clock=clock,
        resurfacing_engine=ResurfacingEngine(clock=clock),
        action_service=ResurfacingActionService(checker=checker, clock=clock),
    )

    @app.get("/demo/source/{case}", response_class=HTMLResponse)
    def demo_source(case: str) -> HTMLResponse:
        # Do not reflect arbitrary URL content; case is intentionally not used.
        return HTMLResponse(
            "<!doctype html><html><head><title>DEMO original article</title></head>"
            "<body style='font:22px system-ui;max-width:850px;margin:100px auto'>"
            "<h1>DEMO — Reconnect with useful knowledge</h1>"
            "<p>This is authored synthetic article content, not a fetched source.</p>"
            f"<p>{DEMO_TEXT[:390]}</p>"
            "<p>Open issued a navigation handoff. It does not establish reading.</p>"
            "<a href='/items'>Back to RecallFlow</a></body></html>"
        )

    @app.get("/demo/architecture")
    def demo_architecture() -> Any:
        from fastapi.responses import FileResponse, PlainTextResponse

        diagram = Path(__file__).resolve().parents[1] / "docs" / "media" / "data-flow.svg"
        if not diagram.is_file():
            return PlainTextResponse("Demo data-flow diagram is not generated yet.", status_code=503)
        return FileResponse(diagram, media_type="image/svg+xml")

    if show_banner:
        @app.middleware("http")
        async def label_demo(request: Any, call_next: Any) -> Any:
            from starlette.responses import Response

            response = await call_next(request)
            if "text/html" not in response.headers.get("content-type", ""):
                return response
            content = b"".join([chunk async for chunk in response.body_iterator])
            banner = (
                "<div style='position:fixed;top:0;left:0;right:0;z-index:9999;"
                "background:#203629;color:#fff;padding:10px 20px;font:14px system-ui;"
                "text-align:center'>DEMO DATA · 3 separate demo accounts · "
                "2026-10-26 UTC · extraction/reachability mocked · no human usage results</div>"
                "<div style='height:44px'></div>"
            ).encode()
            content = content.replace(b"<body>", b"<body>" + banner, 1)
            # Synthetic-source HTML has an inline body style, so insert there too.
            if banner not in content and b"</head>" in content:
                content = content.replace(b"</head>", b"</head>" + banner, 1)
            headers = dict(response.headers)
            headers.pop("content-length", None)
            result = Response(content, status_code=response.status_code, headers=headers)
            # Dict conversion loses duplicate Set-Cookie. Preserve originals.
            result.raw_headers = [(key, value) for key, value in response.raw_headers if key.lower() != b"content-length"]
            result.raw_headers.append((b"content-length", str(len(content)).encode()))
            return result

    return SimpleNamespace(
        app=app,
        engine=engine,
        factory=factory,
        settings=settings,
        clock=clock,
        checker=checker,
        access_codes=access_codes,
        database_path=database_path,
        source_base_url=source_base_url.rstrip("/"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/recallflow-demo.db"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--source-base-url", default="https://example.com")
    parser.add_argument("--seed", action="store_true", help="Prepare Later/Dismiss accounts through normal save routes")
    args = parser.parse_args()
    data_root = (Path(__file__).resolve().parents[1] / "data").resolve()
    if not args.database.resolve().is_relative_to(data_root):
        parser.error("Demo CLI databases must stay within this project's ignored data/ directory")
    env = create_demo_environment(args.database, source_base_url=args.source_base_url)
    if args.seed:
        from scripts.generate_demo_usage import login, save
        from fastapi.testclient import TestClient

        for case in ("later", "dismiss"):
            with TestClient(env.app) as client:
                login(client, env.access_codes[case])
                save(client, f"{env.source_base_url}/demo/source/{case}")
    print("DEMO ONLY: fixed clock; mock extraction and reachability; isolated local database.", flush=True)
    print(f"DEMO_DATABASE={env.database_path}", flush=True)
    for case, code in env.access_codes.items():
        print(f"DEMO_ACCESS_{case.upper()}={code}", flush=True)
    print(f"DEMO_URL=http://127.0.0.1:{args.port}", flush=True)
    import uvicorn

    try:
        uvicorn.run(env.app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        env.engine.dispose()


if __name__ == "__main__":
    main()

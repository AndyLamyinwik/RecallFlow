from datetime import date, datetime, timezone

import httpx
import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import build_engine, initialize_database, schema_version
from app.models import EffortBand, ExtractionErrorType, ExtractionStatus, Intent, SavedItem
from app.repositories import UserRepository
from app.schemas import CaptureInput
from app.services.capture import CaptureErrorCode, CaptureRejected, CaptureService
from app.services.effort import effort_band_v1
from app.services.extraction import (
    ArticleExtractor,
    ExtractionV1Config,
    deterministic_excerpt_v1,
    normalize_text_v1,
)
from app.services.fetch import SafeHttpFetcher


PUBLIC_IP = "93.184.216.34"
NOW = datetime(2026, 9, 14, 16, 30, tzinfo=timezone.utc)  # Sep 15 in Shanghai.


class FrozenClock:
    def now(self) -> datetime:
        return NOW


@pytest.fixture
def session(tmp_path) -> Session:
    engine = build_engine(f"sqlite:///{tmp_path / 'capture.db'}")
    initialize_database(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as database_session:
        yield database_session
    engine.dispose()


@pytest.fixture
def user(session: Session):
    return UserRepository().create(
        session,
        access_code_hash="capture-pilot-code",
        timezone_name="Asia/Shanghai",
    )


def public_resolver(host: str, port: int) -> set[str]:
    assert port in {80, 443}
    return {PUBLIC_IP}


def build_service(
    handler,
    *,
    text_extractor=None,
    resolver=public_resolver,
    peer_ip_getter=lambda response: PUBLIC_IP,
    config: ExtractionV1Config | None = None,
) -> CaptureService:
    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    fetcher = SafeHttpFetcher(
        client=client,
        resolver=resolver,
        peer_ip_getter=peer_ip_getter,
    )
    kwargs = {"config": config}
    if text_extractor is not None:
        kwargs["text_extractor"] = text_extractor
    extractor = ArticleExtractor(fetcher, **kwargs)
    return CaptureService(
        extractor,
        fingerprint_secret="test-only-fingerprint-secret",
        clock=FrozenClock(),
    )


def response_handler(
    *,
    body: str = "<html><head><title>Useful article</title></head><body></body></html>",
    status: int = 200,
    content_type: str = "text/html; charset=utf-8",
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"content-type": content_type},
            content=body.encode(),
            request=request,
        )

    return handler


def adequate_text(length: int = 1_200) -> str:
    unit = "This paragraph contains grounded article information for a deterministic extraction test."
    pieces = []
    while len("\n\n".join(pieces)) < length:
        pieces.append(unit)
    return "\n\n".join(pieces)[:length]


def test_normal_article_capture_defaults_to_read_and_local_plus_seven(session: Session, user) -> None:
    html = """<html><head><title>Grounded systems</title></head><body>
    <article><p>This is a detailed first paragraph about dependable software systems and their boundaries.</p>
    <p>This second paragraph explains observable failures with enough concrete detail for an article.</p>
    <p>This third paragraph describes deterministic recovery behavior and careful validation.</p></article>
    </body></html>"""
    service = build_service(response_handler(body=html), text_extractor=lambda *args, **kwargs: adequate_text())

    result = service.capture(session, user_id=user.id, payload=CaptureInput(url=" HTTPS://Example.COM/article#section "))

    assert result.created is True
    assert result.item.source_url == "https://example.com/article"
    assert result.item.title == "Grounded systems"
    assert result.item.source_domain == "example.com"
    assert result.item.intent is Intent.READ
    assert result.item.next_due_on == date(2026, 9, 22)
    assert result.item.schedule_timezone == "Asia/Shanghai"
    assert result.item.extraction_status is ExtractionStatus.SUCCESS
    assert result.item.source_text_hash is not None
    assert result.item.ai_value_line is None
    assert result.item.ai_value_line_status.value == "NOT_ATTEMPTED"


@pytest.mark.parametrize("url", ["notaurl", "ftp://example.com/a", "https://user:pass@example.com/a", "https://example.com:8443/a"])
def test_invalid_urls_are_rejected_without_saving(session: Session, user, url: str) -> None:
    service = build_service(response_handler())
    with pytest.raises(CaptureRejected, match="URL|HTTP|credentials|port"):
        service.capture(session, user_id=user.id, payload=CaptureInput(url=url))
    assert session.scalar(select(func.count()).select_from(SavedItem)) == 0


def test_private_literal_and_private_dns_are_rejected(session: Session, user) -> None:
    service = build_service(response_handler())
    with pytest.raises(CaptureRejected) as literal_error:
        service.capture(session, user_id=user.id, payload=CaptureInput(url="http://127.0.0.1/a"))
    assert literal_error.value.code is CaptureErrorCode.PRIVATE_TARGET

    service = build_service(response_handler(), resolver=lambda host, port: {"10.0.0.8"})
    with pytest.raises(CaptureRejected) as dns_error:
        service.capture(session, user_id=user.id, payload=CaptureInput(url="https://private.example/a"))
    assert dns_error.value.code is CaptureErrorCode.PRIVATE_TARGET
    assert session.scalar(select(func.count()).select_from(SavedItem)) == 0


def test_duplicate_returns_existing_without_a_second_fetch(session: Session, user) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response_handler()(request)

    service = build_service(handler, text_extractor=lambda *args, **kwargs: adequate_text())
    first = service.capture(session, user_id=user.id, payload=CaptureInput(url="https://example.com/article#one"))
    duplicate = service.capture(session, user_id=user.id, payload=CaptureInput(url="https://example.com/article#two"))

    assert first.created is True
    assert duplicate.created is False
    assert duplicate.item.id == first.item.id
    assert calls == 1


def test_unreachable_public_source_is_saved_title_domain_only(session: Session, user) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    result = build_service(unreachable).capture(
        session,
        user_id=user.id,
        payload=CaptureInput(url="https://example.com/offline"),
    )

    assert result.created is True
    assert result.item.title == "example.com"
    assert result.item.extraction_status is ExtractionStatus.FAILED
    assert result.item.extraction_error_type is ExtractionErrorType.NETWORK_ERROR
    assert result.item.effort_band is EffortBand.UNKNOWN
    assert result.item.source_text is None


def test_unsupported_mime_is_saved_without_downloading_as_article(session: Session, user) -> None:
    service = build_service(response_handler(body="%PDF", content_type="application/pdf"))
    result = service.capture(session, user_id=user.id, payload=CaptureInput(url="https://example.com/file.pdf"))

    assert result.item.extraction_status is ExtractionStatus.UNSUPPORTED
    assert result.item.extraction_error_type is ExtractionErrorType.UNSUPPORTED_MIME
    assert result.item.source_text is None


def test_extraction_failure_is_saved_without_invented_content(session: Session, user) -> None:
    service = build_service(response_handler(), text_extractor=lambda *args, **kwargs: None)
    result = service.capture(session, user_id=user.id, payload=CaptureInput(url="https://example.com/js-only"))

    assert result.item.extraction_status is ExtractionStatus.FAILED
    assert result.item.extraction_error_type is ExtractionErrorType.EXTRACTION_FAILED
    assert result.item.source_excerpt is None
    assert result.item.effort_band is EffortBand.UNKNOWN


def test_too_short_article_is_inadequate_with_deterministic_excerpt(session: Session, user) -> None:
    short = "This source is real but too short to qualify as a complete article. " * 5
    service = build_service(response_handler(), text_extractor=lambda *args, **kwargs: short)
    result = service.capture(session, user_id=user.id, payload=CaptureInput(url="https://example.com/short"))

    assert result.item.extraction_status is ExtractionStatus.INADEQUATE
    assert result.item.extraction_error_type is ExtractionErrorType.INADEQUATE_CONTENT
    assert result.item.source_char_count == len(normalize_text_v1(short))
    assert result.item.source_text is None
    assert result.item.source_excerpt == deterministic_excerpt_v1(normalize_text_v1(short))
    assert result.item.effort_band is EffortBand.UNKNOWN


def test_over_50000_characters_discards_body_but_keeps_count_and_excerpt(session: Session, user) -> None:
    long_text = ("A" * 70 + ".\n") * 720
    normalized = normalize_text_v1(long_text)
    service = build_service(response_handler(), text_extractor=lambda *args, **kwargs: long_text)
    result = service.capture(session, user_id=user.id, payload=CaptureInput(url="https://example.com/long"))

    assert len(normalized) > 50_000
    assert result.item.extraction_status is ExtractionStatus.SUCCESS
    assert result.item.source_char_count == len(normalized)
    assert result.item.source_truncated is True
    assert result.item.source_text is None
    assert result.item.source_text_hash is None
    assert result.item.source_excerpt == "A" * 70 + "."
    assert result.item.effort_band is EffortBand.LONG


@pytest.mark.parametrize(
    ("status", "count", "expected"),
    [
        (ExtractionStatus.FAILED, 6_000, EffortBand.UNKNOWN),
        (ExtractionStatus.SUCCESS, 6_000, EffortBand.SHORT),
        (ExtractionStatus.SUCCESS, 6_001, EffortBand.MEDIUM),
        (ExtractionStatus.SUCCESS, 18_000, EffortBand.MEDIUM),
        (ExtractionStatus.SUCCESS, 18_001, EffortBand.LONG),
    ],
)
def test_effort_v1_boundaries(status, count, expected) -> None:
    assert effort_band_v1(extraction_status=status, source_char_count=count) is expected


def test_custom_date_note_and_learn_intent_are_preserved(session: Session, user) -> None:
    service = build_service(response_handler(), text_extractor=lambda *args, **kwargs: adequate_text())
    result = service.capture(
        session,
        user_id=user.id,
        payload=CaptureInput(
            url="https://example.com/learn",
            intent=Intent.LEARN,
            revisit_on=date(2026, 9, 15),
            why_saved_note="  Needed for the design review.  ",
        ),
    )

    assert result.item.intent is Intent.LEARN
    assert result.item.next_due_on == date(2026, 9, 15)
    assert result.item.why_saved_note == "Needed for the design review."


def test_past_custom_date_is_rejected_before_fetch(session: Session, user) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response_handler()(request)

    service = build_service(handler)
    with pytest.raises(CaptureRejected) as error:
        service.capture(
            session,
            user_id=user.id,
            payload=CaptureInput(url="https://example.com/past", revisit_on=date(2026, 9, 14)),
        )
    assert error.value.code is CaptureErrorCode.INVALID_REVISIT_DATE
    assert calls == 0


def test_private_redirect_is_saved_as_typed_fetch_failure(session: Session, user) -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"}, request=request)

    result = build_service(redirect).capture(
        session,
        user_id=user.id,
        payload=CaptureInput(url="https://example.com/redirect"),
    )
    assert result.item.extraction_status is ExtractionStatus.FAILED
    assert result.item.extraction_error_type is ExtractionErrorType.REDIRECT_POLICY


def test_missing_peer_ip_fails_closed_and_saves_failure(session: Session, user) -> None:
    service = build_service(response_handler(), peer_ip_getter=lambda response: None)
    result = service.capture(session, user_id=user.id, payload=CaptureInput(url="https://example.com/no-peer"))

    assert result.item.extraction_status is ExtractionStatus.FAILED
    assert result.item.extraction_error_type is ExtractionErrorType.PEER_IP_UNAVAILABLE


def test_migration_2_upgrades_an_existing_v1_database(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'old-v1.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL, applied_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql("CREATE TABLE saved_items (id VARCHAR(36) PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, access_code_hash VARCHAR(255) NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO schema_version(version, name, applied_at) VALUES (1, 'minimum_data_model', '2026-09-14 00:00:00')"
        )

    initialize_database(engine)
    initialize_database(engine)

    with engine.connect() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("saved_items")}
        versions = connection.execute(select(schema_version.c.version)).scalars().all()
    assert {
        "source_text_hash",
        "source_char_count",
        "source_truncated",
        "extraction_error_type",
        "extraction_version",
            "effort_rule_version",
            "schedule_timezone",
            "last_source_opened_at",
        } <= columns
    assert versions == [1, 2, 3, 4]
    engine.dispose()

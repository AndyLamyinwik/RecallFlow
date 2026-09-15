from datetime import date, datetime, timezone
from hashlib import sha256
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import build_engine, initialize_database
from app.main import create_app
from app.models import EffortBand, ExtractionStatus, SavedItem, SavedItemStatus, User
from app.repositories import SavedItemRepository, UserRepository, source_url_fingerprint
from app.security import (
    PilotAccessService,
    fingerprint_access_code,
    hash_access_code,
)
from app.services import CaptureService
from app.services.extraction import ArticleExtraction


ACCESS_CODE = "pilot-code-with-enough-entropy-123"
NOW = datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


class FakeExtractor:
    def __init__(self) -> None:
        self.calls = 0
        self.title = "A useful systems article"
        self.note_snapshot = "PRIVATE SOURCE SNAPSHOT " + "grounded text " * 80

    def extract(self, normalized_url: str) -> ArticleExtraction:
        self.calls += 1
        return ArticleExtraction(
            title=self.title,
            source_domain="example.com",
            source_text=self.note_snapshot,
            source_text_hash=sha256(self.note_snapshot.encode()).hexdigest(),
            source_char_count=len(self.note_snapshot),
            source_truncated=False,
            source_excerpt="A short, grounded excerpt that helps identify the saved article later.",
            extraction_status=ExtractionStatus.SUCCESS,
            extraction_error_type=None,
            extraction_version="extraction/v1",
            effort_band=EffortBand.SHORT,
            effort_rule_version="effort_rule/v1",
        )


@pytest.fixture
def ui(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'ui.db'}")
    initialize_database(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    extractor = FakeExtractor()
    capture = CaptureService(
        extractor,
        fingerprint_secret="test-fingerprint-secret",
        clock=FrozenClock(),
    )
    settings = Settings(
        app_env="development",
        app_secret="test-session-secret-that-is-long-enough",
        database_url=f"sqlite:///{tmp_path / 'ui.db'}",
        access_codes=ACCESS_CODE,
    )
    app = create_app(
        database_initializer=lambda: None,
        session_factory=factory,
        capture_service=capture,
        app_settings=settings,
        clock=FrozenClock(),
    )
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, factory=factory, extractor=extractor, settings=settings)
    engine.dispose()


def _first_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _login(ui, access_code: str = ACCESS_CODE):
    home = ui.client.get("/")
    token = _first_csrf(home.text)
    return ui.client.post(
        "/session",
        data={"access_code": access_code, "timezone_name": "Asia/Shanghai", "csrf_token": token},
        follow_redirects=False,
    )


def _save(ui, *, url: str = "https://example.com/article", note: str = "Useful for design review"):
    form = ui.client.get("/items/new")
    token = _first_csrf(form.text)
    return ui.client.post(
        "/items",
        data={
            "url": url,
            "intent": "LEARN",
            "revisit_on": "2026-09-22",
            "why_saved_note": note,
            "csrf_token": token,
        },
        follow_redirects=False,
    )


def test_login_requires_bound_csrf_and_invalid_code_creates_no_user(ui) -> None:
    response = ui.client.post(
        "/session",
        data={"access_code": ACCESS_CODE, "timezone_name": "UTC", "csrf_token": "forged"},
    )
    assert response.status_code == 403

    response = _login(ui, "wrong-code-that-is-long-enough")
    assert response.status_code == 401
    assert "could not be used" in response.text
    assert "wrong-code" not in response.text
    with ui.factory() as database:
        assert database.scalar(select(func.count()).select_from(User)) == 0


def test_valid_code_is_scrypt_hashed_and_session_cookie_is_hardened(ui) -> None:
    response = _login(ui)

    assert response.status_code == 303
    assert response.headers["location"] == "/items"
    session_cookie = next(
        value for value in response.headers.get_list("set-cookie") if value.startswith("recallflow_session=")
    )
    assert "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Path=/" in session_cookie
    assert ACCESS_CODE not in session_cookie
    with ui.factory() as database:
        user = database.scalar(select(User))
        assert user is not None
        assert user.access_code_hash.startswith("scrypt$v1$")
        assert ACCESS_CODE not in user.access_code_hash
        assert user.access_code_fingerprint == fingerprint_access_code(
            ACCESS_CODE,
            secret=ui.settings.app_secret,
        )
        assert user.timezone == "Asia/Shanghai"


def test_access_code_fingerprint_is_unique_and_removed_code_cannot_log_in(ui) -> None:
    assert _login(ui).status_code == 303
    with ui.factory() as database:
        user = database.scalar(select(User))
        assert user is not None
        duplicate = User(
            access_code_hash=hash_access_code("another-high-entropy-code-456"),
            access_code_fingerprint=user.access_code_fingerprint,
            timezone="UTC",
        )
        database.add(duplicate)
        with pytest.raises(IntegrityError):
            database.flush()
        database.rollback()

    revoked_service = PilotAccessService(
        (),
        fingerprint_secret=ui.settings.app_secret,
    )
    with ui.factory() as database:
        assert revoked_service.authenticate_or_provision(
            database,
            access_code=ACCESS_CODE,
            timezone_name="Asia/Shanghai",
        ) is None


def test_production_login_and_session_cookies_are_secure(ui) -> None:
    home = ui.client.get("/")
    token = _first_csrf(home.text)
    ui.settings.app_env = "production"
    response = ui.client.post(
        "/session",
        data={
            "access_code": ACCESS_CODE,
            "timezone_name": "Asia/Shanghai",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    session_cookie = next(
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith("recallflow_session=")
    )
    assert "Secure" in session_cookie

    ui.client.cookies.clear()
    login_page = ui.client.get("/")
    login_cookie = next(
        value
        for value in login_page.headers.get_list("set-cookie")
        if value.startswith("recallflow_login_csrf=")
    )
    assert "Secure" in login_cookie


def test_same_code_returns_same_user_and_login_rotates_session(ui) -> None:
    first = _login(ui)
    first_cookie = ui.client.cookies.get("recallflow_session")
    csrf = _first_csrf(ui.client.get("/items").text)
    assert ui.client.post("/session/logout", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    second = _login(ui)
    second_cookie = ui.client.cookies.get("recallflow_session")

    assert first.status_code == second.status_code == 303
    assert first_cookie != second_cookie
    with ui.factory() as database:
        assert database.scalar(select(func.count()).select_from(User)) == 1


def test_anonymous_and_tampered_sessions_cannot_read_items(ui) -> None:
    response = ui.client.get("/items", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    ui.client.cookies.set("recallflow_session", "tampered")
    response = ui.client.get("/items", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_save_list_detail_duplicate_and_refresh_are_idempotent(ui) -> None:
    assert _login(ui).status_code == 303
    saved = _save(ui)

    assert saved.status_code == 303
    assert "notice=saved" in saved.headers["location"]
    detail = ui.client.get(saved.headers["location"])
    assert detail.status_code == 200
    assert "A useful systems article" in detail.text
    assert "Useful for design review" in detail.text
    assert "Short" in detail.text
    assert ui.extractor.note_snapshot not in detail.text
    assert 'rel="noopener noreferrer"' in detail.text

    refreshed = ui.client.get(saved.headers["location"])
    listing = ui.client.get("/items")
    assert refreshed.status_code == listing.status_code == 200
    assert "A useful systems article" in listing.text
    duplicate = _save(ui, url="https://example.com/article#another-fragment")
    assert duplicate.status_code == 303
    assert "notice=duplicate" in duplicate.headers["location"]
    with ui.factory() as database:
        assert database.scalar(select(func.count()).select_from(SavedItem)) == 1
    assert ui.extractor.calls == 1


def test_capture_validation_error_keeps_safe_form_values_and_saves_nothing(ui) -> None:
    _login(ui)
    response = _save(ui, url="http://127.0.0.1/private")

    assert response.status_code == 422
    assert "could not save" in response.text
    assert "Useful for design review" in response.text
    with ui.factory() as database:
        assert database.scalar(select(func.count()).select_from(SavedItem)) == 0


def test_templates_escape_user_and_article_text(ui) -> None:
    ui.extractor.title = '<script>alert("title")</script>'
    _login(ui)
    saved = _save(ui, note='<img src=x onerror="alert(1)">')
    detail = ui.client.get(saved.headers["location"])

    assert "<script>" not in detail.text
    assert "&lt;script&gt;" in detail.text
    assert "<img src=x" not in detail.text
    assert "&lt;img src=x" in detail.text


def test_date_update_accepts_today_and_rejects_past_or_malformed(ui) -> None:
    _login(ui)
    saved = _save(ui)
    item_path = saved.headers["location"].split("?", 1)[0]
    item_id = item_path.rsplit("/", 1)[1]
    detail = ui.client.get(item_path)
    csrf = _first_csrf(detail.text)
    expected_updated_at = re.search(
        r'name="expected_updated_at" value="([^"]+)"', detail.text
    ).group(1)

    response = ui.client.post(
        f"{item_path}/date",
        data={
            "revisit_on": "2026-09-15",
            "expected_updated_at": expected_updated_at,
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with ui.factory() as database:
        item = database.get(SavedItem, item_id)
        assert item is not None
        assert item.next_due_on == date(2026, 9, 15)
        assert item.schedule_timezone == "Asia/Shanghai"

    for invalid in ("2026-09-14", "not-a-date"):
        csrf = _first_csrf(ui.client.get(item_path).text)
        response = ui.client.post(
            f"{item_path}/date",
            data={"revisit_on": invalid, "csrf_token": csrf},
        )
        assert response.status_code == 422
    with ui.factory() as database:
        assert database.get(SavedItem, item_id).next_due_on == date(2026, 9, 15)


def test_dismissed_item_cannot_be_rescheduled_from_saved_item_ui(ui) -> None:
    _login(ui)
    saved = _save(ui)
    item_path = saved.headers["location"].split("?", 1)[0]
    item_id = item_path.rsplit("/", 1)[1]
    with ui.factory() as database:
        item = database.get(SavedItem, item_id)
        assert item is not None
        original_date = item.next_due_on
        item.status = SavedItemStatus.DISMISSED
        database.commit()

    detail = ui.client.get(item_path)
    assert "cannot be changed" in detail.text
    csrf = _first_csrf(detail.text)
    response = ui.client.post(
        f"{item_path}/date",
        data={"revisit_on": "2026-09-30", "csrf_token": csrf},
    )
    assert response.status_code == 409
    with ui.factory() as database:
        assert database.get(SavedItem, item_id).next_due_on == original_date


def test_owner_scope_hides_foreign_item_from_list_detail_and_writes(ui) -> None:
    _login(ui)
    with ui.factory() as database:
        foreign = UserRepository().create(
            database,
            access_code_hash=hash_access_code("different-pilot-code-456789"),
            timezone_name="UTC",
        )
        foreign_item = SavedItemRepository().create(
            database,
            user_id=foreign.id,
            source_url="https://example.org/private-path?token=secret",
            source_url_hash=source_url_fingerprint(
                user_id=foreign.id,
                normalized_url="https://example.org/private-path?token=secret",
                secret="test-fingerprint-secret",
            ),
            title="Foreign private title",
            source_domain="example.org",
            source_text="foreign private body",
            next_due_on=date(2026, 9, 22),
            schedule_timezone="UTC",
        ).item
        foreign_id = foreign_item.id
        database.commit()

    listing = ui.client.get("/items")
    assert "Foreign private title" not in listing.text
    assert ui.client.get(f"/items/{foreign_id}").status_code == 404
    csrf = _first_csrf(listing.text)
    assert ui.client.post(
        f"/items/{foreign_id}/date",
        data={"revisit_on": "2026-09-23", "csrf_token": csrf},
    ).status_code == 404
    assert ui.client.post(
        f"/items/{foreign_id}/delete",
        data={"csrf_token": csrf},
    ).status_code == 404
    with ui.factory() as database:
        assert database.get(SavedItem, foreign_id) is not None


@pytest.mark.parametrize("endpoint", ["logout", "save", "date", "delete"])
def test_every_authenticated_write_rejects_missing_csrf(ui, endpoint: str) -> None:
    _login(ui)
    saved = _save(ui)
    item_path = saved.headers["location"].split("?", 1)[0]
    requests = {
        "logout": ("/session/logout", {}),
        "save": (
            "/items",
            {"url": "https://example.com/two", "intent": "READ", "revisit_on": "2026-09-22", "why_saved_note": ""},
        ),
        "date": (f"{item_path}/date", {"revisit_on": "2026-09-23"}),
        "delete": (f"{item_path}/delete", {}),
    }
    path, data = requests[endpoint]
    response = ui.client.post(path, data=data, follow_redirects=False)
    assert response.status_code == 403


def test_hard_delete_removes_private_item_and_refresh_does_not_repeat(ui) -> None:
    _login(ui)
    saved = _save(ui)
    item_path = saved.headers["location"].split("?", 1)[0]
    item_id = item_path.rsplit("/", 1)[1]
    csrf = _first_csrf(ui.client.get(item_path).text)

    response = ui.client.post(
        f"{item_path}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/items?notice=deleted"
    assert ui.client.get(item_path).status_code == 404
    assert ui.client.get(response.headers["location"]).status_code == 200
    with ui.factory() as database:
        assert database.get(SavedItem, item_id) is None

"""Capture outcome telemetry covers real route results without private payloads."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import build_engine, initialize_database
from app.main import create_app
from app.models import ActionEvent, ActionEventName, EffortBand, ExtractionStatus, SavedItem
from app.services.capture import CaptureErrorCode, CaptureRejected, CaptureService
from app.services.extraction import ArticleExtraction


NOW = datetime(2026, 10, 26, 9, tzinfo=timezone.utc)
CODE_A = "telemetry-test-access-code-a-123456"
CODE_B = "telemetry-test-access-code-b-123456"


class FixedClock:
    def now(self):
        return NOW


class Extractor:
    def __init__(self):
        self.calls = 0
        self.fallback = False

    def extract(self, url):
        self.calls += 1
        text = None if self.fallback else "PRIVATE SOURCE " * 100
        return ArticleExtraction(
            title="Private title",
            source_domain="example.com",
            source_text=text,
            source_text_hash=sha256(text.encode()).hexdigest() if text else None,
            source_char_count=len(text) if text else None,
            source_truncated=False,
            source_excerpt="Private excerpt" if text else None,
            extraction_status=ExtractionStatus.FAILED if self.fallback else ExtractionStatus.SUCCESS,
            extraction_error_type=None,
            extraction_version="extraction/v1",
            effort_band=EffortBand.UNKNOWN if self.fallback else EffortBand.SHORT,
            effort_rule_version="effort_rule/v1",
        )


class ControlledSession(Session):
    fail_on_commit = None
    commit_calls = 0

    def commit(self):
        ControlledSession.commit_calls += 1
        if ControlledSession.commit_calls == ControlledSession.fail_on_commit:
            raise RuntimeError("Simulated commit failure")
        super().commit()


@pytest.fixture
def telemetry(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'telemetry.db'}")
    initialize_database(engine)
    ControlledSession.commit_calls = 0
    ControlledSession.fail_on_commit = None
    factory = sessionmaker(bind=engine, class_=ControlledSession, autoflush=False, expire_on_commit=False)
    extractor = Extractor()
    capture = CaptureService(extractor, fingerprint_secret="test-only-secret", clock=FixedClock())
    app = create_app(
        database_initializer=lambda: None,
        session_factory=factory,
        capture_service=capture,
        clock=FixedClock(),
        app_settings=Settings(
            _env_file=None,
            app_secret="test-only-session-secret-at-least-32-characters",
            access_codes=f"{CODE_A},{CODE_B}",
        ),
    )
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, app=app, factory=factory, extractor=extractor, capture=capture)
    ControlledSession.fail_on_commit = None
    engine.dispose()


def hidden(html, name):
    match = re.search(rf'name="{name}" value="([^"]+)"', html)
    assert match
    return match.group(1)


def login(client, code=CODE_A):
    home = client.get("/")
    response = client.post("/session", data={
        "csrf_token": hidden(home.text, "csrf_token"),
        "access_code": code,
        "timezone_name": "UTC",
    }, follow_redirects=False)
    assert response.status_code == 303


def form_data(client):
    page = client.get("/items/new")
    assert page.status_code == 200
    return {
        "csrf_token": hidden(page.text, "csrf_token"),
        "capture_operation_id": hidden(page.text, "capture_operation_id"),
        "url": "https://example.com/private-article?token=PRIVATE-URL-TOKEN",
        "intent": "LEARN",
        "revisit_on": "2026-10-27",
        "why_saved_note": "PRIVATE NOTE do not log",
    }


def rows(env):
    with env.factory() as database:
        return list(database.scalars(select(ActionEvent)).all())


def post(client, data):
    return client.post("/items", data=data, follow_redirects=False)


def test_form_uuid_and_success_events_are_correlated_and_private(telemetry):
    login(telemetry.client)
    data = form_data(telemetry.client)
    assert str(UUID(data["capture_operation_id"])) == data["capture_operation_id"]
    response = post(telemetry.client, data)
    assert response.status_code == 303
    events = rows(telemetry)
    assert {e.event_name for e in events} == {ActionEventName.CAPTURE_STARTED, ActionEventName.CAPTURE_SUCCEEDED}
    assert {e.correlation_id for e in events} == {data["capture_operation_id"]}
    assert {e.occurred_at for e in events} == {NOW}
    assert len({e.idempotency_key for e in events}) == 2
    started = next(e for e in events if e.event_name == ActionEventName.CAPTURE_STARTED)
    success = next(e for e in events if e.event_name == ActionEventName.CAPTURE_SUCCEEDED)
    assert started.actor.value == "USER" and success.actor.value == "SYSTEM"
    assert started.item_id is None and success.item_id
    assert len(started.properties["input_fingerprint"]) == 64
    assert success.properties == {"created": True, "extraction_status": "SUCCESS"}
    serialized = json.dumps([e.properties for e in events])
    for private in (data["url"], "PRIVATE", "Private title", CODE_A, "test-only-session-secret"):
        assert private not in serialized


def test_exact_retry_replays_result_and_does_not_extract_or_log_twice(telemetry):
    login(telemetry.client)
    data = form_data(telemetry.client)
    first, repeated = post(telemetry.client, data), post(telemetry.client, data)
    assert first.status_code == repeated.status_code == 303
    assert first.headers["location"] == repeated.headers["location"]
    assert telemetry.extractor.calls == 1
    assert len(rows(telemetry)) == 2


def test_same_operation_reused_with_different_payload_is_rejected(telemetry):
    login(telemetry.client)
    data = form_data(telemetry.client)
    assert post(telemetry.client, data).status_code == 303
    changed = {**data, "why_saved_note": "A different intent"}
    assert post(telemetry.client, changed).status_code == 409
    assert len(rows(telemetry)) == 2
    assert telemetry.extractor.calls == 1


def test_distinct_duplicate_save_is_success_without_second_extraction(telemetry):
    login(telemetry.client)
    assert post(telemetry.client, form_data(telemetry.client)).status_code == 303
    duplicate = post(telemetry.client, form_data(telemetry.client))
    assert duplicate.status_code == 303 and "notice=duplicate" in duplicate.headers["location"]
    successes = [e for e in rows(telemetry) if e.event_name == ActionEventName.CAPTURE_SUCCEEDED]
    assert len(successes) == 2
    assert sorted(e.properties["created"] for e in successes) == [False, True]
    assert telemetry.extractor.calls == 1


def test_extraction_fallback_remains_successful_save(telemetry):
    telemetry.extractor.fallback = True
    login(telemetry.client)
    assert post(telemetry.client, form_data(telemetry.client)).status_code == 303
    successes = [e for e in rows(telemetry) if e.event_name == ActionEventName.CAPTURE_SUCCEEDED]
    assert len(successes) == 1 and successes[0].properties["extraction_status"] == "FAILED"
    assert not any(e.event_name == ActionEventName.CAPTURE_FAILED for e in rows(telemetry))


@pytest.mark.parametrize("change,reason", [
    ({"url": "http://127.0.0.1/private"}, "PRIVATE_TARGET"),
    ({"intent": "invalid"}, "VALIDATION"),
    ({"revisit_on": "2020-01-01"}, "INVALID_REVISIT_DATE"),
])
def test_rejected_capture_persists_failure_and_issues_new_form_id(telemetry, change, reason):
    login(telemetry.client)
    data = {**form_data(telemetry.client), **change}
    response = post(telemetry.client, data)
    assert response.status_code == 422
    assert hidden(response.text, "capture_operation_id") != data["capture_operation_id"]
    repeated = post(telemetry.client, data)
    assert repeated.status_code == 422
    events = rows(telemetry)
    assert len(events) == 2
    failure = next(e for e in events if e.event_name == ActionEventName.CAPTURE_FAILED)
    assert failure.properties == {"reason": reason}
    assert failure.item_id is None
    with telemetry.factory() as database:
        assert database.scalar(select(SavedItem)) is None


def test_service_work_rolls_back_before_failure_is_persisted(telemetry, monkeypatch):
    login(telemetry.client)
    original = telemetry.capture.capture

    def failing_capture(database, **kwargs):
        original(database, **kwargs)
        raise CaptureRejected(CaptureErrorCode.INVALID_URL, "PRIVATE error must not be logged")

    monkeypatch.setattr(telemetry.capture, "capture", failing_capture)
    assert post(telemetry.client, form_data(telemetry.client)).status_code == 422
    assert len(rows(telemetry)) == 2
    with telemetry.factory() as database:
        assert database.scalar(select(SavedItem)) is None


def test_commit_failure_is_incomplete_and_never_claimed_as_success(telemetry):
    login(telemetry.client)
    data = form_data(telemetry.client)
    ControlledSession.fail_on_commit = ControlledSession.commit_calls + 2
    with pytest.raises(RuntimeError, match="Simulated commit failure"):
        post(telemetry.client, data)
    ControlledSession.fail_on_commit = None
    assert [e.event_name for e in rows(telemetry)] == [ActionEventName.CAPTURE_STARTED]
    with telemetry.factory() as database:
        assert database.scalar(select(SavedItem)) is None
    assert post(telemetry.client, data).status_code == 409


def test_auth_and_csrf_rejections_do_not_create_capture_events(telemetry):
    assert post(telemetry.client, {"url": "https://example.com/a", "intent": "READ", "revisit_on": "2026-10-27"}).status_code == 303
    assert rows(telemetry) == []
    login(telemetry.client)
    data = form_data(telemetry.client)
    assert post(telemetry.client, {**data, "csrf_token": "invalid"}).status_code == 403
    assert rows(telemetry) == []


@pytest.mark.parametrize("operation", ["invalid", "x" * 1000])
def test_invalid_operation_id_is_bounded_and_not_admitted(telemetry, operation):
    login(telemetry.client)
    data = {**form_data(telemetry.client), "capture_operation_id": operation}
    assert post(telemetry.client, data).status_code == 422
    assert rows(telemetry) == []


def test_legacy_client_without_id_still_saves(telemetry):
    login(telemetry.client)
    data = form_data(telemetry.client)
    del data["capture_operation_id"]
    assert post(telemetry.client, data).status_code == 303
    assert len(rows(telemetry)) == 2


def test_same_operation_uuid_is_scoped_to_user(telemetry):
    login(telemetry.client)
    data_a = form_data(telemetry.client)
    assert post(telemetry.client, data_a).status_code == 303
    with TestClient(telemetry.app) as second:
        login(second, CODE_B)
        data_b = {**form_data(second), "capture_operation_id": data_a["capture_operation_id"]}
        assert post(second, data_b).status_code == 303
    events = rows(telemetry)
    assert len(events) == 4 and len({e.user_id for e in events}) == 2
    assert {e.correlation_id for e in events} == {data_a["capture_operation_id"]}


def test_unexpected_service_error_leaves_only_started_event(telemetry, monkeypatch):
    login(telemetry.client)
    data = form_data(telemetry.client)

    def crash(*args, **kwargs):
        raise RuntimeError("Synthetic process failure")

    monkeypatch.setattr(telemetry.capture, "capture", crash)
    with pytest.raises(RuntimeError, match="Synthetic process failure"):
        post(telemetry.client, data)
    assert [e.event_name for e in rows(telemetry)] == [ActionEventName.CAPTURE_STARTED]
    assert post(telemetry.client, data).status_code == 409

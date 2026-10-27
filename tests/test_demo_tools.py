from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from scripts.demo_app import create_demo_environment
from scripts.generate_demo_usage import csrf, generate, login, mounted_card, save


def test_generator_runs_real_routes_and_deduplicates_replays(tmp_path: Path) -> None:
    output = generate(tmp_path / "usage-demo.db")
    assert output["data_kind"] == "demo"
    assert output["users"] == 3
    assert output["saved_items"] == 4
    assert output["cards"] == 3
    assert output["event_counts"] == {
        "capture_started": 6,
        "capture_succeeded": 5,
        "capture_failed": 1,
        "dismiss_undone": 1,
        "dismissed": 1,
        "later_confirmed": 1,
        "open_attempt": 2,
        "open_failed": 1,
        "resurfacing_impression": 3,
        "source_opened": 1,
    }


def test_harness_refuses_existing_and_non_demo_database(tmp_path: Path) -> None:
    existing = tmp_path / "existing-demo.db"
    existing.write_bytes(b"preserve this")
    with pytest.raises(FileExistsError):
        create_demo_environment(existing)
    assert existing.read_bytes() == b"preserve this"
    with pytest.raises(ValueError):
        create_demo_environment(tmp_path / "production.db")
    assert not (tmp_path / "production.db").exists()


def test_harness_ignores_dotenv_and_never_opens_its_database(tmp_path: Path, monkeypatch) -> None:
    production_file = tmp_path / "private.db"
    (tmp_path / ".env").write_text(
        f"DATABASE_URL=sqlite:///{production_file.as_posix()}\nDISMISS_UNDO_WINDOW_SECONDS=0\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    env = create_demo_environment(tmp_path / "isolated-demo.db")
    try:
        assert env.settings.dismiss_undo_window_seconds == 30
        assert env.settings.database_url.endswith("isolated-demo.db")
        assert not production_file.exists()
    finally:
        env.engine.dispose()


def test_three_accounts_have_separate_daily_slots_and_banner(tmp_path: Path) -> None:
    from app.models import ActionEvent, ActionEventName, ResurfacingEvent, User

    env = create_demo_environment(tmp_path / "browser-demo.db")
    try:
        for case in ("open", "later", "dismiss"):
            with TestClient(env.app) as client:
                home = client.get("/")
                assert "DEMO DATA" in home.text
                login(client, env.access_codes[case])
                assert save(client, f"https://example.com/demo/source/{case}").status_code == 303
                mounted_card(client)
                for _ in range(2):
                    assert client.get("/items").status_code == 200
        with env.factory() as database:
            assert len(database.scalars(select(User)).all()) == 3
            assert len(database.scalars(select(ResurfacingEvent)).all()) == 3
            impressions = database.scalars(
                select(ActionEvent).where(ActionEvent.event_name == ActionEventName.RESURFACING_IMPRESSION)
            ).all()
            assert len(impressions) == 3
        assert env.checker.calls == {}
    finally:
        env.engine.dispose()


def test_source_page_and_fallback_are_explicitly_synthetic(tmp_path: Path) -> None:
    from app.models import ExtractionStatus, SavedItem

    env = create_demo_environment(tmp_path / "source-demo.db")
    try:
        with TestClient(env.app) as client:
            source = client.get("/demo/source/open")
            assert "synthetic article content" in source.text
            assert "not a fetched source" in source.text
            assert "DEMO DATA" in source.text
            login(client, env.access_codes["open"])
            assert save(client, "https://example.com/demo/source/fallback").status_code == 303
        with env.factory() as database:
            item = database.scalar(select(SavedItem))
            assert item.extraction_status is ExtractionStatus.FAILED
            assert item.source_text is None
    finally:
        env.engine.dispose()

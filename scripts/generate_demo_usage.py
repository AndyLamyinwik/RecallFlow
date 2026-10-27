"""Generate clearly synthetic usage using the actual RecallFlow HTTP routes.

This is a scripted fixture, never human usage or a usability study. Every event
is emitted by an actual production route; no analytics event is backfilled.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
from pathlib import Path
import re
from typing import Any

from scripts.demo_app import DEMO_NOW, create_demo_environment


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not match:
        raise RuntimeError("Normal page did not provide a CSRF token")
    return match.group(1)


def login(client: Any, code: str) -> None:
    home = client.get("/")
    response = client.post(
        "/session",
        data={"csrf_token": csrf(home.text), "access_code": code, "timezone_name": "UTC"},
        follow_redirects=False,
    )
    if response.status_code != 303:
        raise RuntimeError(f"Demo login failed: {response.status_code}")


def save(client: Any, url: str, *, revisit_on: str = "2026-10-26", note: str = "DEMO — revisit this when useful", replay: bool = False) -> Any:
    page = client.get("/items/new")
    data = {
        "csrf_token": csrf(page.text),
        "url": url,
        "intent": "LEARN",
        "revisit_on": revisit_on,
        "why_saved_note": note,
    }
    operation = re.search(r'name="capture_operation_id" value="([^"]+)"', page.text)
    if operation:
        data["capture_operation_id"] = operation.group(1)
    response = client.post(
        "/items",
        data=data,
        follow_redirects=False,
    )
    if replay:
        if not operation:
            raise RuntimeError("Capture-operation telemetry is required for a safe replay scenario")
        retried = client.post("/items", data=data, follow_redirects=False)
        assert retried.status_code == response.status_code
        assert retried.headers.get("location") == response.headers.get("location")
    return response


def mounted_card(client: Any) -> tuple[str, str]:
    page = client.get("/items")
    match = re.search(r'action="/resurfacing/([^/]+)/impression"', page.text)
    if not match:
        raise RuntimeError("Expected a new due card with mount acknowledgement")
    event_id = match.group(1)
    token = csrf(page.text)
    mount = client.post(f"/resurfacing/{event_id}/impression", data={"csrf_token": token})
    if mount.status_code != 204:
        raise RuntimeError("Card mount acknowledgement failed")
    # Replay the mount and refresh: neither may create another impression.
    assert client.post(f"/resurfacing/{event_id}/impression", data={"csrf_token": token}).status_code == 204
    assert client.get("/items").status_code == 200
    return event_id, token


def generate(database_path: Path) -> dict[str, Any]:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    env = create_demo_environment(database_path, show_banner=False)
    from app.models import ActionEvent, ResurfacingEvent, SavedItem, User
    try:
        for case in ("open", "later", "dismiss"):
            with TestClient(env.app) as client:
                login(client, env.access_codes[case])
                url_case = "open-fail-once" if case == "open" else case
                response = save(client, f"https://example.com/demo/source/{url_case}", replay=case == "open")
                assert response.status_code == 303
                # A second, future fallback item illustrates save != extraction.
                if case == "later":
                    assert save(client, "https://example.com/demo/source/fallback", revisit_on="2026-11-02").status_code == 303
                    # A distinct save of the same URL is a successful duplicate
                    # result, not a second saved item. It is not a request replay.
                    assert save(client, f"https://example.com/demo/source/{url_case}").status_code == 303
                    # This actual route records a terminal validation rejection.
                    assert save(client, "http://127.0.0.1/private").status_code == 422
                event_id, token = mounted_card(client)
                env.clock.current += timedelta(seconds=1)
                base_data = {"csrf_token": token}
                if case == "open":
                    path = f"/resurfacing/{event_id}/open"
                    failed_data = {**base_data, "idempotency_key": "demo-open-failed-001"}
                    assert client.post(path, data=failed_data, follow_redirects=False).status_code == 303
                    # Exact client retry replays, not another logical operation.
                    assert client.post(path, data=failed_data, follow_redirects=False).status_code == 303
                    env.clock.current += timedelta(seconds=1)
                    success_data = {**base_data, "idempotency_key": "demo-open-success-002"}
                    opened = client.post(path, data=success_data, follow_redirects=False)
                    assert opened.status_code == 303 and opened.headers["location"] == f"https://example.com/demo/source/{url_case}"
                    assert client.post(path, data=success_data, follow_redirects=False).status_code == 303
                elif case == "later":
                    path = f"/resurfacing/{event_id}/later"
                    data = {**base_data, "idempotency_key": "demo-later-choice-001", "choice": "plus_3"}
                    assert client.post(path, data=data, follow_redirects=False).status_code == 303
                    assert client.post(path, data=data, follow_redirects=False).status_code == 303
                else:
                    path = f"/resurfacing/{event_id}/dismiss"
                    data = {**base_data, "idempotency_key": "demo-dismiss-choice-001"}
                    assert client.post(path, data=data, follow_redirects=False).status_code == 303
                    assert client.post(path, data=data, follow_redirects=False).status_code == 303
                    env.clock.current += timedelta(seconds=1)
                    undo = client.post(
                        f"/resurfacing/{event_id}/dismiss/undo",
                        data={**base_data, "idempotency_key": "demo-dismiss-undo-001"},
                        follow_redirects=False,
                    )
                    assert undo.status_code == 303
                    # Restore UI without acknowledging a fresh card impression.
                    assert client.get(undo.headers["location"]).status_code == 200
                env.clock.current += timedelta(seconds=1)
        with env.factory() as database:
            names = Counter(event.event_name.value for event in database.scalars(select(ActionEvent)))
            return {
                "data_kind": "demo",
                "scenario": "Scripted HTTP-route fixture; not human usage; transports mocked",
                "start": DEMO_NOW.isoformat(),
                "end": env.clock.current.isoformat(),
                "users": len(database.scalars(select(User)).all()),
                "saved_items": len(database.scalars(select(SavedItem)).all()),
                "cards": len(database.scalars(select(ResurfacingEvent)).all()),
                "event_counts": dict(sorted(names.items())),
                "capture_telemetry": "Emitted by actual production save routes; no analytics rows inserted by this generator",
            }
    finally:
        env.engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/recallflow-usage-demo.db"))
    args = parser.parse_args()
    data_root = (Path(__file__).resolve().parents[1] / "data").resolve()
    if not args.database.resolve().is_relative_to(data_root):
        parser.error("Demo CLI databases must stay within this project's ignored data/ directory")
    import json

    print(json.dumps(generate(args.database), indent=2))


if __name__ == "__main__":
    main()

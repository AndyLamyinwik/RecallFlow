from fastapi.testclient import TestClient

from app.main import create_app


app = create_app(database_initializer=lambda: None)


def test_home_returns_recallflow_page() -> None:
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "RecallFlow" in response.text
    assert "Save now. Decide later." in response.text


def test_health_returns_ok() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

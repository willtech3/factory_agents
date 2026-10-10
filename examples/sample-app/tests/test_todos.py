import pytest
from fastapi.testclient import TestClient

from app.main import app, store


@pytest.fixture(autouse=True)
def clean():
    store.clear()
    yield
    store.clear()


@pytest.fixture
def client():
    return TestClient(app)


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_list_complete_delete(client):
    r = client.post("/todos", json={"title": "Write the outcome"})
    assert r.status_code == 201
    todo = r.json()
    assert todo["id"] == 1 and todo["done"] is False and todo["created_at"]
    assert [t["title"] for t in client.get("/todos").json()] == ["Write the outcome"]
    assert client.post("/todos/1/complete").json()["done"] is True
    assert client.delete("/todos/1").status_code == 204
    assert client.get("/todos").json() == []


def test_validation_and_missing(client):
    assert client.post("/todos", json={"title": ""}).status_code == 422
    assert client.post("/todos/99/complete").status_code == 404
    assert client.delete("/todos/99").status_code == 404

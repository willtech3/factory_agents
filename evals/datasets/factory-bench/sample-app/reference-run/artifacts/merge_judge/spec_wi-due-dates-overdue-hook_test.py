from dataclasses import asdict
import pytest
from fastapi.testclient import TestClient
from app import storage
from app.main import app, store

@pytest.fixture(autouse=True)
def clean():
    store.clear(); yield; store.clear()

def test_is_overdue_hook(monkeypatch):
    orig = storage.Todo.is_overdue
    fired = []
    def wrapped(self, today=None):
        r = orig(self, today); fired.append(store.complete(1) is not None); return r
    monkeypatch.setattr(storage.Todo, "is_overdue", wrapped)
    r = TestClient(app).post("/todos", json={"title": "late", "due_date": "2000-01-01"})
    assert fired == [True]
    t = r.json(); assert t["overdue"] is (not t["done"]), t

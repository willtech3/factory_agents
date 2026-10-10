"""Scratch test (reviewer's, not part of the change): the F-1 consistency check at both hook points.

Run from a checkout root:  python -m pytest -q -p no:cacheprovider /mnt/session/outputs/probes/scratch_test_f1_both_orders.py
Hook 'asdict' is the implementer's test (complete() lands right after asdict reads the fields).
Hook 'is_overdue' completes the todo right after is_overdue() returns, which is the interleaving that tears an
implementation computing overdue from self before asdict(self).
"""
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.main import app, store


@pytest.fixture(autouse=True)
def clean():
    store.clear()
    yield
    store.clear()


@pytest.mark.parametrize("hook", ["asdict", "is_overdue"])
def test_post_response_consistent_whichever_read_comes_first(monkeypatch, hook):
    fired = []
    if hook == "asdict":
        def wrapped(obj):
            fields = asdict(obj)
            fired.append(store.complete(1) is not None)
            return fields

        monkeypatch.setattr("app.storage.asdict", wrapped)
    else:
        original = storage.Todo.is_overdue

        def wrapped(self, today=None):
            result = original(self, today)
            fired.append(store.complete(1) is not None)
            return result

        monkeypatch.setattr(storage.Todo, "is_overdue", wrapped)
    r = TestClient(app).post("/todos", json={"title": "late", "due_date": "2000-01-01"})
    assert r.status_code == 201 and fired == [True]
    todo = r.json()
    assert todo["overdue"] is (not todo["done"]), todo

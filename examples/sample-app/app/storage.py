"""In-memory storage. Deliberately simple: a dict guarded by a lock, reset between tests."""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Todo:
    id: int
    title: str
    done: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TodoStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[int, Todo] = {}
        self._next = 1

    def list(self) -> list[Todo]:
        with self._lock:
            return sorted(self._items.values(), key=lambda t: t.id)

    def create(self, title: str) -> Todo:
        with self._lock:
            todo = Todo(id=self._next, title=title)
            self._items[todo.id] = todo
            self._next += 1
            return todo

    def get(self, todo_id: int) -> Todo | None:
        with self._lock:
            return self._items.get(todo_id)

    def complete(self, todo_id: int) -> Todo | None:
        with self._lock:
            todo = self._items.get(todo_id)
            if todo is not None:
                todo.done = True
            return todo

    def delete(self, todo_id: int) -> bool:
        with self._lock:
            return self._items.pop(todo_id, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._next = 1

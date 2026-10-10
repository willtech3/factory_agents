"""A small todo API: the factory's sample target.

Endpoints: GET /health, GET /todos, POST /todos, POST /todos/{id}/complete, DELETE /todos/{id}.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .storage import TodoStore

app = FastAPI(title="Sample todo service", version="0.1.0")
store = TodoStore()


class TodoIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/todos")
def list_todos() -> list[dict]:
    return [t.to_dict() for t in store.list()]


@app.post("/todos", status_code=201)
def create_todo(body: TodoIn) -> dict:
    return store.create(body.title).to_dict()


@app.post("/todos/{todo_id}/complete")
def complete_todo(todo_id: int) -> dict:
    todo = store.complete(todo_id)
    if todo is None:
        raise HTTPException(404, "no such todo")
    return todo.to_dict()


@app.delete("/todos/{todo_id}", status_code=204)
def delete_todo(todo_id: int) -> None:
    if not store.delete(todo_id):
        raise HTTPException(404, "no such todo")

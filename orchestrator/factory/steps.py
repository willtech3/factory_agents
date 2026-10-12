"""Durable steps (ADR-0013): the units a run's pipeline is made of, each with a recorded result.

`run_step(ctx, key, inputs, fn)` runs `fn` once per key and inputs. A step that finished with the same inputs returns
its recorded result without running, so `factory resume` replays the pipeline from the top and only the steps that did
not finish run again:

- a step still `running` (its controller died) runs again in the same attempt;
- a step that `failed` (an exception, a budget pause, a stop) runs again as a new attempt;
- a step whose inputs changed (a recovery decision is one of its inputs) runs again as a new attempt.

A step that runs again gets the sessions it already finished back from their own steps; only a session whose output
was rejected (`stages/common.require_json`) runs again.

What makes running a step again safe is in the steps themselves, not here. Everything the pipeline does between steps
is a pure function of earlier steps' results. A step that runs again is idempotent where it touches the outside world:
a session step reattaches to or reuses its session, a gate finds the gate it opened, and `open_change_request` finds the
change request.

A result goes through `encode` and `decode` on the live path as well as on replay, so both paths see the same values.
The current step's key is a context variable: a gate is identified within the step that opens it, and a worker thread
started inside a step gets the key through `carry`.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:  # pragma: no cover
    from .stages import RunContext

T = TypeVar("T")
ADOPTED = "adopted"     # the inputs hash of a step recorded from an older controller's log (`runner.adopt_legacy_run`)

_SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar("factory_step_scope", default=None)


def scope() -> str | None:
    """The key of the step the caller runs in, or None outside every step."""
    return _SCOPE.get()


def inputs_sha(inputs: Any) -> str:
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()


def run_step(ctx: RunContext, key: str, inputs: Any, fn: Callable[[], T], *, kind: str = "task",
             encode: Callable[[T], Any] | None = None, decode: Callable[[Any], T] | None = None) -> T:
    """`fn()` as the durable step `key`, or its recorded result when the step already finished with the same `inputs`."""
    store, run_id = ctx.store, ctx.run_id
    sha = inputs_sha(inputs)
    prior = store.step(run_id, key)
    if prior is not None and prior.status == "done" and prior.inputs_sha in (sha, ADOPTED):
        return decode(prior.result) if decode else prior.result
    if prior is None or prior.status != "running" or prior.inputs_sha != sha:
        store.begin_step(run_id, key, kind, sha)
    scope_token = _SCOPE.set(key)
    try:
        value = fn()
    except BaseException as e:
        store.fail_step(run_id, key, f"{type(e).__name__}: {str(e)[:500]}")
        raise
    finally:
        _SCOPE.reset(scope_token)
    result = encode(value) if encode else value
    store.finish_step(run_id, key, result)
    return decode(result) if decode else result


def recorded(ctx: RunContext, key: str, inputs: Any) -> bool:
    """`run_step(ctx, key, inputs, ...)` would return a recorded result rather than run."""
    prior = ctx.store.step(ctx.run_id, key)
    return prior is not None and prior.status == "done" and prior.inputs_sha in (inputs_sha(inputs), ADOPTED)


def record_step(ctx: RunContext, key: str, result: Any, *, kind: str = "task") -> None:
    """Record `key` as done with `result`, matching any inputs: how an older controller's log is adopted."""
    ctx.store.begin_step(ctx.run_id, key, kind, ADOPTED)
    ctx.store.finish_step(ctx.run_id, key, result)


def carry(fn: Callable[..., T]) -> Callable[..., T]:
    """`fn` for a worker thread: it runs in the caller's step (a thread pool does not copy context variables)."""
    key = _SCOPE.get()

    def run(*args: Any, **kwargs: Any) -> T:
        token = _SCOPE.set(key)
        try:
            return fn(*args, **kwargs)
        finally:
            _SCOPE.reset(token)
    return run


def put_json(ctx: RunContext, value: Any) -> str:
    """Keep a JSON value as a blob of the run; returns its sha256. Key order is kept: a spec written back out from its
    blob must be the same bytes the owner was shown."""
    return ctx.store.put_blob(ctx.run_id, json.dumps(value).encode())


def get_json(ctx: RunContext, sha: str) -> Any:
    return json.loads(ctx.store.get_blob(ctx.run_id, sha))

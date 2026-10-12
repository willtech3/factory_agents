"""Driver for one Managed Agents session per stage.

Follows the client patterns in the Managed Agents docs: stream first, then send;
answer `agent.custom_tool_use` with `user.custom_tool_result`; answer paused tool
calls (`evaluated_permission == "ask"`) by denying when unattended; break on idle only
when `stop_reason.type != "requires_action"`; fetch outputs from `/mnt/session/outputs/`
via the Files API with `scope_id`.

Uses `client.beta.{agents,sessions,files}` (anthropic >= 1.0). It has driven every stage of the
live runs; the reconnect, pagination and acknowledgement handling below come from what those runs
hit (docs/postmortems/, docs/audits/2026-10-10-factory-review.md).
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from .config import Config

# Seconds a short platform call (upload, create, send, list, download) may wait for a response before the SDK
# retries it. The client default is 600 s: in live run 2 one reviewer's session creation hung for exactly that
# long before the retry succeeded, which held the whole review round. Streams keep the client default.
SHORT_CALL_TIMEOUT = 120.0

# A dropped event stream is reopened, not fatal. These are the transport errors httpx and the SDK raise for it.
import httpx

STREAM_RETRYABLE: tuple[type[BaseException], ...] = (httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout, httpx.ConnectError,
                                                    httpx.WriteError, anthropic.APIConnectionError, anthropic.APITimeoutError)
STREAM_MAX_RECONNECTS = 40           # with the 2..30 s backoff: well over an hour of retrying before giving up

CustomToolHandler = Callable[[dict[str, Any], "SessionContext"], str]


@dataclass
class SessionContext:
    session_id: str
    run_id: str
    stage: str
    work_item_id: str | None = None
    custom_tool_use_id: str | None = None


@dataclass
class StageResult:
    session_id: str
    agent_id: str
    agent_version: int | None
    outputs: dict[str, bytes]
    outcome_result: str | None          # satisfied | needs_revision | max_iterations_reached | failed | interrupted | None
    outcome_iterations: int
    list_cost_usd: float | None
    stop_reason: str | None
    events_seen: int
    custom_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_recorded: bool = False
    last_message: str | None = None     # the agent's final chat text; explains a turn that ended without its output file


class SessionDriver:
    def __init__(self, cfg: Config, client: anthropic.Anthropic | None = None, unattended: bool = True,
                 on_session_started: Callable[[dict[str, Any]], None] | None = None,
                 on_session_finished: Callable[[dict[str, Any]], None] | None = None):
        self.cfg = cfg
        from .config import anthropic_client
        self.client = client or anthropic_client(cfg)
        self.unattended = unattended
        self.on_session_started = on_session_started   # the orchestrator records `session.started` so the Console can link to it
        self.on_session_finished = on_session_finished  # interrupted sessions: the orchestrator records `session.finished` with their spend
        self.active: dict[str, dict[str, Any]] = {}     # sessions started by this process that have not finished, by id

    def interrupt_active(self) -> list[str]:
        """Send `user.interrupt` to every session this driver still has in flight. Called when the orchestrator is
        stopped or crashes, so a session does not keep spending up to its cap for nobody. Each one is then retrieved
        for its spend and reported through `on_session_finished` with stop_reason `interrupted` (live run 1's five
        interrupted sessions cost $3.33 that no run total counted; post-mortem issue 16). Returns the ids reached."""
        done = []
        for sid, info in sorted(self.active.items()):
            reached = False
            try:
                self.client.beta.sessions.events.send(session_id=sid, events=[{"type": "user.interrupt"}], timeout=SHORT_CALL_TIMEOUT)
                reached = True
                done.append(sid)
            except Exception as e:  # noqa: BLE001
                print(f"[session] could not interrupt {sid}: {str(e)[:120]}")
            if self.on_session_finished:
                try:
                    self.on_session_finished({**info, "session_id": sid, "list_cost_usd": self._list_cost(sid),
                                              "outcome_result": "interrupted", "stop_reason": "interrupted" if reached else "unreachable"})
                except Exception as e:  # noqa: BLE001  (bookkeeping must not stop the shutdown)
                    print(f"[session] could not record the end of {sid}: {str(e)[:120]}")
        self.active.clear()
        return done

    def _list_cost(self, session_id: str) -> float | None:
        try:
            final = self.client.beta.sessions.retrieve(session_id=session_id, timeout=SHORT_CALL_TIMEOUT)
            return int(final.usage.list_cost.amount) / 100.0
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ uploads

    def _upload_tree(self, files: dict[str, bytes]) -> list[dict[str, Any]]:
        """Upload each file and return `file` resources mounted under /mnt/session/uploads/<path>."""
        resources = []
        for rel_path, content in files.items():
            if not content:
                # A 0-byte spec.md reached every implementer and reviewer of live run 2 (post-mortem issue 1). An empty
                # upload is always an orchestrator bug: the agent would read nothing and say so in its own words.
                raise ValueError(f"refusing to upload an empty file as {rel_path}")
            f = self.client.beta.files.upload(file=(Path(rel_path).name, content), timeout=SHORT_CALL_TIMEOUT)
            resources.append({"type": "file", "file_id": f.id, "mount_path": "/" + rel_path.lstrip("/")})
        return resources

    def standard_uploads(self, schema_names: tuple[str, ...] | None = None) -> dict[str, bytes]:
        """Schemas and the sandbox validator are mounted into every session."""
        out: dict[str, bytes] = {}
        for p in self.cfg.schemas_dir.glob("*.schema.json"):
            if schema_names is not None and p.name not in schema_names:
                continue
            out[f"schemas/{p.name}"] = p.read_bytes()
        out["tools/validate.py"] = (Path(__file__).parent / "sandbox_tools" / "validate.py").read_bytes()
        return out

    # ------------------------------------------------------------ run

    def run(
        self,
        *,
        agent_key: str,
        title: str,
        run_id: str,
        stage: str,
        uploads: dict[str, bytes],
        description: str,
        rubric_md: str | None,
        max_iterations: int,
        budget_usd: float,
        custom_tools: dict[str, CustomToolHandler] | None = None,
        work_item_id: str | None = None,
        model_override: dict[str, Any] | None = None,
        metadata: dict[str, str] | None = None,
        repository: dict[str, Any] | None = None,
        environment_id: str | None = None,
        vault_ids: list[str] | None = None,
        system_override: str | None = None,
        schema_names: tuple[str, ...] | None = None,
    ) -> StageResult:
        """`repository` is a session resource from `Forge.session_repository_resource` (the native
        `github_repository` mount, cloned through Anthropic's git proxy at /workspace/repo) or None when
        the forge has no native resource and the agent clones with the vault credential (GitLab, ADR-0003)."""
        agent_ref = self.cfg.agents[agent_key]
        if model_override or system_override:
            agent: Any = {"type": "agent_with_overrides", "id": agent_ref.id}
            if model_override:
                agent["model"] = model_override
            if system_override:
                agent["system"] = system_override
            if agent_ref.version and self.cfg.pin_agent_versions:
                agent["version"] = agent_ref.version
        elif agent_ref.version and self.cfg.pin_agent_versions:
            agent = {"type": "agent", "id": agent_ref.id, "version": agent_ref.version}
        else:
            agent = agent_ref.id

        all_uploads = {**self.standard_uploads(schema_names), **uploads}
        resources = self._upload_tree(all_uploads)
        if repository:
            resources.append(repository)

        if rubric_md:
            kickoff = [{
                "type": "user.define_outcome",
                "description": description,
                "rubric": {"type": "text", "content": rubric_md},
                "max_iterations": max_iterations,
            }]
        else:
            kickoff = [{"type": "user.message", "content": [{"type": "text", "text": description}]}]

        session = self.client.beta.sessions.create(
            agent=agent,
            environment_id=environment_id or self.cfg.environment_id,
            title=title,
            resources=resources,
            # App test vaults are independent of forge authentication. An explicit []
            # also prevents a test session inheriting the controller's forge vault.
            vault_ids=vault_ids if vault_ids is not None else ([self.cfg.vault_id] if (self.cfg.vault_id and not repository) else None),
            budget={"type": "limit", "max_list_cost": {"amount": str(round(budget_usd * 100)), "currency": "USD"}},
            metadata={"run_id": run_id, "stage": stage, **({"work_item_id": work_item_id} if work_item_id else {}),
                      **(metadata or {})},
            initial_events=kickoff,
            timeout=SHORT_CALL_TIMEOUT,
        )
        print(f"[session] {stage} {title}: {self.cfg.console_session_url(session.id)}")
        self.active[session.id] = {"agent_id": agent_ref.id, "agent_version": agent_ref.version, "agent_key": agent_key,
                                   "stage": stage, "run_id": run_id, "work_item_id": work_item_id, "title": title}
        if self.on_session_started:
            self.on_session_started({"session_id": session.id, "agent_id": agent_ref.id, "agent_version": agent_ref.version,
                                     "agent_key": agent_key, "stage": stage, "run_id": run_id, "work_item_id": work_item_id,
                                     "title": title, "console_url": self.cfg.console_session_url(session.id)})

        ctx = SessionContext(session_id=session.id, run_id=run_id, stage=stage, work_item_id=work_item_id)
        outcome_result: str | None = None
        iterations = 0
        stop_reason: str | None = None
        last_message: str | None = None
        seen: set[str] = set()
        calls: list[dict[str, Any]] = []
        tool_results: dict[str, str] = {}
        acknowledged: set[str] = set()
        handlers = custom_tools or {}

        def handle(event: Any) -> bool:
            """Returns True when the drain loop should stop."""
            nonlocal outcome_result, iterations, stop_reason, last_message
            et = event.type
            if et == "agent.message":
                text = "".join(getattr(b, "text", "") or "" for b in (getattr(event, "content", None) or [])).strip()
                if text:
                    last_message = text
            elif et == "agent.custom_tool_use":
                if event.id in acknowledged:
                    return False
                ctx.custom_tool_use_id = event.id
                if event.id not in tool_results:
                    handler = handlers.get(event.name)
                    if handler is None:
                        text = json.dumps({"error": f"tool {event.name} is not available in this stage"})
                    else:
                        text = handler(dict(event.input), ctx)
                    tool_results[event.id] = text
                    calls.append({"name": event.name, "input": dict(event.input), "result": text[:2000]})
                text = tool_results[event.id]
                self.client.beta.sessions.events.send(
                    session_id=session.id,
                    events=[{"type": "user.custom_tool_result", "custom_tool_use_id": event.id,
                             "content": [{"type": "text", "text": text}]}],
                    timeout=SHORT_CALL_TIMEOUT,
                )
            elif et in ("agent.tool_use", "agent.mcp_tool_use") and getattr(event, "evaluated_permission", None) == "ask":
                if event.id in acknowledged:
                    return False
                # Nobody is watching in unattended mode: deny, and tell the agent why so it can adapt.
                self.client.beta.sessions.events.send(
                    session_id=session.id,
                    events=[{"type": "user.tool_confirmation", "tool_use_id": event.id,
                             "result": "deny" if self.unattended else "allow",
                             **({"deny_message": "Unattended run: this call needs a human. Record it in blocked.md "
                                                 "and continue without it."} if self.unattended else {})}],
                    timeout=SHORT_CALL_TIMEOUT,
                )
            elif et == "span.outcome_evaluation_end":
                outcome_result = event.result
                iterations = int(getattr(event, "iteration", iterations)) + 1
            elif et == "session.status_idle":
                sr = getattr(event.stop_reason, "type", None)
                if sr == "requires_action":
                    return False
                stop_reason = sr
                return True
            elif et == "session.status_terminated":
                stop_reason = "terminated"
                return True
            return False

        # Stream-first, consolidated with history on (re)connect; dedupe by event id.
        # Drain the session: catch up on what already happened, then follow the stream. A stream that drops (the
        # platform closing a long-lived connection, a network blip) is reopened after a short wait; the catch-up listing
        # on reopen and the `seen` set keep every event handled exactly once. Live greenfield run: one
        # RemoteProtocolError on the implementer's stream failed a 2.5 h run and interrupted the session.
        done = False
        reconnects = 0
        delay = 2.0
        while not done:
            try:
                with self.client.beta.sessions.events.stream(session_id=session.id) as stream:
                    history = list(self._session_events(session.id))
                    # A result send may succeed server-side even if its response is lost.
                    # Reconcile acknowledgements before replaying tool calls in time order.
                    for ev in history:
                        if ev.type == "user.custom_tool_result":
                            acknowledged.add(ev.custom_tool_use_id)
                        elif ev.type == "user.tool_confirmation":
                            acknowledged.add(ev.tool_use_id)
                    for ev in history:
                        if ev.id in seen:
                            continue
                        terminal = handle(ev)
                        seen.add(ev.id)
                        if terminal:
                            done = True
                            break
                    if not done:
                        for ev in stream:
                            if getattr(ev, "id", None) in seen:
                                continue
                            terminal = handle(ev)
                            if getattr(ev, "id", None):
                                seen.add(ev.id)
                            if terminal:
                                done = True
                                break
                if not done:
                    # An HTTP stream can end while the session continues. Only a terminal event
                    # proves completion; reconnect and recover the paginated history otherwise.
                    raise httpx.RemoteProtocolError("event stream ended before session completion")
            except STREAM_RETRYABLE as e:
                reconnects += 1
                if reconnects > STREAM_MAX_RECONNECTS:
                    raise
                print(f"[session] {session.id}: event stream dropped ({type(e).__name__}); reconnecting in {delay:.0f}s ({reconnects}/{STREAM_MAX_RECONNECTS})")
                time.sleep(delay)
                delay = min(delay * 2, 30.0)

        # Post-idle status write race: wait for the session object to settle before reading usage.
        final = None
        for _ in range(10):
            final = self.client.beta.sessions.retrieve(session_id=session.id, timeout=SHORT_CALL_TIMEOUT)
            if final.status != "running":
                break
            time.sleep(0.3)
        list_cost = None
        try:
            amount = final.usage.list_cost.amount  # integer string in cents
            list_cost = int(amount) / 100.0
        except (AttributeError, TypeError, ValueError):
            list_cost = None

        outputs = self._fetch_outputs(session.id)
        self.active.pop(session.id, None)
        return StageResult(
            session_id=session.id,
            agent_id=agent_ref.id,
            agent_version=agent_ref.version,
            outputs=outputs,
            outcome_result=outcome_result,
            outcome_iterations=iterations,
            list_cost_usd=list_cost,
            stop_reason=stop_reason,
            events_seen=len(seen),
            custom_tool_calls=calls,
            last_message=last_message,
        )

    def _session_events(self, session_id: str):
        page = self.client.beta.sessions.events.list(session_id=session_id, order="asc", timeout=SHORT_CALL_TIMEOUT)
        while True:
            yield from page.data
            more = page.has_next_page() if hasattr(page, "has_next_page") else getattr(page, "has_more", False)
            if not more:
                return
            page = page.get_next_page()

    def _fetch_outputs(self, session_id: str) -> dict[str, bytes]:
        """Download what the session wrote to /mnt/session/outputs/ (brief indexing lag; retry a few times).

        The session scope lists the uploads too (schemas, tools, inputs) with `downloadable: false`; only files the
        session produced are downloadable, so filter on that flag and never fail the whole fetch on one file.
        The listing is paginated (20 per page) and uploads and outputs are interleaved, so every page is read:
        live run 2 lost review_response.json to page 2 and the run died on a "missing" output."""
        for attempt in range(4):
            data = [f for f in self._list_session_files(session_id) if getattr(f, "downloadable", True)]
            if data or attempt == 3:
                out: dict[str, bytes] = {}
                for f in data:
                    try:
                        resp = self.client.beta.files.download(f.id, timeout=SHORT_CALL_TIMEOUT)
                    except Exception as e:  # noqa: BLE001  (one undownloadable file must not lose the others)
                        print(f"[session] {session_id}: could not download {f.filename}: {str(e)[:120]}")
                        continue
                    out[f.filename] = resp.read() if hasattr(resp, "read") else bytes(resp)
                return out
            time.sleep(1.5)
        return {}

    def _list_session_files(self, session_id: str) -> list[Any]:
        """Every file in the session's scope, across all pages of the listing."""
        page = self.client.beta.files.list(scope_id=session_id, betas=["managed-agents-2026-04-01"], timeout=SHORT_CALL_TIMEOUT)
        files: list[Any] = list(page.data)
        while getattr(page, "has_more", False) and hasattr(page, "get_next_page"):
            page = page.get_next_page()
            files.extend(page.data)
        return files

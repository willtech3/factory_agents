"""Driver for one Managed Agents session per stage.

Follows the client patterns in the Managed Agents docs: stream first, then send;
answer `agent.custom_tool_use` with `user.custom_tool_result`; answer paused tool
calls (`evaluated_permission == "ask"`) by denying when unattended; break on idle only
when `stop_reason.type != "requires_action"`; fetch outputs from `/mnt/session/outputs/`
via the Files API with `scope_id`.

Status: written against the documented SDK surface (anthropic >= 1.0,
`client.beta.{agents,sessions,files}`), not yet exercised against the live API. The
first spike in docs/roadmap.md is to run `decompose` end to end with this driver.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import anthropic

from .config import Config

# Seconds a short platform call (upload, create, send, list, download) may wait for a response before the SDK
# retries it. The client default is 600 s: in live run 2 one reviewer's session creation hung for exactly that
# long before the retry succeeded, which held the whole review round. Streams keep the client default.
SHORT_CALL_TIMEOUT = 120.0

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


class SessionDriver:
    def __init__(self, cfg: Config, client: anthropic.Anthropic | None = None, unattended: bool = True,
                 on_session_started: Callable[[dict[str, Any]], None] | None = None):
        self.cfg = cfg
        from .config import anthropic_client
        self.client = client or anthropic_client(cfg)
        self.unattended = unattended
        self.on_session_started = on_session_started   # the orchestrator records `session.started` so the Console can link to it
        self.active: set[str] = set()                   # sessions started by this process that have not finished

    def interrupt_active(self) -> list[str]:
        """Send `user.interrupt` to every session this driver still has in flight. Called when the orchestrator is
        stopped or crashes, so a session does not keep spending up to its cap for nobody. Returns the ids reached."""
        done = []
        for sid in sorted(self.active):
            try:
                self.client.beta.sessions.events.send(session_id=sid, events=[{"type": "user.interrupt"}], timeout=SHORT_CALL_TIMEOUT)
                done.append(sid)
            except Exception as e:  # noqa: BLE001
                print(f"[session] could not interrupt {sid}: {str(e)[:120]}")
        self.active.clear()
        return done

    # ------------------------------------------------------------ uploads

    def _upload_tree(self, files: dict[str, bytes]) -> list[dict[str, Any]]:
        """Upload each file and return `file` resources mounted under /mnt/session/uploads/<path>."""
        resources = []
        for rel_path, content in files.items():
            f = self.client.beta.files.upload(file=(Path(rel_path).name, content), timeout=SHORT_CALL_TIMEOUT)
            resources.append({"type": "file", "file_id": f.id, "mount_path": "/" + rel_path.lstrip("/")})
        return resources

    def standard_uploads(self) -> dict[str, bytes]:
        """Schemas and the sandbox validator are mounted into every session."""
        out: dict[str, bytes] = {}
        for p in self.cfg.schemas_dir.glob("*.schema.json"):
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
    ) -> StageResult:
        """`repository` is a session resource from `Forge.session_repository_resource` (the native
        `github_repository` mount, cloned through Anthropic's git proxy at /workspace/repo) or None when
        the forge has no native resource and the agent clones with the vault credential (GitLab, ADR-0003)."""
        agent_ref = self.cfg.agents[agent_key]
        if model_override:
            agent: Any = {"type": "agent_with_overrides", "id": agent_ref.id, "model": model_override}
            if agent_ref.version and self.cfg.pin_agent_versions:
                agent["version"] = agent_ref.version
        elif agent_ref.version and self.cfg.pin_agent_versions:
            agent = {"type": "agent", "id": agent_ref.id, "version": agent_ref.version}
        else:
            agent = agent_ref.id

        all_uploads = {**self.standard_uploads(), **uploads}
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
            environment_id=self.cfg.environment_id,
            title=title,
            resources=resources,
            vault_ids=[self.cfg.vault_id] if (self.cfg.vault_id and not repository) else None,
            budget={"type": "limit", "max_list_cost": {"amount": str(int(round(budget_usd * 100))), "currency": "USD"}},
            metadata={"run_id": run_id, "stage": stage, **({"work_item_id": work_item_id} if work_item_id else {}),
                      **(metadata or {})},
            initial_events=kickoff,
            timeout=SHORT_CALL_TIMEOUT,
        )
        print(f"[session] {stage} {title}: {self.cfg.console_session_url(session.id)}")
        self.active.add(session.id)
        if self.on_session_started:
            self.on_session_started({"session_id": session.id, "agent_id": agent_ref.id, "agent_version": agent_ref.version,
                                     "agent_key": agent_key, "stage": stage, "run_id": run_id, "work_item_id": work_item_id,
                                     "title": title, "console_url": self.cfg.console_session_url(session.id)})

        ctx = SessionContext(session_id=session.id, run_id=run_id, stage=stage, work_item_id=work_item_id)
        outcome_result: str | None = None
        iterations = 0
        stop_reason: str | None = None
        seen: set[str] = set()
        calls: list[dict[str, Any]] = []
        handlers = custom_tools or {}

        def handle(event: Any) -> bool:
            """Returns True when the drain loop should stop."""
            nonlocal outcome_result, iterations, stop_reason
            et = event.type
            if et == "agent.custom_tool_use":
                ctx.custom_tool_use_id = event.id
                handler = handlers.get(event.name)
                if handler is None:
                    text = json.dumps({"error": f"tool {event.name} is not available in this stage"})
                else:
                    text = handler(dict(event.input), ctx)
                calls.append({"name": event.name, "input": dict(event.input), "result": text[:2000]})
                self.client.beta.sessions.events.send(
                    session_id=session.id,
                    events=[{"type": "user.custom_tool_result", "custom_tool_use_id": event.id,
                             "content": [{"type": "text", "text": text}]}],
                    timeout=SHORT_CALL_TIMEOUT,
                )
            elif et in ("agent.tool_use", "agent.mcp_tool_use") and getattr(event, "evaluated_permission", None) == "ask":
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
        with self.client.beta.sessions.events.stream(session_id=session.id) as stream:
            for ev in self.client.beta.sessions.events.list(session_id=session.id, timeout=SHORT_CALL_TIMEOUT).data:
                seen.add(ev.id)
                if handle(ev):
                    break
            else:
                for ev in stream:
                    if getattr(ev, "id", None) in seen:
                        continue
                    if getattr(ev, "id", None):
                        seen.add(ev.id)
                    if handle(ev):
                        break

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
        except Exception:
            pass

        outputs = self._fetch_outputs(session.id)
        self.active.discard(session.id)
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
        )

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

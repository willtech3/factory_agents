"""A scripted forge HTTP API for the forge tests: a route table answered through httpx's MockTransport, every request
recorded. The forge clients under test are the real ones."""
import json

import httpx

from factory.forge.github import GitHubForge


class Seq(list):
    """Route value: one (status, payload) per call in order; the last one repeats. A plain list is a JSON array."""


class Recorder:
    """Scripted forge API: records every request; answers from a route table (method + decoded path -> json/status)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.raw_paths = []                              # as sent, so a missing percent-encoding shows

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.raw_paths.append(request.url.raw_path.decode().split("?")[0])
        self.calls.append((request.method, request.url.path, dict(request.url.params), body))
        key = f"{request.method} {request.url.path}"
        resp = self.routes.get(key)
        if resp is None:
            return httpx.Response(404, json={"message": f"no route {key}"})
        if isinstance(resp, Seq):                        # one response per call, the last one repeats
            idx = self.calls.count((request.method, request.url.path, dict(request.url.params), body)) - 1
            resp = resp[min(idx, len(resp) - 1)]
        status, payload = (resp if isinstance(resp, tuple) else (200, resp))
        if callable(payload):
            payload = payload(body, dict(request.url.params))
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)


def github_forge(routes, **kw) -> tuple[GitHubForge, Recorder]:
    rec = Recorder(routes)
    f = GitHubForge("https://github.com", "ghp_test", "will/ledgerline", transport=httpx.MockTransport(rec), **kw)
    return f, rec

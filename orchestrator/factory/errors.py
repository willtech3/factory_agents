"""Errors the controller raises for a person to act on, independent of any web framework."""
from __future__ import annotations


class Refusal(Exception):
    """A request refused for a reason the person can act on (a busy app, a changed plan, a missing credential).
    `status_code` is how the Factory Console answers it; the CLI prints `detail`."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail

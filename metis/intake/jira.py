"""Jira adapter.

Credentials are fetched from the substrate's store, never passed in and never
returned. Settings (URL, account email, JQL) come from `metis.yaml`.
"""

from __future__ import annotations

from typing import Any

import requests

from .. import secrets
from .base import Issue

TIMEOUT = 30
NAME = "jira"


def _adf(text: str) -> dict[str, Any]:
    """Jira Cloud comment bodies are Atlassian Document Format, not plain text."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line or " "}]}
            for line in text.splitlines() or [" "]
        ],
    }


def _plain(node: Any) -> str:
    """Flatten an ADF description back to text."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return "".join(_plain(c) for c in node.get("content") or [])
    if isinstance(node, list):
        return "".join(_plain(c) for c in node)
    return ""


class Jira:
    name = NAME

    def __init__(self, settings: dict[str, Any]):
        self.url = str(settings.get("url", "")).rstrip("/")
        self.email = settings.get("email")
        self.jql = settings.get("jql") or "assignee = currentUser() AND resolution = Unresolved"
        self.on_start = settings.get("on_start")
        self.on_done = settings.get("on_done")
        self.max_results = int(settings.get("max_results", 25))

        if not self.url or not self.email:
            raise ValueError("intake.jira needs both `url` and `email` in metis.yaml")

    @property
    def _auth(self) -> tuple[str, str]:
        return (self.email, secrets.require("jira.api_token"))

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        resp = requests.request(
            method, f"{self.url}{path}", auth=self._auth,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=TIMEOUT, **kwargs,
        )
        resp.raise_for_status()
        return resp

    def fetch(self) -> list[Issue]:
        resp = self._request(
            "GET", "/rest/api/3/search",
            params={"jql": self.jql, "maxResults": self.max_results,
                    "fields": "summary,description,labels,reporter,status"},
        )
        issues: list[Issue] = []

        for item in resp.json().get("issues", []):
            fields = item.get("fields") or {}
            reporter = (fields.get("reporter") or {}).get("displayName")
            issues.append(Issue(
                source=NAME,
                key=item.get("key", ""),
                title=fields.get("summary") or "",
                body=_plain(fields.get("description")),
                url=f"{self.url}/browse/{item.get('key')}",
                reporter=reporter,
                labels=list(fields.get("labels") or []),
                raw={"status": (fields.get("status") or {}).get("name")},
            ))
        return issues

    def comment(self, key: str, body: str) -> None:
        self._request("POST", f"/rest/api/3/issue/{key}/comment", json={"body": _adf(body)})

    def transition(self, key: str, state: str) -> None:
        available = self._request("GET", f"/rest/api/3/issue/{key}/transitions").json()
        match = next(
            (t for t in available.get("transitions", [])
             if t.get("name", "").lower() == state.lower()
             or (t.get("to") or {}).get("name", "").lower() == state.lower()),
            None,
        )
        if not match:
            names = ", ".join(t.get("name", "") for t in available.get("transitions", []))
            raise ValueError(f"no transition to '{state}' from here (available: {names})")

        self._request("POST", f"/rest/api/3/issue/{key}/transitions",
                      json={"transition": {"id": match["id"]}})


def build(settings: dict[str, Any]) -> Jira:
    return Jira(settings)

"""Trello adapter.

Trello has no workflow states, so a "transition" is a card moving to another
list. That difference is absorbed here rather than leaking into `sync.py`.
"""

from __future__ import annotations

from typing import Any

import requests

from .. import secrets
from .base import Issue

TIMEOUT = 30
BASE = "https://api.trello.com/1"
NAME = "trello"


class Trello:
    name = NAME

    def __init__(self, settings: dict[str, Any]):
        self.board_id = settings.get("board_id")
        self.list_name = settings.get("list_name") or "Ready for Dev"
        self.on_start = settings.get("on_start")
        self.on_done = settings.get("on_done")

        if not self.board_id:
            raise ValueError("intake.trello needs `board_id` in metis.yaml")
        self._lists: dict[str, str] | None = None

    @property
    def _credentials(self) -> dict[str, str]:
        return {"key": secrets.require("trello.key"), "token": secrets.require("trello.token")}

    def _request(self, method: str, path: str, params: dict | None = None) -> Any:
        resp = requests.request(
            method, f"{BASE}{path}", params={**self._credentials, **(params or {})},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else None

    def _list_ids(self) -> dict[str, str]:
        if self._lists is None:
            lists = self._request("GET", f"/boards/{self.board_id}/lists")
            self._lists = {l["name"]: l["id"] for l in lists}
        return self._lists

    def fetch(self) -> list[Issue]:
        list_id = self._list_ids().get(self.list_name)
        if not list_id:
            raise ValueError(
                f"no list named '{self.list_name}' on the board "
                f"(found: {', '.join(self._list_ids())})"
            )

        cards = self._request("GET", f"/lists/{list_id}/cards", {"fields": "name,desc,url,labels"})
        return [
            Issue(
                source=NAME,
                key=card["id"],
                title=card.get("name") or "",
                body=card.get("desc") or "",
                url=card.get("url"),
                labels=[l.get("name", "") for l in card.get("labels") or [] if l.get("name")],
                raw={"list": self.list_name},
            )
            for card in cards
        ]

    def comment(self, key: str, body: str) -> None:
        self._request("POST", f"/cards/{key}/actions/comments", {"text": body})

    def transition(self, key: str, state: str) -> None:
        list_id = self._list_ids().get(state)
        if not list_id:
            raise ValueError(f"no list named '{state}' (found: {', '.join(self._list_ids())})")
        self._request("PUT", f"/cards/{key}", {"idList": list_id})


def build(settings: dict[str, Any]) -> Trello:
    return Trello(settings)

"""Shared test doubles for the notify-service suite."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx


@dataclass
class RecordedRequest:
    url: str
    json_body: dict[str, object]


class FakeAsyncClient:
    """httpx.AsyncClient double capturing posts; never touches network."""

    def __init__(self, *, fail: bool = False) -> None:
        self.posts: list[RecordedRequest] = []
        self._fail = fail
        self.base_url = "https://api.telegram.org"

    async def post(self, url: str, json: dict[str, object]) -> httpx.Response:
        self.posts.append(RecordedRequest(url=url, json_body=json))
        request = httpx.Request("POST", url)
        if self._fail:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": True}, request=request)


class FakeLifecycle:
    """LifecycleLogger double recording audit calls."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, str]] = []

    async def record(
        self,
        *,
        event: str,
        subject: str,
        outcome: str = "ok",
        payload: dict[str, object] | None = None,
    ) -> None:
        self.records.append((event, subject, outcome))


def dumps(value: object) -> str:
    return json.dumps(value)


# Re-export for parity with execution-service fakes naming.
field = field

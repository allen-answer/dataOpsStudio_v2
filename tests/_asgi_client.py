from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from starlette.types import ASGIApp, Message, Scope


@dataclass(frozen=True)
class AsgiResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class AsgiClient:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    def get(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, object] | None = None,
    ) -> AsgiResponse:
        return self.request("GET", path, headers=headers, params=params)

    def post(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: object | None = None,
        params: dict[str, object] | None = None,
    ) -> AsgiResponse:
        return self.request("POST", path, headers=headers, json_body=json_body, params=params)

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: object | None = None,
        params: dict[str, object] | None = None,
    ) -> AsgiResponse:
        return asyncio.run(
            self._request(method, path, headers=headers, json_body=json_body, params=params)
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None,
        json_body: object | None,
        params: dict[str, object] | None,
    ) -> AsgiResponse:
        path_only, _, raw_query = path.partition("?")
        query = raw_query
        if params:
            encoded = urlencode(params)
            query = f"{query}&{encoded}" if query else encoded

        body = b""
        raw_headers = [
            (b"host", b"testserver"),
        ]
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            raw_headers.extend(
                [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ]
            )
        for name, value in (headers or {}).items():
            raw_headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path_only,
            "raw_path": path_only.encode("ascii"),
            "query_string": query.encode("ascii"),
            "headers": raw_headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        response_started: Message | None = None
        response_body = bytearray()
        receive_called = False

        async def receive() -> Message:
            nonlocal receive_called
            if not receive_called:
                receive_called = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = message
                return
            if message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))

        await self._app(scope, receive, send)
        if response_started is None:
            raise AssertionError("ASGI app did not start an HTTP response")
        response_headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in response_started.get("headers", [])
        }
        return AsgiResponse(
            status_code=int(response_started["status"]),
            headers=response_headers,
            body=bytes(response_body),
        )

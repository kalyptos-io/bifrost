"""admission control. one middleware, three bounds on a request: body bytes, the worker's in-flight
count, and wall-clock time. the deadline is what actually bounds a pool-starved request - cancelling
it releases its pending acquire - so no per-query timeout is needed downstream."""

import asyncio
import json

from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SKIP = frozenset({"/health", "/ready", "/metrics"})  # probes must never be shed or timed out


async def _reject(
    send: Send, status: int, detail: str, extra: tuple[tuple[bytes, bytes], ...] = ()
) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *extra,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class LimitsMiddleware:
    def __init__(
        self, app: ASGIApp, *, max_body_bytes: int, max_inflight: int, request_timeout: float
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.request_timeout = request_timeout
        self.sem = asyncio.Semaphore(max_inflight)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _SKIP:
            await self.app(scope, receive, send)
            return
        if self.sem.locked():  # shed rather than queue; nothing awaits before the acquire below
            await _reject(send, 503, "overloaded", ((b"retry-after", b"1"),))
            return
        read, started = 0, False

        async def counted() -> Message:
            nonlocal read
            message = await receive()
            if message["type"] == "http.request":
                read += len(message.get("body", b""))
                if read > self.max_body_bytes:
                    # the body read re-raises HTTPException, so this renders as a normal 413
                    raise HTTPException(status_code=413, detail="request body too large")
            return message

        async def capture(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            async with self.sem, asyncio.timeout(self.request_timeout):
                await self.app(scope, counted, capture)
        except TimeoutError:
            if started:  # already on the wire: a truncated body beats a second response start
                raise
            await _reject(send, 504, "request timed out")

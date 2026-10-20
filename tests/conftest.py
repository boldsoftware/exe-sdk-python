"""Fixtures that run each test against both Client and AsyncClient, with a fake exe.dev."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx
import pytest

import exedev
from exedev import _async, _sync

Handler = Callable[[str], Any]


class Chunks(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A response body that arrives in pieces, then optionally breaks."""

    def __init__(self, chunks: list[bytes], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield from self.chunks
        if self.error:
            raise self.error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error

    def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.closed = True


def frame(**kw: Any) -> bytes:
    """One NDJSON frame; stdout and stderr are given as bytes."""
    doc = {k: base64.b64encode(v).decode() if isinstance(v, bytes) else v for k, v in kw.items()}
    return json.dumps(doc).encode() + b"\n"


def ndjson(*frames: bytes, split: int = 0, error: Exception | None = None) -> httpx.Response:
    """An NDJSON response; split > 0 delivers the body in pieces of that size."""
    body = b"".join(frames)
    chunks = [body[i : i + split] for i in range(0, len(body), split)] if split else [body]
    return streaming(Chunks(chunks, error))


def streaming(stream: Chunks) -> httpx.Response:
    """An NDJSON response with this body. response.chunks is the stream, to check it was closed."""
    response = httpx.Response(200, headers={"Content-Type": "application/x-ndjson"}, stream=stream)
    response.chunks = stream  # type: ignore[attr-defined]
    return response


def ran(stdout: bytes = b"", stderr: bytes = b"", exit_code: int = 0) -> httpx.Response:
    """A command that wrote stdout and stderr, then exited."""
    frames = []
    if stdout:
        frames.append(frame(stdout=stdout))
    if stderr:
        frames.append(frame(stderr=stderr))
    return ndjson(*frames, frame(exit_code=exit_code))


def error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": message})


class Server:
    """A fake exe.dev that records each command line and answers with handler.

    handler returns an httpx.Response, or a value to send as JSON, or a
    string to send as text. A dict maps command lines to answers instead; a
    list of answers is used in turn.
    """

    def __init__(self, handler: Handler | dict[str, Any]) -> None:
        self.handler = handler
        self.commands: list[str] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        command = request.content.decode()
        self.requests.append(request)
        self.commands.append(command)
        if callable(self.handler):
            answer = self.handler(command)
        else:
            if command not in self.handler:
                return error(422, f"unexpected command: {command}")
            answer = self.handler[command]
            if isinstance(answer, list):
                answer = answer.pop(0) if len(answer) > 1 else answer[0]
            if isinstance(answer, httpx.Response):
                # A response is read once; this one may be the answer again.
                answer = fresh(answer)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        if isinstance(answer, str):
            return httpx.Response(200, text=answer)
        return httpx.Response(200, json=answer)


def fresh(response: httpx.Response) -> httpx.Response:
    """An unread copy of response."""
    stream = getattr(response, "chunks", None)
    if isinstance(stream, Chunks):
        return streaming(Chunks(stream.chunks, stream.error))
    return httpx.Response(response.status_code, headers=response.headers, content=response.content)


class API:
    """Makes a client of one flavor, and resolves its results."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.sync = mode == "sync"

    def client(self, handler: Handler | dict[str, Any], **kw: Any) -> tuple[Any, Server]:
        server = Server(handler)
        transport = httpx.MockTransport(server)
        if self.sync:
            return exedev.Client("tok", http_client=httpx.Client(transport=transport), **kw), server
        return exedev.AsyncClient("tok", http_client=httpx.AsyncClient(transport=transport), **kw), server

    def __call__(self, value: Any) -> Any:
        """value itself from the sync client; awaited from the async one."""
        if inspect.isawaitable(value):

            async def wait() -> Any:
                return await value

            return asyncio.run(wait())
        return value


@pytest.fixture(params=["sync", "async"])
def api(request: pytest.FixtureRequest) -> API:
    return API(request.param)


class Clock:
    """Fake time: sleeping advances it at once."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    async def async_sleep(self, seconds: float) -> None:
        self.sleep(seconds)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    for module in (_sync, _async):
        monkeypatch.setattr(module, "monotonic", c.monotonic)
    monkeypatch.setattr(_sync.time, "sleep", c.sleep)
    monkeypatch.setattr(_async.asyncio, "sleep", c.async_sleep)
    return c

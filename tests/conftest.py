"""Fixtures that run each test against both Client and AsyncClient, with a fake exe.dev."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import re
import shlex
import signal
import subprocess
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import exedev
from exedev import _async, _sync, _wire

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


def record(name: str, **kw: Any) -> dict[str, Any]:
    """A VM as ls lists it."""
    return {
        "vm_name": name,
        "ssh_dest": f"{name}.exe.xyz",
        "https_url": f"https://{name}.exe.xyz",
        "status": "running",
        **kw,
    }


# The SDK's scripts on the VM, by name: RUN_SCRIPT is "run".
SCRIPTS = {getattr(_wire, n): n.removesuffix("_SCRIPT").lower() for n in dir(_wire) if n.endswith("_SCRIPT")}


def describe(command: str) -> str:
    """command, with an SDK script it runs on a VM by name: ssh worker run 'echo hi'."""
    match = re.match(r"ssh ([a-z0-9-]+) '(.*)'\Z", command, re.DOTALL)
    if not match:
        return command
    # Undo shlex.quote, quickly: commands can be large.
    inner = match[2].replace("'\"'\"'", "'")
    for script, name in SCRIPTS.items():
        prefix = shlex.join(["sh", "-c", script, "sh"])
        if inner == prefix or inner.startswith(prefix + " "):
            rest = inner[len(prefix) + 1 :]
            args = rest.split(" ") if re.fullmatch(r"[\w@%+=:,./ -]*", rest) else shlex.split(rest)
            return shlex.join(["ssh", match[1], name, *args])
    return command


class Server:
    """A fake exe.dev that records each command line and answers with handler.

    handler returns an httpx.Response, or a value to send as JSON, or a
    string to send as text. A dict maps command lines to answers instead; a
    list of answers is used in turn. Commands are described: SDK scripts on
    the VM are named (see describe()). Unless the handler is a dict that
    answers it, "ls NAME" lists a running VM.
    """

    def __init__(self, handler: Handler | dict[str, Any]) -> None:
        self.handler = handler
        self.commands: list[str] = []
        self.raw: list[str] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        raw = request.content.decode()
        command = describe(raw)
        self.raw.append(raw)
        self.requests.append(request)
        self.commands.append(command)
        listed = re.fullmatch(r"ls ([a-z0-9-]+)", command)
        if listed and not (isinstance(self.handler, dict) and command in self.handler):
            return httpx.Response(200, json={"vms": [record(listed[1])]})
        if callable(self.handler):
            answer = self.handler(raw if getattr(self.handler, "raw", False) else command)
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

    def vm(self, handler: Handler | dict[str, Any], name: str = "worker", **kw: Any) -> tuple[Any, Any, Server]:
        """A client, its VM name, and its server, which has recorded no commands yet."""
        exe, server = self.client(handler, **kw)
        vm = self(exe.vm(name))
        server.commands.clear()
        server.raw.clear()
        server.requests.clear()
        return exe, vm, server

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


class LocalVM:
    """A fake VM "worker": this machine, running each ssh command with bash
    as a VM does, in a home directory of its own."""

    raw = True

    def __init__(self, home: Path) -> None:
        self.home = home

    def __call__(self, command: str) -> httpx.Response:
        words = shlex.split(command)
        if command == "ls worker":
            return httpx.Response(200, json={"vms": [record("worker")]})
        if len(words) != 3 or words[:2] != ["ssh", "worker"]:
            return error(422, f"unexpected command: {command}")
        env = {"HOME": str(self.home), "SHELL": "/bin/bash", "PATH": os.environ["PATH"], "USER": "exedev"}
        done = subprocess.run(
            ["bash", "-c", words[2]], check=False, cwd=self.home, env=env, capture_output=True, timeout=60
        )
        out = [frame(stdout=done.stdout[i : i + 65536]) for i in range(0, len(done.stdout), 65536)]
        err = [frame(stderr=done.stderr[i : i + 65536]) for i in range(0, len(done.stderr), 65536)]
        return ndjson(*out, *err, frame(exit_code=done.returncode))

    def kill_processes(self) -> None:
        """Kill what spawn() started."""
        for pid in self.home.glob(".local/state/exedev/processes/*/pid"):
            try:
                os.killpg(int(pid.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


@pytest.fixture
def local(api: API, tmp_path: Path) -> Iterator[tuple[Any, LocalVM, Server]]:
    """The VM "worker", which is this machine (see LocalVM), its LocalVM, and its server."""
    home = tmp_path / "home"
    home.mkdir()
    machine = LocalVM(home)
    _, vm, server = api.vm(machine)
    yield vm, machine, server
    machine.kill_processes()

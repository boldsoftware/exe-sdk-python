"""VM.run: commands over SSH, streamed as NDJSON frames."""

from __future__ import annotations

import asyncio
import shlex

import httpx
import pytest
from conftest import Chunks, Server, error, frame, ndjson, ran, streaming

import exedev
from exedev import _wire


def run_server(api, *answers):
    """A client whose VM "worker" answers each run with the next answer."""
    queue = list(answers)
    _, vm, server = api.vm(lambda _: queue.pop(0))
    return vm, server


def async_vm(handler):
    """An AsyncClient's VM "worker", made in the running event loop."""
    transport = httpx.MockTransport(Server(handler))
    exe = exedev.AsyncClient("tok", http_client=httpx.AsyncClient(transport=transport))
    return exe.vm("worker")


def test_run(api):
    vm, server = run_server(api, ran(b"hi \xc3\xa9\x00\xff\n", b"warning\n", 7))
    result = api(vm.run("echo hello"))
    assert server.commands == ["ssh worker run 'echo hello'"]
    assert server.raw == ["ssh worker " + shlex.quote(_wire.sh(_wire.RUN_SCRIPT, "echo hello"))]
    assert server.requests[0].headers["Accept"] == "application/json, application/x-ndjson"
    assert result == exedev.CommandResult(7, b"hi \xc3\xa9\x00\xff\n", b"warning\n")
    assert result.stdout == "hi é\x00\ufffd\n"
    assert result.stderr == "warning\n"
    assert not result.ok
    assert result.captured


def test_run_argv(api):
    vm, server = run_server(api, ran(), ran())
    api(vm.run(["cat", "my file", "$HOME"]))
    api(vm.run("ls", cwd="/srv/my app", env={"A": "1", "B": "it's"}))
    # The /exec body is a command line; its ssh command is another.
    first, second = (shlex.split(command) for command in server.commands)
    assert first == ["ssh", "worker", "run", "cat 'my file' '$HOME'"]
    assert second[:3] == ["ssh", "worker", "run"]
    assert shlex.split(second[3]) == ["cd", "/srv/my app", "||", "exit;", "export", "A=1;", "export", "B=it's;", "ls"]
    assert second[3] == "cd '/srv/my app' || exit; export A=1; export B='it'\"'\"'s'; ls"


def test_run_rejects_bad_commands(api):
    vm, server = run_server(api)
    for bad in [[], ["ls", 1], b"ls", None]:
        with pytest.raises(TypeError):
            api(vm.run(bad))
    with pytest.raises(TypeError):
        api(vm.run("ls", env={"A=B": "c"}))
    with pytest.raises(TypeError):
        api(vm.run("ls", env={"A": 1}))
    with pytest.raises(ValueError):
        api(vm.run("ls", timeout=0))
    assert server.commands == []


def test_check(api):
    vm, _ = run_server(api, ran(b"", b"first\nmake: *** [test] Error 2\n\n", 2), ran(b"ok"))
    with pytest.raises(exedev.CommandError) as info:
        api(vm.run("make test", check=True))
    assert str(info.value) == "command exited with status 2: make: *** [test] Error 2"
    assert info.value.exit_code == 2
    assert info.value.result.stderr_bytes.startswith(b"first")
    assert api(vm.run("true", check=True)).stdout == "ok"


def test_frames_split_anywhere(api):
    vm, _ = run_server(api, ndjson(frame(stdout=b"one"), b"\n", frame(stderr=b"two"), frame(exit_code=0), split=3))
    result = api(vm.run("x"))
    assert (result.stdout, result.stderr, result.exit_code) == ("one", "two", 0)


def test_heartbeats_and_trailing_newline_are_optional(api):
    vm, _ = run_server(api, ndjson(b"\n\n", frame(stdout=b"a"), b'{"exit_code": 3}'))
    assert api(vm.run("x")).exit_code == 3


def collector(api, log, name, fail_on=None):
    def callback(text):
        if text == fail_on:
            raise RuntimeError("stop")
        log.append((name, text))

    async def async_callback(text):
        await asyncio.sleep(0)
        callback(text)

    return callback if api.sync else async_callback


def test_callbacks(api):
    # Callbacks see whole lines, without line endings, and whole characters:
    # é is split across frames.
    frames = [
        frame(stdout=b"one\ntw"),
        frame(stderr=b"err\n"),
        frame(stdout=b"o\r\nthr\xc3"),
        frame(stdout=b"\xa9e"),
        frame(exit_code=0),
    ]
    vm, _ = run_server(api, ndjson(*frames, split=7))
    log = []
    result = api(vm.run("x", on_stdout=collector(api, log, "out"), on_stderr=collector(api, log, "err")))
    assert log == [("out", "one"), ("err", "err"), ("out", "two"), ("out", "thrée")]
    assert result.stdout == "one\ntwo\r\nthrée"


def test_long_lines_arrive_in_pieces(api):
    vm, _ = run_server(api, ran(b"x" * (_wire.MAX_LINE * 2 + 1) + b"\n"))
    log = []
    api(vm.run("x", on_stdout=collector(api, log, "out")))
    assert [len(text) for _, text in log] == [_wire.MAX_LINE, _wire.MAX_LINE, 1]


def test_sync_callbacks_in_async_code():
    log = []

    async def main():
        vm = await async_vm(lambda _: ran(b"out"))
        await vm.run("x", on_stdout=log.append)

    asyncio.run(main())
    assert log == ["out"]


def test_callback_errors_stop_the_command(api):
    response = ndjson(frame(stdout=b"one\n"), frame(stdout=b"two\n"), frame(stdout=b"three\n"), frame(exit_code=0))
    vm, _ = run_server(api, response)
    log = []
    with pytest.raises(RuntimeError, match="stop"):
        api(vm.run("x", on_stdout=collector(api, log, "out", fail_on="two")))
    assert log == [("out", "one")]
    assert response.chunks.closed


def test_invalid_utf8_at_the_end_is_replaced(api):
    vm, _ = run_server(api, ran(b"ok\xc3"))
    log = []
    api(vm.run("x", on_stdout=collector(api, log, "out")))
    assert log == [("out", "ok\ufffd")]


def test_output_limit(api):
    response = ndjson(frame(stdout=b"12345"), frame(stderr=b"678"), frame(stdout=b"9"), frame(exit_code=0))
    vm, _ = run_server(api, response, ran(b"x" * 100))
    with pytest.raises(exedev.OutputLimitError) as info:
        api(vm.run("x", max_output=8))
    assert isinstance(info.value, exedev.CommandInterruptedError)
    assert (info.value.stdout, info.value.stderr) == ("12345", "678")
    assert response.chunks.closed
    log = []
    result = api(vm.run("x", capture=False, max_output=8, on_stdout=collector(api, log, "out")))
    assert (result.stdout, result.captured, log) == ("", False, [("out", "x" * 100)])


def test_stream_ends_early(api):
    vm, _ = run_server(api, ndjson(frame(stdout=b"partial")))
    with pytest.raises(exedev.CommandInterruptedError) as info:
        api(vm.run("x"))
    assert type(info.value) is exedev.CommandInterruptedError
    assert info.value.stdout == "partial"
    assert info.value.reason is None


def test_connection_breaks(api):
    vm, _ = run_server(api, ndjson(frame(stdout=b"partial"), error=httpx.ReadError("reset")))
    with pytest.raises(exedev.CommandInterruptedError) as info:
        api(vm.run("x"))
    assert info.value.stdout == "partial"
    assert isinstance(info.value.__cause__, httpx.ReadError)


def test_request_fails(api):
    vm, _ = run_server(api, httpx.ConnectError("refused"))
    with pytest.raises(exedev.CommandInterruptedError):
        api(vm.run("x"))


def test_error_frame(api):
    vm, _ = run_server(api, ndjson(frame(stderr=b"e"), frame(error="ssh: connection lost")))
    with pytest.raises(exedev.CommandInterruptedError) as info:
        api(vm.run("x"))
    assert info.value.reason == "ssh: connection lost"
    assert info.value.stderr == "e"
    assert "connection lost" not in str(info.value)


@pytest.mark.parametrize(
    "body",
    [
        b"not json\n",
        b"[1]\n",
        b'{"stdout": "aGk=", "stderr": "aGk="}\n',
        b'{"stdout": "not base64!"}\n',
        b'{"stdout": 1}\n',
        b'{"exit_code": -1}\n',
        b'{"exit_code": true}\n',
        b'{"exit_code": 1.5}\n',
        b'{"other": 1}\n',
        b'{"exit_code": 0}\n{"stdout": "aGk="}\n',
        b'{"stdout": "' + b"A" * (1 << 20) + b'"}\n',
        b'{"stdout": "' + b"A" * (1 << 20),
    ],
)
def test_malformed_frames(api, body):
    vm, _ = run_server(api, ndjson(body, frame(exit_code=0)))
    with pytest.raises(exedev.ProtocolError):
        api(vm.run("x"))


def test_raw_output_is_not_read(api):
    # An older server streams the command's output itself, which may never end.
    stream = Chunks([b"raw output"])
    vm, _ = run_server(api, httpx.Response(200, headers={"Content-Type": "text/plain"}, stream=stream))
    with pytest.raises(exedev.ProtocolError):
        api(vm.run("x"))
    assert stream.closed


def test_api_errors(api):
    vm, _ = run_server(api, error(422, "failed to connect to VM"), error(403, "token may not ssh"))
    with pytest.raises(exedev.APIError) as info:
        api(vm.run("x"))
    assert (info.value.status, info.value.operation) == (422, "ssh")
    with pytest.raises(exedev.PermissionDeniedError):
        api(vm.run("x"))


def test_missing_vm(api):
    # As delete() does, whatever status /exec uses for it.
    vm, _ = run_server(api, error(422, 'VM "worker" not found'), error(422, 'VM "other" not found'))
    with pytest.raises(exedev.NotFoundError) as info:
        api(vm.run("x"))
    assert info.value.status == 404
    with pytest.raises(exedev.APIError) as other:
        api(vm.run("x"))
    assert type(other.value) is exedev.APIError


def test_stopped_vm(api):
    message = 'VM "worker" is not running.\n\nTo see why it failed, run:\n  ssh exe.dev vm-logs worker'
    vm, _ = run_server(api, error(422, message))
    with pytest.raises(exedev.VMNotRunningError) as info:
        api(vm.run("x"))
    assert isinstance(info.value, exedev.APIError)
    assert info.value.status == 422
    assert str(info.value) == "exe.dev API error (422) during ssh: VM worker is not running"


def test_large_commands_are_uploaded(api):
    program = "print('" + "x" * 70000 + "')"
    vm, server = run_server(api, ran(), ran(b"x"))
    assert api(vm.run(["python3", "-c", program])).stdout == "x"
    write, run = (shlex.split(command) for command in server.commands)
    assert write[:3] == ["ssh", "worker", "write"]
    path = write[3]
    assert path.startswith("/tmp/.exedev-command-")
    assert run == ["ssh", "worker", "run", _wire.UPLOADED_COMMAND.format(path=path)]


def test_timeout(api, clock):
    class Slow(Chunks):
        def __iter__(self):
            yield frame(stdout=b"a")
            clock.now += 10
            yield frame(stdout=b"b")
            yield frame(exit_code=0)

        async def __aiter__(self):
            for chunk in self.__iter__():
                yield chunk

    stream = Slow([])
    vm, _ = run_server(api, streaming(stream))
    with pytest.raises(exedev.CommandTimeoutError) as info:
        api(vm.run("x", timeout=5))
    assert isinstance(info.value, TimeoutError)
    assert info.value.stdout == "ab"
    assert stream.closed


def test_cancellation_closes_the_stream():
    started = asyncio.Event()

    class Endless(Chunks):
        async def __aiter__(self):
            yield frame(stdout=b"tick")
            started.set()
            await asyncio.Event().wait()
            yield b""

    stream = Endless([])
    response = streaming(stream)

    async def main():
        vm = await async_vm(lambda _: response)
        task = asyncio.create_task(vm.run("sleep infinity"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert stream.closed

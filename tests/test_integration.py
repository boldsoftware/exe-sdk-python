"""Tests against exe.dev itself, which create and delete VMs.

The token comes from EXE_TOKEN, or EXE_TOKEN_FILE (a bare token or token
JSON; default ~/.config/exedev/sdk-test-token). Set EXE_SKIP_INTEGRATION=1 to
skip these tests, or EXE_SKIP_SSH_INTEGRATION=1 to skip what needs ssh.
EXE_BASE_URL selects another server, and EXE_TEST_TAG the tag the token
scopes VMs to (default sdk-test).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import threading
import time
from pathlib import Path

import httpx
import pytest

import exedev

pytestmark = pytest.mark.skipif(bool(os.environ.get("EXE_SKIP_INTEGRATION")), reason="EXE_SKIP_INTEGRATION is set")
skip_ssh = pytest.mark.skipif(
    bool(os.environ.get("EXE_SKIP_SSH_INTEGRATION")), reason="EXE_SKIP_SSH_INTEGRATION is set"
)
SSH = not os.environ.get("EXE_SKIP_SSH_INTEGRATION")
READY = {} if SSH else {"wait_until": "created"}
TAG = os.environ.get("EXE_TEST_TAG", "sdk-test")
BASE_URL = os.environ.get("EXE_BASE_URL", "https://exe.dev")


def token() -> str | None:
    if os.environ.get("EXE_TOKEN"):
        return os.environ["EXE_TOKEN"].strip()
    path = Path(os.environ.get("EXE_TOKEN_FILE") or Path.home() / ".config/exedev/sdk-test-token")
    if not path.exists():
        return None
    text = path.read_text().strip()
    return json.loads(text)["token"] if text.startswith("{") else text


NAME = f"sdk-py-{int(time.time() * 1000):x}"
NAMES = {
    "vm": NAME,
    "renamed": f"{NAME}-r",
    "copy": f"{NAME}-c",
    "setup": f"{NAME}-s",
    "missing": f"{NAME}-x",
    "interrupted": f"{NAME}-i",
}


@pytest.fixture(scope="module")
def exe():
    tok = token()
    assert tok, (
        "no API token: set EXE_TOKEN or EXE_TOKEN_FILE (default ~/.config/exedev/sdk-test-token), or EXE_SKIP_INTEGRATION=1"
    )
    with exedev.Client(tok, base_url=BASE_URL) as client:
        yield client
        result = client.delete(*NAMES.values())
        leaked = [n for n in result["failed"] if "not found" not in result.get("errors", {}).get(n, "not found")]
        assert leaked == [], f"cleanup failed: {result}"


@pytest.fixture(scope="module")
def vm(exe):
    try:
        return exe.new(name=NAME, comment="sdk integration test", **READY)
    except exedev.VMReadyError as err:
        pytest.fail(f"{err} (cause: {err.__cause__!r})")


def test_whoami(exe):
    me = exe.whoami()
    assert "@" in me["email"]
    assert isinstance(me["ssh_keys"], list)


def test_new(vm):
    assert isinstance(vm, exedev.VM)
    assert vm.name == NAME
    assert TAG in vm.tags
    assert vm.https_url and vm.https_url.startswith("https://")
    assert vm.created_at is not None
    assert vm.egress == "all"


@skip_ssh
def test_run(vm):
    # The first command after new() needs no retry.
    result = vm.run("echo to-stdout; echo to-stderr >&2; exit 7")
    assert (result.exit_code, result.stdout, result.stderr) == (7, "to-stdout\n", "to-stderr\n")
    with pytest.raises(exedev.CommandError, match="status 7: to-stderr"):
        vm.run("echo to-stderr >&2; exit 7", check=True)


@skip_ssh
def test_run_argv_cwd_env(vm):
    result = vm.run(["printf", "%s|", "a b", "$HOME", "it's"], check=True)
    assert result.stdout == "a b|$HOME|it's|"
    result = vm.run('pwd; printf %s "$GREETING"', cwd="/tmp", env={"GREETING": "it's me"}, check=True)
    assert result.stdout == "/tmp\nit's me"
    assert vm.run("true", cwd="/nonexistent").exit_code != 0


@skip_ssh
def test_streaming_and_limits(vm):
    lines: list[str] = []
    result = vm.run("echo a; echo b", capture=False, on_stdout=lines.append)
    assert (result.exit_code, result.stdout, lines) == (0, "", ["a", "b"])
    assert vm.run("head -c 1024 /dev/zero").stdout_bytes == bytes(1024)
    with pytest.raises(exedev.OutputLimitError):
        vm.run("head -c 16384 /dev/zero", max_output=128)


@skip_ssh
def test_timeout(vm):
    start = time.monotonic()
    with pytest.raises(exedev.CommandTimeoutError) as info:
        vm.run("echo started; sleep 61 & sleep 62", timeout=3)
    assert time.monotonic() - start < 20
    assert info.value.stdout == "started\n"
    # Everything the command started stops with it.
    deadline = time.monotonic() + 10
    while vm.run("pgrep -f 'sleep 6[12]'").exit_code == 0:
        assert time.monotonic() < deadline, "the command's processes outlived its timeout"
        time.sleep(0.5)


@skip_ssh
def test_login_environment(vm):
    vm.write_file(".local/bin/hello-sdk", "#!/bin/sh\necho hello from PATH\n", mode=0o755)
    assert vm.run("hello-sdk", check=True).stdout == "hello from PATH\n"


@skip_ssh
def test_large_commands(vm):
    word = "x" * 100_000
    assert vm.run(f"printf %s {word} | wc -c", check=True).stdout.strip() == "100000"


@skip_ssh
def test_files(vm, tmp_path):
    vm.write_file("sdk/notes.txt", "héllo\n")
    assert vm.read_file("sdk/notes.txt") == "héllo\n".encode()
    data = random.Random(1).randbytes(300_000)
    vm.write_file("/tmp/sdk.bin", data)
    assert vm.read_file("/tmp/sdk.bin") == data
    assert (
        vm.run("sha256sum /tmp/sdk.bin", check=True).stdout.split()[0] == __import__("hashlib").sha256(data).hexdigest()
    )
    with pytest.raises(FileNotFoundError):
        vm.read_file("sdk/missing")
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "a.py").write_text("print('a')\n")
    (src / ".git").mkdir()
    (src / "run.sh").write_text("#!/bin/sh\necho ran\n")
    (src / "run.sh").chmod(0o755)
    vm.upload(src, "proj", exclude=[".git"])
    assert vm.run("./proj/run.sh && ls -A proj", check=True).stdout == "ran\npkg\nrun.sh\n"
    vm.download("proj", tmp_path / "back")
    assert (tmp_path / "back" / "pkg" / "a.py").read_text() == "print('a')\n"
    assert os.access(tmp_path / "back" / "run.sh", os.X_OK)


@skip_ssh
def test_spawn(exe, vm):
    server = vm.spawn("echo serving; exec python3 -m http.server 8123", name="web")
    assert server.poll() is None
    deadline = time.monotonic() + 30
    while vm.run("curl -sf localhost:8123/ >/dev/null").exit_code != 0:
        assert time.monotonic() < deadline, server.logs()
        time.sleep(0.5)
    # Found again, by another client.
    with exedev.Client(token(), base_url=BASE_URL) as other:
        found = other.vm(vm.name).process("web")
        assert found.pid == server.pid
        assert found.logs().stdout == "serving\n"
        found.terminate()
    result = server.wait(timeout=30)
    assert result.exit_code != 0
    job = vm.spawn("sleep 2; echo done", name="job")
    assert job.wait(check=True).stdout == "done\n"
    assert {p.name for p in vm.processes()} >= {"web", "job"}


@skip_ssh
def test_paused(vm):
    vm.pause()
    with pytest.raises(exedev.VMNotRunningError):
        vm.run("true")
    vm.resume()
    assert vm.run("true").ok


@skip_ssh
def test_async(exe, vm):
    async def main() -> list[str]:
        seen: list[str] = []

        async def collect(text: str) -> None:
            seen.append(text)

        async with exedev.AsyncClient(token(), base_url=BASE_URL) as aexe:
            (avm,) = [v for v in await aexe.vms(vm.name) if v.name == vm.name]
            result = await avm.run("echo async", on_stdout=collect, check=True)
            assert result.stdout == "async\n"
            # Cancelling a run stops a command that would never end.
            task = asyncio.create_task(avm.run("yes", capture=False, on_stdout=lambda _: None))
            await asyncio.sleep(2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return seen

    assert asyncio.run(main()) == ["async"]


def test_vms_and_refresh(exe, vm):
    (listed,) = [v for v in exe.vms() if v.name == vm.name]
    assert listed.status
    assert [v.name for v in exe.vms(vm.name)] == [vm.name]
    vm.refresh()
    assert vm.comment == "sdk integration test"


def test_egress(exe, vm):
    try:
        result = vm.set_egress("integrations")
    except exedev.PermissionDeniedError:
        pytest.skip("the token may not run egress")
    try:
        assert result["egress_policy"] == "integrations"
        assert vm.egress == "integrations"
        assert exe.vm(vm.name).egress == "integrations"
    finally:
        vm.set_egress("all")
    assert vm.egress == "all"


def test_copy(vm):
    # A copy of a paused VM has its recent writes: pause() flushes them.
    if SSH:
        vm.write_file("recent.txt", "before pause\n")
    vm.pause()
    try:
        with vm.copy(NAMES["copy"], **READY) as copy:
            assert copy.name == NAMES["copy"]
            assert TAG in copy.tags
            assert copy.comment == "sdk integration test"
            assert copy.created_at is not None
            assert copy.egress == vm.egress
            if SSH:
                assert copy.run("hostname; cat recent.txt", check=True).stdout.split("\n")[:2] == [
                    NAMES["copy"],
                    "before pause",
                ]
        with pytest.raises(exedev.NotFoundError):
            copy.refresh()
    finally:
        vm.resume(**READY)


def test_stats_and_share(vm):
    assert isinstance(vm.stats(), dict)
    assert vm.share.show()["vm_name"] == vm.name


def test_account(exe):
    assert isinstance(exe.ssh_key.list(), (dict, list, str))
    # This token may only manage its own VMs.
    with pytest.raises(exedev.PermissionDeniedError):
        exe.billing.plan()


def test_restart(vm):
    if SSH:
        vm.write_file("recent.txt", "before restart\n")
        sleeper = vm.spawn("sleep 600", name="sleeper")
    vm.restart(**READY)
    if SSH:
        assert vm.read_file("recent.txt") == b"before restart\n"
        with pytest.raises(exedev.ProcessLostError):
            sleeper.wait(timeout=30)


def test_rename(exe, vm):
    vm.rename(NAMES["renamed"])
    assert vm.name == NAMES["renamed"]
    assert vm.ssh_dest and NAMES["renamed"] in vm.ssh_dest
    destinations = (vm.ssh_dest, vm.https_url)
    if SSH:
        assert vm.run("printf renamed").stdout == "renamed"
    vm.refresh()
    assert (vm.ssh_dest, vm.https_url) == destinations
    assert [v.name for v in exe.vms(vm.name)] == [vm.name]


@skip_ssh
def test_setup_failure(exe):
    script = "#!/bin/sh\nsudo true\necho setup-output-marker\necho 'back\\slash' >&2\nexit 3"
    with pytest.raises(exedev.VMSetupError) as info:
        exe.new(name=NAMES["setup"], setup_script=script, wait_until="setup")
    assert info.value.vm.name == NAMES["setup"]
    assert info.value.exit_code == 3
    # The script's output, and only its output: not systemd's or sudo's.
    assert info.value.output.splitlines() == ["setup-output-marker", "back\\slash"]
    # new() deleted the VM it could not hand over.
    with pytest.raises(exedev.NotFoundError):
        exe.vm(NAMES["setup"])


def test_interrupted_new(exe):
    """Interrupted while the server creates it, new() still deletes the VM."""
    main = threading.main_thread().ident
    assert main is not None

    def interrupt(request: httpx.Request) -> None:
        if request.content.startswith(b"new "):
            threading.Timer(0.3, signal.pthread_kill, (main, signal.SIGINT)).start()

    http = httpx.Client(timeout=300, event_hooks={"request": [interrupt]})
    with exedev.Client(token(), base_url=BASE_URL, http_client=http) as client, pytest.raises(KeyboardInterrupt):
        client.new(name=NAMES["interrupted"], **READY)
    with pytest.raises(exedev.NotFoundError):
        exe.vm(NAMES["interrupted"])


def test_delete(exe, vm):
    vm.delete()
    assert vm.name not in [v.name for v in exe.vms()]
    with pytest.raises(exedev.NotFoundError):
        exe.vm(vm.name)
    with pytest.raises(exedev.NotFoundError):
        vm.delete()
    if SSH:
        with pytest.raises(exedev.NotFoundError):
            vm.run("true")
    with pytest.raises(exedev.APIError) as info:
        vm.restart()
    assert info.value.status == 422
    bulk = exe.delete(NAMES["missing"])
    assert (bulk["deleted"], bulk["failed"]) == ([], [NAMES["missing"]])

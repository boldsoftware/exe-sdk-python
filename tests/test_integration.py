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
import time
from pathlib import Path

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
NAMES = {"vm": NAME, "renamed": f"{NAME}-r", "copy": f"{NAME}-c", "setup": f"{NAME}-s", "missing": f"{NAME}-x"}


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
    chunks: list[str] = []
    result = vm.run("echo a; echo b", capture=False, on_stdout=chunks.append)
    assert (result.exit_code, result.stdout, "".join(chunks)) == (0, "", "a\nb\n")
    assert vm.run("head -c 1024 /dev/zero").stdout_bytes == bytes(1024)
    with pytest.raises(exedev.OutputLimitError):
        vm.run("head -c 16384 /dev/zero", max_output=128)


@skip_ssh
def test_timeout(vm):
    start = time.monotonic()
    with pytest.raises(exedev.CommandTimeoutError) as info:
        vm.run("echo started; sleep 60", timeout=3)
    assert time.monotonic() - start < 20
    assert info.value.stdout == "started\n"


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

    assert asyncio.run(main()) == ["async\n"]


def test_vms_and_refresh(exe, vm):
    (listed,) = [v for v in exe.vms() if v.name == vm.name]
    assert listed.status
    assert [v.name for v in exe.vms(vm.name)] == [vm.name]
    vm.refresh()
    assert vm.comment == "sdk integration test"


def test_copy(vm):
    with vm.copy(NAMES["copy"], **READY) as copy:
        assert copy.name == NAMES["copy"]
        assert TAG in copy.tags
        if SSH:
            assert copy.run("hostname", check=True).stdout.strip() == NAMES["copy"]
    with pytest.raises(exedev.NotFoundError):
        copy.refresh()


def test_stats_and_share(vm):
    assert isinstance(vm.stats(), dict)
    assert vm.share.show()["vm_name"] == vm.name


def test_account(exe):
    assert isinstance(exe.ssh_key.list(), (dict, list, str))
    # This token may only manage its own VMs.
    with pytest.raises(exedev.PermissionDeniedError):
        exe.billing.plan()


def test_restart(vm):
    vm.restart(**READY)
    if SSH:
        assert vm.run("true").ok


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
    with pytest.raises(exedev.VMSetupError) as info:
        exe.new(name=NAMES["setup"], setup_script="#!/bin/sh\necho setup-output-marker\nexit 3", wait_until="setup")
    assert info.value.vm.name == NAMES["setup"]
    assert info.value.exit_code == 3
    assert "setup-output-marker" in info.value.output
    info.value.vm.delete()


def test_delete(exe, vm):
    vm.delete()
    assert vm.name not in [v.name for v in exe.vms()]
    with pytest.raises(exedev.APIError) as info:
        exe.vm(NAMES["missing"]).delete()
    assert info.value.status in (404, 422)
    with pytest.raises(exedev.APIError) as info:
        exe.vm(NAMES["missing"]).restart()
    assert info.value.status == 422
    bulk = exe.delete(NAMES["missing"])
    assert (bulk["deleted"], bulk["failed"]) == ([], [NAMES["missing"]])

"""VMs: creating them, waiting for them, and keeping their attributes current."""

from __future__ import annotations

import asyncio
import shlex

import httpx
import pytest
from conftest import error, ran

import exedev
from exedev._wire import SETUP_PROBE

NEW = {
    "vm_name": "worker",
    "tags": ["ci"],
    "ssh_command": "ssh worker.exe.xyz",
    "ssh_dest": "worker.exe.xyz",
    "ssh_host": "worker.exe.xyz",
    "ssh_port": 22,
    "https_url": "https://worker.exe.xyz",
    "proxy_port": 8000,
}
CP = {
    "name": "copy",
    "state": "starting",
    "source": "worker",
    "ssh": "copy.exe.xyz",
    "ssh_host": "copy.exe.xyz",
    "https_url": "https://copy.exe.xyz",
    "tags": ["ci"],
    "elapsed": 1.5,
}
NOT_READY = error(422, "failed to connect to VM: connection refused")
SETUP = f"ssh worker {shlex.quote(SETUP_PROBE)}"


def setup_output(**props: str) -> httpx.Response:
    lines = [f"{k}={v}" for k, v in props.items()]
    journal = props.pop("journal", None)
    text = "\n".join(line for line in lines if not line.startswith("journal=")) + "\n"
    if journal:
        text += "---\n" + journal
    return ran(text.encode())


def test_new_waits_for_ssh(api, clock):
    exe, server = api.client({"new --name=worker --tag=ci": NEW, "ssh worker true": [NOT_READY, NOT_READY, ran()]})
    vm = api(exe.new(name="worker", tags=["ci"]))
    assert server.commands == ["new --name=worker --tag=ci"] + ["ssh worker true"] * 3
    assert clock.sleeps == [0.1, 0.2]
    assert vm.name == "worker"
    assert vm.status == "running"
    assert vm.ssh_dest == "worker.exe.xyz"
    assert vm.https_url == "https://worker.exe.xyz"
    assert vm.tags == ("ci",)


def test_new_flags(api):
    exe, server = api.client(lambda _: NEW)
    api(
        exe.new(
            image="ubuntu:24.04",
            cpu=2,
            memory="4G",
            disk=20,
            env={"A": "1", "B": "two words"},
            no_email=True,
            setup_script="apt-get install -y jq",
            wait_until="created",
        )
    )
    assert server.commands == [
        (
            "new --cpu=2 --disk=20 --env=A=1 --env='B=two words' --image=ubuntu:24.04 --memory=4G --no-email "
            "--setup-script='apt-get install -y jq'"
        )
    ]


def test_new_created_does_not_wait(api):
    exe, server = api.client({"new": NEW})
    vm = api(exe.new(wait_until="created"))
    assert server.commands == ["new"]
    assert vm.status is None


def test_readiness_arguments_are_checked_first(api):
    exe, server = api.client({"new": NEW})
    with pytest.raises(ValueError, match="wait_until"):
        api(exe.new(wait_until="running"))
    with pytest.raises(ValueError, match="timeout"):
        api(exe.new(timeout=-1))
    with pytest.raises(ValueError, match="timeout"):
        api(exe.vm("worker").restart(timeout=0))
    assert server.commands == []


def test_new_reports_prompt_errors(api):
    exe, _ = api.client({"new --prompt=hi": {**NEW, "error": "Shelley is busy"}})
    vm = api(exe.new(prompt="hi", wait_until="created"))
    assert vm.prompt_error == "Shelley is busy"


def test_readiness_deadline(api, clock):
    exe, server = api.client({"new": NEW, "ssh worker true": NOT_READY})
    with pytest.raises(exedev.VMReadyError) as info:
        api(exe.new(timeout=10))
    assert str(info.value) == "VM worker is not ready: SSH readiness timed out"
    assert info.value.vm.name == "worker"
    assert isinstance(info.value.__cause__, exedev.APIError)
    assert sum(clock.sleeps) == pytest.approx(10)
    assert max(clock.sleeps) == 2.0
    assert "rm worker" not in server.commands


def test_readiness_without_ssh_permission_fails_fast(api, clock):
    exe, server = api.client({"new": NEW, "ssh worker true": error(403, "forbidden")})
    with pytest.raises(exedev.VMReadyError, match='use wait_until="created"'):
        api(exe.new())
    assert server.commands == ["new", "ssh worker true"]


@pytest.mark.parametrize(
    "answer",
    [error(404, "VM not found"), error(422, "VM has no SSH configured"), ran(exit_code=1)],
)
def test_readiness_fails_fast(api, clock, answer):
    exe, server = api.client({"new": NEW, "ssh worker true": answer})
    with pytest.raises(exedev.VMReadyError, match="is not SSH-ready"):
        api(exe.new())
    assert server.commands == ["new", "ssh worker true"]


def test_readiness_retries_interruptions(api, clock):
    exe, server = api.client(
        {"new": NEW, "ssh worker true": [httpx.ConnectError("reset"), error(422, "failed to dial"), ran()]}
    )
    api(exe.new())
    assert server.commands.count("ssh worker true") == 3


def test_wait_for_setup(api, clock):
    pending = setup_output(Setup="present", LoadState="loaded", ActiveState="activating")
    done = setup_output(
        Setup="present",
        LoadState="loaded",
        ActiveState="inactive",
        Result="success",
        ExecMainExitTimestampMonotonic="5",
    )
    exe, server = api.client({"new --setup-script=make": NEW, SETUP: [pending, pending, done]})
    vm = api(exe.new(setup_script="make", wait_until="setup"))
    assert server.commands == ["new --setup-script=make", SETUP, SETUP, SETUP]
    assert clock.sleeps == [1.0, 2.0]
    assert vm.status == "running"


def test_setup_failure(api, clock):
    failed = setup_output(
        Setup="present",
        LoadState="loaded",
        ActiveState="failed",
        Result="exit-code",
        ExecMainCode="1",
        ExecMainStatus="3",
        journal="E: Unable to locate package nope\n",
    )
    exe, _ = api.client({"new --setup-script=x": NEW, SETUP: failed})
    with pytest.raises(exedev.VMSetupError) as info:
        api(exe.new(setup_script="x", wait_until="setup"))
    err = info.value
    assert isinstance(err, exedev.VMReadyError)
    assert str(err) == "VM worker setup script failed (exit code 3)"
    assert (err.result, err.exit_code, err.signal) == ("exit-code", 3, None)
    assert err.output == "E: Unable to locate package nope\n"


def test_setup_unsupported(api, clock):
    exe, _ = api.client(
        {"new --image=alpine --setup-script=x": NEW, SETUP: setup_output(Setup="present", State="unsupported")}
    )
    with pytest.raises(exedev.VMSetupError, match="image does not run setup scripts") as info:
        api(exe.new(image="alpine", setup_script="x", wait_until="setup"))
    assert info.value.result == "unsupported"


def test_wait_until_ready(api, clock):
    exe, _server = api.client({"ssh worker true": [NOT_READY, ran()]})
    vm = exe.vm("worker")
    assert api(vm.wait_until_ready()) is None
    assert vm.status == "running"
    with pytest.raises(ValueError):
        api(vm.wait_until_ready("created"))


def test_sync_context_manager_deletes():
    commands = []

    def handle(request):
        commands.append(request.content.decode())
        if request.content == b"rm worker":
            return httpx.Response(200, json={"deleted": ["worker"], "failed": []})
        return httpx.Response(200, json=NEW)

    exe = exedev.Client("tok", http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(RuntimeError), exe.new(wait_until="created") as vm:
        assert vm.name == "worker"
        raise RuntimeError("boom")
    assert commands == ["new", "rm worker"]


def test_async_context_manager_deletes():
    commands = []

    def handle(request):
        commands.append(request.content.decode())
        if request.content == b"rm worker":
            return httpx.Response(200, json={"deleted": [], "failed": ["worker"], "not_found": ["worker"]})
        return httpx.Response(200, json=NEW)

    exe = exedev.AsyncClient("tok", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))

    async def main():
        async with exe.new(wait_until="created") as vm:
            assert isinstance(vm, exedev.AsyncVM)
        vm = await exe.new(wait_until="created")
        async with vm:
            pass

    asyncio.run(main())
    # A VM already gone is not an error at the end of the block.
    assert commands == ["new", "rm worker", "new", "rm worker"]


def test_only_new_vms_are_context_managers(api):
    exe, _ = api.client({})
    vm = exe.vm("worker")
    with pytest.raises(TypeError):
        if api.sync:
            with vm:
                pass
        else:

            async def main():
                async with vm:
                    pass

            asyncio.run(main())


def test_copy(api, clock):
    exe, server = api.client({"cp --cpu=4 worker copy": CP, "ssh copy true": ran()})
    copy = api(exe.vm("worker").copy("copy", cpu=4))
    assert server.commands == ["cp --cpu=4 worker copy", "ssh copy true"]
    assert (copy.name, copy.ssh_dest, copy.https_url, copy.status, copy.tags) == (
        "copy",
        "copy.exe.xyz",
        "https://copy.exe.xyz",
        "running",
        ("ci",),
    )


def test_copy_without_tags(api):
    exe, server = api.client({"cp --copy-tags=false worker": CP})
    api(exe.vm("worker").copy(copy_tags=False, wait_until="created"))
    assert server.commands == ["cp --copy-tags=false worker"]


def test_delete(api):
    exe, _server = api.client(
        {
            "rm worker": {"deleted": ["worker"], "failed": []},
            "rm gone": {"deleted": [], "failed": ["gone"], "not_found": ["gone"], "errors": {"gone": "not found"}},
            "rm stuck": {"deleted": [], "failed": ["stuck"], "errors": {"stuck": "busy"}},
        }
    )
    assert api(exe.vm("worker").delete()) is None
    with pytest.raises(exedev.NotFoundError):
        api(exe.vm("gone").delete())
    with pytest.raises(exedev.APIError) as info:
        api(exe.vm("stuck").delete())
    assert info.value.status == 422


def test_refresh(api):
    listing = {
        "vms": [{"vm_name": "worker", "ssh_dest": "worker.exe.xyz", "https_url": "https://w", "status": "paused"}]
    }
    exe, _server = api.client({"ls worker": listing, "ls gone": {"vms": []}})
    vm = exe.vm("worker")
    assert api(vm.refresh()) is None
    assert (vm.status, vm.ssh_dest) == ("paused", "worker.exe.xyz")
    with pytest.raises(exedev.NotFoundError):
        api(exe.vm("gone").refresh())


def test_rename(api):
    exe, _server = api.client(
        {
            "rename worker builder": {
                "status": "renamed",
                "old_name": "worker",
                "new_name": "builder",
                "ssh_dest": "builder.exe.xyz",
                "https_url": "https://builder.exe.xyz",
            },
            "rename builder other": {"status": "renamed", "old_name": "builder", "new_name": "nope"},
        }
    )
    vm = exe.vm("worker")
    result = api(vm.rename("builder"))
    assert result["new_name"] == "builder"
    assert (vm.name, vm.ssh_dest, vm.https_url) == ("builder", "builder.exe.xyz", "https://builder.exe.xyz")
    with pytest.raises(exedev.ProtocolError):
        api(vm.rename("other"))
    assert vm.name == "builder"


def test_lifecycle(api, clock):
    exe, server = api.client(
        {
            "pause worker": {"vm_name": "worker", "status": "paused"},
            "resume worker": {"vm_name": "worker", "status": "running", "cold_booted": "false"},
            "restart worker": {"status": "restarted", "vm_name": "worker"},
            "ssh worker true": [NOT_READY, ran()],
        }
    )
    vm = exe.vm("worker")
    api(vm.pause())
    assert vm.status == "paused"
    assert api(vm.resume())["cold_booted"] == "false"
    assert vm.status == "running"
    api(vm.restart(wait_until="created"))
    api(vm.restart())
    assert server.commands == [
        "pause worker",
        "resume worker",
        "ssh worker true",
        "ssh worker true",
        "restart worker",
        "restart worker",
        "ssh worker true",
    ]


def test_tags_and_comment(api):
    exe, _server = api.client(
        {
            "tag worker a b": {"vm_name": "worker", "tags": ["a", "b", "c"]},
            "tag --d worker c": {"vm_name": "worker", "tags": ["a", "b"]},
            "comment worker 'hello there'": {"vm_name": "worker", "comment": "hello there"},
            "comment other x": {"vm_name": "other", "comment": "x"},
        }
    )
    vm = exe.vm("worker")
    api(vm.tag("a", "b"))
    assert vm.tags == ("a", "b", "c")
    api(vm.untag("c"))
    assert vm.tags == ("a", "b")
    api(vm.set_comment("hello there"))
    assert vm.comment == "hello there"
    # A result about another VM does not change this one.
    other = exe.vm("other")
    api(other.set_comment("x"))
    assert vm.comment == "hello there"


def test_vm_namespaces(api):
    exe, server = api.client(lambda _: {})
    vm = exe.vm("worker")
    api(vm.share.add("friend@example.com", message="have a look"))
    api(vm.share.set_public())
    api(vm.domain.add("example.com"))
    api(vm.shelley.install())
    api(vm.resize(memory="8G"))
    api(vm.stats(range="7d"))
    assert server.commands == [
        "share add --message='have a look' worker friend@example.com",
        "share set-public worker",
        "domain add worker example.com",
        "shelley install worker",
        "resize --memory=8G worker",
        "stat --range=7d worker",
    ]

"""VMs: creating them, waiting for them, and keeping their attributes current."""

from __future__ import annotations

import asyncio
import shlex
import signal
import threading
import time

import httpx
import pytest
from conftest import error, ran, record

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
    "egress_policy": "integrations",
}
CP = {
    "name": "copy",
    "state": "starting",
    "source": "worker",
    "ssh": "copy.exe.xyz",
    "ssh_host": "copy.exe.xyz",
    "https_url": "https://copy.exe.xyz",
    "tags": ["ci"],
    "egress_policy": "all",
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
    exe, server = api.client(
        {"new --name=worker --tag=ci --no-email": NEW, "ssh worker true": [NOT_READY, NOT_READY, ran()]}
    )
    vm = api(exe.new(name="worker", tags=["ci"]))
    assert server.commands == ["new --name=worker --tag=ci --no-email"] + ["ssh worker true"] * 3
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
            setup_script="apt-get install -y jq",
            egress="integrations",
            wait_until="created",
        )
    )
    vm = api(exe.new(email=True, wait_until="created"))
    assert vm.egress == "integrations"
    assert server.commands == [
        (
            "new --cpu=2 --disk=20 --egress=integrations --env=A=1 --env='B=two words' --image=ubuntu:24.04 "
            "--memory=4G --setup-script='apt-get install -y jq' --no-email"
        ),
        "new",
    ]


def test_setup_scripts_are_sent_as_written(api):
    # The server reads escapes in a setup script; the SDK sends backslashes as such.
    exe, server = api.client(lambda _: NEW)
    api(exe.new(setup_script="#!/bin/sh\nprintf 'a\\tb\\n'\n", wait_until="created"))
    assert shlex.split(server.commands[0])[1] == "--setup-script=#!/bin/sh\nprintf 'a\\\\tb\\\\n'\n"


def test_new_created_does_not_wait(api):
    exe, server = api.client({"new --no-email": NEW})
    vm = api(exe.new(wait_until="created"))
    assert server.commands == ["new --no-email"]
    assert vm.status == "starting"


def test_readiness_arguments_are_checked_first(api):
    exe, vm, server = api.vm({"new --no-email": NEW})
    with pytest.raises(ValueError, match="wait_until"):
        api(exe.new(wait_until="running"))
    with pytest.raises(ValueError, match="timeout"):
        api(exe.new(timeout=-1))
    with pytest.raises(ValueError, match="timeout"):
        api(vm.restart(timeout=0))
    assert server.commands == []


def test_new_reports_prompt_errors(api):
    exe, _ = api.client({"new --prompt=hi --no-email": {**NEW, "error": "Shelley is busy"}})
    vm = api(exe.new(prompt="hi", wait_until="created"))
    assert vm.prompt_error == "Shelley is busy"


RM = {"deleted": ["worker"], "failed": []}


def test_readiness_deadline(api, clock):
    exe, server = api.client({"new --no-email": NEW, "ssh worker true": NOT_READY, "rm worker": RM})
    with pytest.raises(exedev.VMReadyError) as info:
        api(exe.new(timeout=10))
    assert str(info.value) == "VM worker is not ready: SSH readiness timed out"
    assert info.value.vm.name == "worker"
    assert isinstance(info.value.__cause__, exedev.APIError)
    assert sum(clock.sleeps) == pytest.approx(10)
    assert max(clock.sleeps) == 2.0
    # The caller never got the VM, so new() deleted it.
    assert server.commands[-1] == "rm worker"


def test_keep_on_failure(api, clock):
    exe, server = api.client({"new --no-email": NEW, "ssh worker true": NOT_READY})
    with pytest.raises(exedev.VMReadyError) as info:
        api(exe.new(timeout=10, keep_on_failure=True))
    assert info.value.vm.name == "worker"
    assert "rm worker" not in server.commands


def test_new_deletes_the_vm_when_interrupted():
    commands = []

    def handle(request):
        commands.append(request.content.decode())
        if commands[-1] == "ssh worker true":
            raise KeyboardInterrupt
        return httpx.Response(200, json=RM if commands[-1] == "rm worker" else NEW)

    exe = exedev.Client("tok", http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(KeyboardInterrupt):
        exe.new()
    assert commands == ["new --no-email", "ssh worker true", "rm worker"]


def test_new_deletes_the_vm_when_cancelled():
    commands = []

    def handle(request):
        commands.append(request.content.decode())
        if commands[-1] == "ssh worker true":
            return NOT_READY
        return httpx.Response(200, json=RM if commands[-1] == "rm worker" else NEW)

    async def main():
        exe = exedev.AsyncClient("tok", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        task = asyncio.create_task(exe.new())
        while "ssh worker true" not in commands:
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert commands == ["new --no-email", "ssh worker true", "rm worker"]


def interrupting(first: str, interrupts: int = 1):
    """A handler for which a Ctrl-C arrives while the server runs first,
    which creates the VM: it is done, but the response is yet to come."""
    commands = []
    main = threading.main_thread().ident
    assert main is not None

    def handle(request):
        commands.append(request.content.decode())
        if commands[-1] == first:
            for _ in range(interrupts):
                signal.pthread_kill(main, signal.SIGINT)
                time.sleep(0.05)
        if commands[-1].startswith("rm "):
            return httpx.Response(200, json={"deleted": [commands[-1][3:]], "failed": []})
        return httpx.Response(200, json=CP if first.startswith("cp ") else NEW)

    return commands, handle


def test_new_deletes_the_vm_when_interrupted_during_creation():
    commands, handle = interrupting("new --no-email")
    exe = exedev.Client("tok", http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(KeyboardInterrupt):
        exe.new()
    assert commands == ["new --no-email", "rm worker"]


def test_copy_deletes_the_vm_when_interrupted_during_creation():
    commands, handle = interrupting("cp worker")
    exe = exedev.Client("tok", http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    vm = exedev.VM(exe._t, exedev._wire.vm_info(NEW, "new"))
    with pytest.raises(KeyboardInterrupt):
        vm.copy(flush=False)
    assert commands == ["cp worker", "rm copy"]


def test_a_second_interrupt_abandons_the_vm():
    commands, handle = interrupting("new --no-email", interrupts=2)
    exe = exedev.Client("tok", http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(KeyboardInterrupt):
        exe.new()
    time.sleep(0.2)
    assert commands == ["new --no-email"]


def test_new_deletes_the_vm_when_cancelled_during_creation():
    commands = []

    async def handle(request):
        commands.append(request.content.decode())
        if commands[-1] == "new --no-email":
            await asyncio.sleep(0.05)
        return httpx.Response(200, json=RM if commands[-1] == "rm worker" else NEW)

    async def main():
        exe = exedev.AsyncClient("tok", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        task = asyncio.create_task(exe.new())
        while not commands:
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert commands == ["new --no-email", "rm worker"]


def test_readiness_without_ssh_permission_fails_fast(api, clock):
    exe, server = api.client({"new --no-email": NEW, "ssh worker true": error(403, "forbidden"), "rm worker": RM})
    with pytest.raises(exedev.VMReadyError, match='use wait_until="created"'):
        api(exe.new())
    assert server.commands == ["new --no-email", "ssh worker true", "rm worker"]


@pytest.mark.parametrize(
    "answer",
    [error(404, "VM not found"), error(422, "VM has no SSH configured"), ran(exit_code=1)],
)
def test_readiness_fails_fast(api, clock, answer):
    exe, server = api.client({"new --no-email": NEW, "ssh worker true": answer, "rm worker": RM})
    with pytest.raises(exedev.VMReadyError, match="is not SSH-ready"):
        api(exe.new())
    assert server.commands == ["new --no-email", "ssh worker true", "rm worker"]


def test_readiness_retries_interruptions(api, clock):
    exe, server = api.client(
        {"new --no-email": NEW, "ssh worker true": [httpx.ConnectError("reset"), error(422, "failed to dial"), ran()]}
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
    exe, server = api.client({"new --setup-script=make --no-email": NEW, SETUP: [pending, pending, done]})
    vm = api(exe.new(setup_script="make", wait_until="setup"))
    assert server.commands == ["new --setup-script=make --no-email", SETUP, SETUP, SETUP]
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
    exe, _ = api.client({"new --setup-script=x --no-email": NEW, SETUP: failed})
    with pytest.raises(exedev.VMSetupError) as info:
        api(exe.new(setup_script="x", wait_until="setup"))
    err = info.value
    assert isinstance(err, exedev.VMReadyError)
    assert str(err) == "VM worker setup script failed (exit code 3)"
    assert (err.result, err.exit_code, err.signal) == ("exit-code", 3, None)
    assert err.output == "E: Unable to locate package nope\n"


def test_setup_unsupported(api, clock):
    exe, _ = api.client(
        {
            "new --image=alpine --setup-script=x --no-email": NEW,
            SETUP: setup_output(Setup="present", State="unsupported"),
        }
    )
    with pytest.raises(exedev.VMSetupError, match="image does not run setup scripts") as info:
        api(exe.new(image="alpine", setup_script="x", wait_until="setup"))
    assert info.value.result == "unsupported"


def test_wait_until_ready(api, clock):
    _, vm, _ = api.vm({"ssh worker true": [NOT_READY, ran()]})
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
    assert commands == ["new --no-email", "rm worker"]


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
    assert commands == ["new --no-email", "rm worker", "new --no-email", "rm worker"]


def test_only_new_vms_are_context_managers(api):
    _, vm, _ = api.vm({})
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
    _, vm, server = api.vm({"ssh worker flush": ran(), "cp --cpu=4 worker copy": CP, "ssh copy true": ran()})
    copy = api(vm.copy("copy", cpu=4))
    # It flushes the VM's writes to disk first, so that the copy has them.
    assert server.commands == ["ssh worker flush", "cp --cpu=4 worker copy", "ssh copy true"]
    assert (copy.name, copy.ssh_dest, copy.https_url, copy.status, copy.tags, copy.egress) == (
        "copy",
        "copy.exe.xyz",
        "https://copy.exe.xyz",
        "running",
        ("ci",),
        "all",
    )


def test_copy_without_tags(api):
    _, vm, server = api.vm({"cp --copy-tags=false worker": CP})
    api(vm.copy(copy_tags=False, flush=False, wait_until="created"))
    assert server.commands == ["cp --copy-tags=false worker"]


def test_copy_flush_is_best_effort(api, clock):
    _, vm, server = api.vm({"ssh worker flush": error(403, "no ssh"), "cp worker": CP})
    api(vm.copy(wait_until="created"))
    assert server.commands == ["ssh worker flush", "cp worker"]


def test_delete(api):
    exe, _server = api.client(
        {
            "rm worker": {"deleted": ["worker"], "failed": []},
            "rm gone": {"deleted": [], "failed": ["gone"], "not_found": ["gone"], "errors": {"gone": "not found"}},
            "rm stuck": {"deleted": [], "failed": ["stuck"], "errors": {"stuck": "busy"}},
        }
    )
    assert api(api(exe.vm("worker")).delete()) is None
    with pytest.raises(exedev.NotFoundError):
        api(api(exe.vm("gone")).delete())
    with pytest.raises(exedev.APIError) as info:
        api(api(exe.vm("stuck")).delete())
    assert info.value.status == 422


def test_refresh(api):
    paused = {"vms": [record("worker", status="paused", comment="hi", egress_policy="integrations")]}
    exe, _server = api.client({"ls worker": [{"vms": [record("worker")]}, paused, {"vms": []}]})
    vm = api(exe.vm("worker"))
    assert vm.status == "running"
    assert api(vm.refresh()) is None
    assert (vm.status, vm.comment, vm.egress) == ("paused", "hi", "integrations")
    with pytest.raises(exedev.NotFoundError):
        api(vm.refresh())


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
    vm = api(exe.vm("worker"))
    result = api(vm.rename("builder"))
    assert result["new_name"] == "builder"
    assert (vm.name, vm.ssh_dest, vm.https_url) == ("builder", "builder.exe.xyz", "https://builder.exe.xyz")
    with pytest.raises(exedev.ProtocolError):
        api(vm.rename("other"))
    assert vm.name == "builder"


def test_lifecycle(api, clock):
    _, vm, server = api.vm(
        {
            "pause worker": {"vm_name": "worker", "status": "paused"},
            "resume worker": {"vm_name": "worker", "status": "running", "cold_booted": "false"},
            "restart worker": {"status": "restarted", "vm_name": "worker"},
            "ssh worker true": [NOT_READY, ran()],
            "ssh worker flush": ran(),
        }
    )
    api(vm.pause())
    assert vm.status == "paused"
    assert api(vm.resume())["cold_booted"] == "false"
    assert vm.status == "running"
    api(vm.restart(wait_until="created"))
    api(vm.restart(flush=False))
    api(vm.pause(flush=False))
    assert server.commands == [
        # Recent writes reach the disk before a pause, so copies of the
        # paused VM have them, and before a restart, which loses the rest.
        "ssh worker flush",
        "pause worker",
        "resume worker",
        "ssh worker true",
        "ssh worker true",
        "ssh worker flush",
        "restart worker",
        "restart worker",
        "ssh worker true",
        "pause worker",
    ]


def test_flushing_is_best_effort(api):
    _, vm, server = api.vm(
        {"ssh worker flush": error(403, "no ssh"), "pause worker": {"vm_name": "worker", "status": "paused"}}
    )
    api(vm.pause())
    assert server.commands == ["ssh worker flush", "pause worker"]


def test_tags_and_comment(api):
    exe, _server = api.client(
        {
            "tag worker a b": {"vm_name": "worker", "tags": ["a", "b", "c"]},
            "tag --d worker c": {"vm_name": "worker", "tags": ["a", "b"]},
            "comment worker 'hello there'": {"vm_name": "worker", "comment": "hello there"},
            "comment other x": {"vm_name": "other", "comment": "x"},
        }
    )
    vm = api(exe.vm("worker"))
    api(vm.tag("a", "b"))
    assert vm.tags == ("a", "b", "c")
    api(vm.untag("c"))
    assert vm.tags == ("a", "b")
    api(vm.set_comment("hello there"))
    assert vm.comment == "hello there"
    # A result about another VM does not change this one.
    other = api(exe.vm("other"))
    api(other.set_comment("x"))
    assert vm.comment == "hello there"


def test_egress(api):
    exe, server = api.client(
        {
            "ls worker": {"vms": [record("worker", egress_policy="all")]},
            "ls shared": {"vms": [record("shared")]},
            "egress worker integrations": {"vm_name": "worker", "egress_policy": "integrations"},
        }
    )
    vm = api(exe.vm("worker"))
    assert vm.egress == "all"
    assert api(vm.set_egress("integrations")) == {"vm_name": "worker", "egress_policy": "integrations"}
    assert vm.egress == "integrations"
    # Only a VM's owner is told its policy.
    assert api(exe.vm("shared")).egress is None
    assert server.commands == ["ls worker", "egress worker integrations", "ls shared"]
    assert not hasattr(exe, "egress")


def test_vm_namespaces(api):
    _, vm, server = api.vm(lambda _: {})
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


def test_resize_and_restart(api, clock):
    _, vm, server = api.vm(
        {
            "resize --memory=8G worker": {"vm_name": "worker", "memory": "8G"},
            "restart worker": {"status": "restarted", "vm_name": "worker"},
            "ssh worker true": ran(),
            "ssh worker flush": ran(),
        }
    )
    api(vm.resize(memory="8G", restart=True))
    assert server.commands == ["resize --memory=8G worker", "ssh worker flush", "restart worker", "ssh worker true"]

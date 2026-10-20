"""Background processes: spawn, and the processes it starts, against a local fake VM."""

from __future__ import annotations

import os
import signal
import time

import pytest

import exedev


def test_spawn_and_wait(api, local):
    vm, _, server = local
    process = api(vm.spawn("echo out; echo err >&2; exit 3", name="job"))
    assert (process.name, process.vm) == ("job", vm)
    assert process.pid > 0
    result = api(process.wait())
    assert (result.exit_code, result.stdout, result.stderr) == (3, "out\n", "err\n")
    assert process.exit_code == 3
    assert api(process.poll()) == 3
    with pytest.raises(exedev.CommandError):
        api(process.wait(check=True))
    assert api(process.wait(capture=False)).stdout == ""
    assert server.commands[0].startswith("ssh worker spawn job ")


def test_spawn_options(api, local):
    vm, machine, _ = local
    (machine.home / "work").mkdir()
    process = api(vm.spawn(["sh", "-c", 'pwd; echo "$GREETING"'], cwd="work", env={"GREETING": "it's me"}))
    assert len(process.name) == 8
    assert api(process.wait()).stdout == f"{machine.home}/work\nit's me\n"


def test_running_processes(api, local):
    vm, _, _ = local
    server = api(vm.spawn("echo started; sleep 60", name="server"))
    assert api(server.poll()) is None
    assert server.exit_code is None
    found = api(vm.process("server"))
    assert (found.name, found.pid) == ("server", server.pid)
    assert [(p.name, p.pid) for p in api(vm.processes())] == [("server", server.pid)]
    with pytest.raises(exedev.ExeError, match="already running"):
        api(vm.spawn("true", name="server"))
    deadline = time.monotonic() + 5
    while api(server.logs()).stdout != "started\n" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert api(server.logs(tail=1)) == exedev.Logs("started\n", "")
    api(server.kill())
    assert api(server.wait()).exit_code == 128 + signal.SIGKILL
    # Its name is free again.
    api(api(vm.spawn("true", name="server")).wait(check=True))


def test_signals_reach_the_whole_process_group(api, local):
    vm, _, _ = local
    process = api(vm.spawn("trap 'echo bye; exit 5' HUP; sleep 60 & wait", name="trap"))
    deadline = time.monotonic() + 5
    while api(vm.processes())[0].exit_code is None and time.monotonic() < deadline:
        api(process.send_signal("HUP"))
        if api(process.poll()) is not None:
            break
        time.sleep(0.1)
    result = api(process.wait())
    assert (result.exit_code, result.stdout) == (5, "bye\n")
    with pytest.raises(ValueError):
        api(process.send_signal("NOPE; rm -rf /"))


def test_context_manager_kills(api, local):
    vm, _, _ = local
    if api.sync:
        with vm.spawn("sleep 60", name="cm") as process:
            pass
    else:
        import asyncio

        async def main():
            async with vm.spawn("sleep 60", name="cm") as process:
                return process

        process = asyncio.run(main())
    assert api(process.wait()).exit_code == 128 + signal.SIGKILL


def test_missing_processes(api, local):
    vm, _, _ = local
    with pytest.raises(ProcessLookupError):
        api(vm.process("nope"))
    with pytest.raises(ValueError):
        api(vm.process("../etc"))
    assert api(vm.processes()) == []


def test_lost_processes(api, local):
    vm, machine, _ = local
    process = api(vm.spawn("sleep 60", name="lost"))
    # As if the VM had restarted since.
    (machine.home / ".local/state/exedev/processes/lost/boot").write_text("another boot\n")
    with pytest.raises(exedev.ProcessLostError):
        api(process.poll())
    with pytest.raises(exedev.ProcessLostError):
        api(process.wait())
    os.killpg(process.pid, signal.SIGKILL)


@pytest.mark.parametrize(
    "damage",
    [
        {},
        {"pid": ""},
        {"pid": None},
        {"pid": "\x00\x00\x00\x00"},
        {"boot": None, "pid": ""},
        {"exit": ""},
        {"exit": "\x00\x00"},
    ],
)
def test_records_damaged_by_a_crash_are_lost(api, local, damage):
    # A VM that stopped before writing a record out leaves files missing or
    # empty. Such a process is lost, whatever its record lacks.
    vm, machine, _ = local
    api(api(vm.spawn("true", name="crashed")).wait())
    record = machine.home / ".local/state/exedev/processes/crashed"
    (record / "boot").write_text("another boot\n")
    (record / "exit").unlink()
    for name, content in damage.items():
        if content is None:
            (record / name).unlink()
        else:
            (record / name).write_text(content)
    process = api(vm.process("crashed"))
    assert process.exit_code is None
    assert "lost" in repr(process)
    with pytest.raises(exedev.ProcessLostError):
        api(process.poll())
    with pytest.raises(exedev.ProcessLostError):
        api(process.wait())
    assert [p.name for p in api(vm.processes())] == ["crashed"]


def test_an_exit_status_outlives_a_damaged_pid(api, local):
    vm, machine, _ = local
    api(api(vm.spawn("exit 4", name="crashed")).wait())
    record = machine.home / ".local/state/exedev/processes/crashed"
    (record / "boot").write_text("another boot\n")
    (record / "pid").write_text("")
    process = api(vm.process("crashed"))
    assert (process.pid, process.exit_code) == (0, 4)
    assert api(process.wait()).exit_code == 4


def test_spawn_writes_its_records_out(api, local):
    # So that they survive the VM stopping without flushing its disk.
    vm, _, server = local
    api(vm.spawn("true", name="durable"))
    script = next(r for r in server.raw if "durable" in r)
    assert 'sync "$d/pid"' in script

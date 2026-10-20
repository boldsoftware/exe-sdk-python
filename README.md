# exedev

The official Python SDK for [exe.dev](https://exe.dev): create VMs, run
commands on them, and manage your account, from sync or async code.

```sh
pip install exedev    # or: uv add exedev
```

## Quick start

```python
import exedev

exe = exedev.Client()  # reads EXE_TOKEN

with exe.new(image="ubuntu:24.04") as vm:  # deleted at the end of the block
    print(vm.name, vm.https_url)
    result = vm.run("uname -a", check=True)
    print(result.stdout)
```

`new()` returns once the VM runs commands, so the first `run()` needs no
retry loop, and if it fails or is interrupted on the way, it deletes the VM
rather than leak it. Create a token with `ssh exe.dev ssh-key generate-api-key`; its
permissions decide which methods work.

The same API is available for asyncio:

```python
import asyncio
import exedev


async def main() -> None:
    async with exedev.AsyncClient() as exe:
        async with exe.new() as vm:
            result = await vm.run(["python3", "-c", "print('hello')"], check=True)
            print(result.stdout)


asyncio.run(main())
```

`Client` and `AsyncClient`, and `VM` and `AsyncVM`, have the same methods;
the async ones are coroutines.

## Running commands

`vm.run()` runs a command once, in the VM user's login shell, and returns a
`CommandResult` with `exit_code`, `stdout`, `stderr` (text), `stdout_bytes`,
`stderr_bytes` (exact bytes), and `ok`.

```python
vm.run("make -j8 && make test")               # a shell command line
vm.run(["cat", "/etc/os-release"])            # an argument list, quoted for you
vm.run("npm test", cwd="/app", env={"CI": "1"})
vm.run("pytest", check=True)                  # nonzero exit raises CommandError
vm.run("./long-job", timeout=600)             # stopped after 10 minutes
vm.run(llm_written_program)                   # any size: long commands are uploaded first
```

As with `subprocess.run`, a nonzero exit status is a result, not an
exception, unless you pass `check=True`. The `CommandError` it raises carries
the `result`, and its message ends with the last line of standard error.

Stream output as it arrives with `on_stdout` and `on_stderr`, which receive
it a line at a time, without the line ending (and may be coroutine functions
with `AsyncVM`):

```python
vm.run("apt-get install -y ffmpeg", on_stdout=print, on_stderr=print)
```

Output is captured up to `max_output` bytes (8 MiB by default); beyond that,
`OutputLimitError`. For large output, pass `capture=False` and stream it.

If the connection breaks before the exit status arrives, `run()` raises
`CommandInterruptedError` with whatever output it captured: the command may
or may not have finished, so do not blindly retry one that is unsafe to run
twice. Closing the connection, whether by `timeout`, a callback raising, or
cancelling the asyncio task, stops the command and everything it started.

`run()` lasts only as long as its request. For servers and long jobs, use
`spawn()`.

## Background processes

`vm.spawn()` starts a command and returns a `Process` at once. The process
runs on its own, whatever becomes of your program, and its output goes to
files on the VM.

```python
server = vm.spawn("python3 -m http.server 8000", name="web", cwd="site")
print(vm.url(8000))                   # its URL through the exe.dev proxy
server.logs(tail=20)                  # Logs(stdout=..., stderr=...)
server.poll()                         # None while it runs, then its exit status
server.terminate()                    # or kill(), or send_signal("HUP")

job = vm.spawn("make -j8 all")
result = job.wait(timeout=3600, check=True)  # a CommandResult; survives lost connections

vm.process("web")                     # found again later, from any program
vm.processes()                        # everything spawn() started

with vm.spawn("./worker") as worker:  # killed at the end of the block
    ...
```

Signals reach the process and everything it started. If the VM restarts,
`poll()` and `wait()` on a process it was running raise `ProcessLostError`.

## Files

```python
vm.write_file("app/config.json", json.dumps(config))   # str or bytes; makes directories
vm.write_file("bin/tool", script, mode=0o755)
data = vm.read_file("results/out.csv")                  # bytes; FileNotFoundError if missing
vm.upload("./src", "app/src", exclude=[".git", "node_modules", "*.pyc"])
vm.download("app/build", "./build")                     # a file or a directory
```

Paths on the VM are relative to the home directory unless absolute.
Transfers go through `/exec` in parts of about 64 KB, sent in parallel, and
compressed when that helps: expect a few seconds per megabyte.

## Creating VMs

```python
vm = exe.new(
    name="build",                       # default: a generated name
    image="ghcr.io/me/ci:latest",
    cpu=4, memory="8G", disk="50G",
    env={"MODE": "ci"},
    tags=["ci"],
    setup_script="#!/bin/sh\napt-get install -y jq",
    wait_until="setup",                 # "ssh" (default), "setup", or "created"
    email=False,                        # the default; True emails you about it
)
```

`wait_until="ssh"` returns once the VM runs commands; `"setup"` also waits for
its setup script to finish, raising `VMSetupError` (with the script's
`exit_code` and `output`) if it fails; `"created"` returns at once. Waiting
needs a token that may run `ssh`. If the VM is not ready within `timeout`
seconds (60, or 600 for setup), `VMReadyError` is raised.

If waiting fails, or is interrupted by an exception, a `KeyboardInterrupt`,
or cancellation, `new()` deletes the VM before raising. Pass
`keep_on_failure=True` to keep it for debugging, as `err.vm`.

A VM from `new()` or `vm.copy()` is a context manager that deletes it at the
end of the block (`async with exe.new() as vm:` in async code). Otherwise,
call `vm.delete()`.

## Working with VMs

```python
for vm in exe.vms():                  # or exe.vms("web-*")
    print(vm.name, vm.status, vm.tags)

vm = exe.vm("build")                  # NotFoundError if there is none

copy = vm.copy("build-2", cpu=8)      # a new VM with a copy of the disk (syncs it first)
vm.restart()                          # waits for SSH again, like new()
vm.pause(); vm.resume()
vm.rename("builder")                  # vm.name, ssh_dest, and https_url follow
vm.tag("ci", "linux"); vm.untag("linux")
vm.set_comment("nightly builds")
vm.resize(memory="16G", restart=True) # memory and CPU changes need a restart
vm.url(3000, "/api")                  # https://build.exe.xyz:3000/api
vm.stats(range="7d")
vm.delete()                           # NotFoundError if it does not exist

exe.delete("a", "b", vm)              # several at once; reports, rather than raises, failures
```

A VM's attributes (`status`, `ssh_dest`, `https_url`, `tags`, `comment`,
`region`, `created_at`) are a snapshot: the SDK's own operations keep them
current, and `refresh()` fetches the rest.

`vm.delete()` raises `NotFoundError` for a VM that is gone, while
`exe.delete()` reports each VM's fate in its result, since some may be
deleted and others not.

VM-scoped command groups are namespaces on the VM:

```python
vm.share.port(3000)
vm.share.set_public()
vm.share.add("friend@example.com", message="have a look")
vm.share.add_link()
vm.domain.add("app.example.com")
vm.shelley.install()
```

## Your account

Every other exe.dev command is a method, grouped as on the command line:
`ssh exe.dev team members` is `exe.team.members()`, and flags are keyword
arguments (`--copy-tags=false` is `copy_tags=False`).

```python
exe.whoami()
exe.ssh_key.list()
exe.ssh_key.generate_api_key(label="ci", vm="build", exp="30d")
exe.integrations.add("http-proxy", name="api", target="https://api.example.com", headers=["X-Key:secret"])
exe.integrations.attach("api", "tag:ci")
exe.team()                            # a group that is also a command is callable
exe.team.settings.vm_placement.pool("shared")
exe.billing.credits()
```

Each method's docstring is the command's help, with its CLI usage.

## Results

Methods whose commands report JSON in a known shape return it as a dict,
typed with a `TypedDict` from `exedev.types` (for example `whoami()` returns
`exedev.types.WhoamiResponse`). Commands that report something else return
their JSON, or their text.

## Errors

All exceptions derive from `exedev.ExeError`:

- `APIError`: exe.dev refused or failed a command; it has `status`,
  `operation`, and the response `body`. Subclasses: `AuthenticationError`
  (401), `PermissionDeniedError` (403), `NotFoundError` (404, also for
  `run()` on a VM that does not exist), `RateLimitError` (429, after the
  client's `max_retries`), and `VMNotRunningError` (`run()` on a paused or
  stopped VM).
- `NetworkError`: the request did not complete; whether it took effect is
  unknown.
- `ProtocolError`: a response was not what the SDK expected.
- `CommandError`: `run(check=True)` and the command failed.
- `CommandInterruptedError`: the command's exit status is unknown.
  Subclasses: `OutputLimitError`, and `CommandTimeoutError` (also a
  `TimeoutError`).
- `VMReadyError`: a VM did not become ready. Subclass: `VMSetupError`.
- `ProcessLostError`: a spawned process ended without an exit status.

File methods raise the built-in `FileNotFoundError` and `IsADirectoryError`,
and `vm.process()` raises `ProcessLookupError`.

Error messages include the server's reason, sanitized and shortened, but never
your token or the command line.

## Configuration

```python
exe = exedev.Client(
    token="exe1.…",                     # default: EXE_TOKEN
    base_url="https://exe.dev",
    http_client=httpx.Client(proxy="http://proxy:3128"),
    max_retries=5,                      # retries of rate-limited requests, with backoff
)
```

Pass your own `httpx.Client` (or `httpx.AsyncClient`) for proxies, custom
TLS, or tests; the SDK leaves it open. Close a client the SDK made with
`exe.close()` (`await exe.aclose()`), or use the client as a context manager.

## Development

This SDK is generated from the command tree of exe.dev's server, so it
follows the commands exe.dev offers. `src/exedev` is a copy of `sdk/python`
in the exe.dev repository; do not edit it here. To update it:

```sh
scripts/sync.sh ../exe
```

Run the tests with `uv run pytest`. They include integration tests that
create VMs on exe.dev, with a token from `EXE_TOKEN` or `EXE_TOKEN_FILE`
(default `~/.config/exedev/sdk-test-token`); set `EXE_SKIP_INTEGRATION=1` to
skip them, or `EXE_SKIP_SSH_INTEGRATION=1` to skip those that need `ssh`.
`uv run mypy` and `uv run pyright` check types.

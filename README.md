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
retry loop. Create a token with `ssh exe.dev ssh-key generate-api-key`; its
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
```

As with `subprocess.run`, a nonzero exit status is a result, not an
exception, unless you pass `check=True`. The `CommandError` it raises carries
the `result`, and its message ends with the last line of standard error.

Stream output as it arrives with `on_stdout` and `on_stderr`, which receive
decoded text (and may be coroutine functions with `AsyncVM`):

```python
vm.run("apt-get install -y ffmpeg", on_stdout=print, on_stderr=print)
```

Output is captured up to `max_output` bytes (8 MiB by default); beyond that,
`OutputLimitError`. For large output, pass `capture=False` and stream it.

If the connection breaks before the exit status arrives, `run()` raises
`CommandInterruptedError` with whatever output it captured: the command may
or may not have finished, so do not blindly retry one that is unsafe to run
twice. Closing the connection, whether by `timeout`, a callback raising, or
cancelling the asyncio task, stops the command.

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
)
```

`wait_until="ssh"` returns once the VM runs commands; `"setup"` also waits for
its setup script to finish, raising `VMSetupError` (with the script's
`exit_code` and `output`) if it fails; `"created"` returns at once. Waiting
needs a token that may run `ssh`. If the VM is not ready within `timeout`
seconds (60, or 600 for setup), `VMReadyError` is raised; the VM still exists,
as `err.vm`.

A VM from `new()` or `vm.copy()` is a context manager that deletes it at the
end of the block (`async with exe.new() as vm:` in async code). Otherwise,
call `vm.delete()`.

## Working with VMs

```python
for vm in exe.vms():                  # or exe.vms("web-*")
    print(vm.name, vm.status, vm.tags)

vm = exe.vm("build")                  # no request; vm.refresh() fetches its attributes

copy = vm.copy("build-2", cpu=8)      # a new VM with a copy of the disk
vm.restart()                          # waits for SSH again, like new()
vm.pause(); vm.resume()
vm.rename("builder")                  # vm.name, ssh_dest, and https_url follow
vm.tag("ci", "linux"); vm.untag("linux")
vm.set_comment("nightly builds")
vm.resize(memory="16G")
vm.stats(range="7d")
vm.delete()                           # NotFoundError if it does not exist

exe.delete("a", "b", vm)              # several at once; reports, rather than raises, failures
```

A VM's attributes (`status`, `ssh_dest`, `https_url`, `tags`, `comment`,
`region`, `created_at`) are a snapshot: the SDK's own operations keep them
current, and `refresh()` fetches the rest.

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
  (401), `PermissionDeniedError` (403), `NotFoundError` (404), and
  `RateLimitError` (429).
- `NetworkError`: the request did not complete; whether it took effect is
  unknown.
- `ProtocolError`: a response was not what the SDK expected.
- `CommandError`: `run(check=True)` and the command failed.
- `CommandInterruptedError`: the command's exit status is unknown.
  Subclasses: `OutputLimitError`, and `CommandTimeoutError` (also a
  `TimeoutError`).
- `VMReadyError`: a VM did not become ready. Subclass: `VMSetupError`.

Error messages include the server's reason, sanitized and shortened, but never
your token or the command line.

## Configuration

```python
exe = exedev.Client(
    token="exe1.…",                     # default: EXE_TOKEN
    base_url="https://exe.dev",
    http_client=httpx.Client(proxy="http://proxy:3128"),
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

"""The client: how methods become commands, and how responses become results or errors."""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path

import httpx
import pytest
from conftest import error

import exedev


def test_posts_the_command_line_with_the_token(api):
    exe, server = api.client({"whoami": {"email": "me@example.com", "ssh_keys": []}})
    assert api(exe.whoami()) == {"email": "me@example.com", "ssh_keys": []}
    (request,) = server.requests
    assert request.method == "POST"
    assert str(request.url) == "https://exe.dev/exec"
    assert request.headers["Authorization"] == "Bearer tok"
    assert request.headers["Content-Type"] == "text/plain"
    assert request.headers["Accept"] == "application/json, application/x-ndjson"
    assert request.content == b"whoami"


def test_base_url(api):
    exe, server = api.client(lambda _: {}, base_url="http://localhost:8080/")
    api(exe.whoami())
    assert str(server.requests[0].url) == "http://localhost:8080/exec"
    assert repr(exe) == f"<{type(exe).__name__} 'http://localhost:8080'>"


def test_token_from_the_environment(monkeypatch):
    monkeypatch.setenv("EXE_TOKEN", "from-env")
    seen = []
    transport = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={}))
    exe = exedev.Client(http_client=httpx.Client(transport=transport))
    exe.whoami()
    assert seen[0].headers["Authorization"] == "Bearer from-env"
    assert "from-env" not in repr(exe)


def test_token_is_required(monkeypatch):
    monkeypatch.delenv("EXE_TOKEN", raising=False)
    with pytest.raises(exedev.ExeError, match="EXE_TOKEN"):
        exedev.Client()
    with pytest.raises(exedev.ExeError, match="EXE_TOKEN"):
        exedev.AsyncClient()


def test_flags(api):
    exe, server = api.client(lambda _: "ok")
    api(exe.team.disable(yes=True))
    api(exe.billing.receipts(from_="2026-01", to=None))
    api(exe.billing.capacity(cpu=8))
    api(exe.ssh_key.add("ssh-ed25519 AAAA me@host", tag="ci"))
    api(exe.ssh_key.generate_api_key(label="ci", tag="ci", exp="30d"))
    api(exe.integrations.add("http-proxy", name="api", target="https://example.com", headers=["A:1", "B: 2"]))
    api(exe.integrations.setup.github(delete=False, list=True))
    assert server.commands == [
        "team disable --yes",
        "billing receipts --from=2026-01",
        "billing capacity --cpu=8",
        "ssh-key add --tag=ci 'ssh-ed25519 AAAA me@host'",
        "ssh-key generate-api-key --exp=30d --label=ci --tag=ci",
        "integrations add --header=A:1 --header='B: 2' --name=api --target=https://example.com http-proxy",
        "integrations setup github --list",
    ]


def test_flag_values_are_checked(api):
    exe, server = api.client(lambda _: "ok")
    with pytest.raises(TypeError, match="cpu"):
        api(exe.billing.capacity(cpu=2.5))
    with pytest.raises(TypeError, match="cpu"):
        api(exe.billing.capacity(cpu=True))
    with pytest.raises(TypeError):
        api(exe.billing.capacity(cpus=2))
    assert server.commands == []


def test_positionals(api):
    exe, server = api.client(lambda command: {} if command.startswith(("share", "comment")) else "ok")
    api(exe.doc())
    api(exe.doc("https api", "proxy"))
    api(exe.team.add("a@example.com"))
    api(exe.team.add("a@example.com", "admin"))
    vm = api(exe.vm("worker"))
    api(vm.grant_support_root(False))
    api(vm.share.port(8080))
    api(vm.set_comment("-x"))
    assert server.commands == [
        "doc",
        "doc 'https api' proxy",
        "team add a@example.com",
        "team add a@example.com admin",
        "ls worker",
        "grant-support-root worker off",
        "share port worker 8080",
        "comment -- worker -x",
    ]


def test_required_variadic(api):
    _, vm, server = api.vm(lambda _: "ok")
    with pytest.raises(TypeError, match="tag name"):
        api(vm.tag())
    assert server.commands == []


def test_text_results(api):
    exe, _ = api.client({"invite show": "https://exe.dev/i/abc\n"})
    assert api(exe.invite.show()) == "https://exe.dev/i/abc\n"


def test_ndjson_results_are_lists(api):
    exe, _ = api.client({"team members": '{"email": "a"}\n{"email": "b"}\n'})
    assert api(exe.team.members()) == [{"email": "a"}, {"email": "b"}]


def test_json_results_must_be_json(api):
    exe, _ = api.client({"whoami": "you are me"})
    with pytest.raises(exedev.ProtocolError, match="whoami"):
        api(exe.whoami())


@pytest.mark.parametrize(
    ("status", "cls"),
    [
        (400, exedev.APIError),
        (401, exedev.AuthenticationError),
        (403, exedev.PermissionDeniedError),
        (404, exedev.NotFoundError),
        (422, exedev.APIError),
        (429, exedev.RateLimitError),
        (500, exedev.APIError),
    ],
)
def test_api_errors(api, status, cls):
    exe, _ = api.client(lambda _: error(status, "nope"), max_retries=0)
    with pytest.raises(cls) as info:
        api(exe.whoami())
    err = info.value
    assert type(err) is cls
    assert isinstance(err, exedev.APIError)
    assert err.status == status
    assert err.operation == "whoami"
    assert err.body == '{"error":"nope"}'
    assert str(err) == f"exe.dev API error ({status}) during whoami: nope"


def test_rate_limits_are_retried(api, clock):
    exe, server = api.client({"whoami": [error(429, "slow down")] * 3 + [{"email": "me@example.com"}]})
    assert api(exe.whoami()) == {"email": "me@example.com"}
    assert server.commands == ["whoami"] * 4
    # Exponential backoff, with jitter.
    for delay, backoff in zip(clock.sleeps, [0.5, 1, 2], strict=True):
        assert backoff <= delay <= backoff * 1.5


def test_rate_limit_retries_run_out(api, clock):
    exe, server = api.client(lambda _: httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "7"}))
    with pytest.raises(exedev.RateLimitError):
        api(exe.whoami())
    assert len(server.commands) == 6
    assert min(clock.sleeps) >= 7
    exe, server = api.client(lambda _: error(429, "slow down"), max_retries=0)
    with pytest.raises(exedev.RateLimitError):
        api(exe.whoami())
    assert len(server.commands) == 1
    with pytest.raises(TypeError):
        api.client({}, max_retries=-1)


def test_api_error_messages_are_safe(api):
    reason = "\x1b[31mred\x1b[0m\x1b]8;;http://evil\x07link\x1b]8;;\x07 \u202eevil\n\tline " + "x" * 300
    exe, _ = api.client(lambda _: error(422, reason))
    with pytest.raises(exedev.APIError) as info:
        api(exe.team.add("a@example.com"))
    message = str(info.value)
    assert message.startswith("exe.dev API error (422) during team add: redlink evil line xxx")
    assert len(message) < 260
    assert message.endswith("…")
    assert "tok" not in message
    assert "a@example.com" not in message
    assert not any(c < " " or "\u202a" <= c <= "\u202e" for c in message)


def test_api_error_without_a_reason(api):
    exe, _ = api.client(lambda _: httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(exedev.APIError) as info:
        api(exe.whoami())
    assert str(info.value) == "exe.dev API error (502) during whoami"
    assert info.value.body == "<html>bad gateway</html>"


def test_error_documents_are_errors_despite_200(api):
    exe, _ = api.client(lambda _: '{"ok": 1}\n{"error": "half failed"}\n')
    with pytest.raises(exedev.APIError, match="half failed") as info:
        api(exe.team.members())
    assert info.value.status == 200


def test_network_errors(api):
    exe, _ = api.client(lambda _: httpx.ConnectError("connection refused"))
    with pytest.raises(exedev.NetworkError) as info:
        api(exe.whoami())
    assert info.value.operation == "whoami"
    assert isinstance(info.value.__cause__, httpx.ConnectError)
    assert "whoami" in str(info.value)


def test_vm_names_are_checked(api):
    exe, server = api.client(lambda _: {})
    for bad in ["", "-x", "a b", "a;b", "a/b"]:
        with pytest.raises(ValueError):
            api(exe.vm(bad))
    assert server.commands == []


def test_vm(api):
    exe, server = api.client({"ls worker": VMS_WORKER, "ls gone": {"vms": []}})
    vm = api(exe.vm("worker"))
    assert server.commands == ["ls worker"]
    assert (vm.name, vm.status, vm.ssh_dest, vm.https_url) == (
        "worker",
        "running",
        "worker.exe.xyz",
        "https://worker.exe.xyz",
    )
    assert (vm.tags, vm.comment, vm.region) == ((), "", "lax")
    assert repr(vm) == f"<{type(vm).__name__} 'worker' status='running'>"
    with pytest.raises(exedev.NotFoundError):
        api(exe.vm("gone"))


VMS_WORKER = {
    "vms": [
        # ls with a name lists VMs whose names start with it, too.
        {"vm_name": "worker-2", "ssh_dest": "worker-2.exe.xyz", "https_url": "https://w2", "status": "paused"},
        {
            "vm_name": "worker",
            "ssh_dest": "worker.exe.xyz",
            "https_url": "https://worker.exe.xyz",
            "status": "running",
            "region": "lax",
        },
    ]
}


def test_url(api):
    _, vm, _ = api.vm({})
    assert vm.url() == "https://worker.exe.xyz/"
    assert vm.url(3000) == "https://worker.exe.xyz:3000/"
    assert vm.url(8080, "/api?x=1") == "https://worker.exe.xyz:8080/api?x=1"
    for bad in [0, 70000, True, "80"]:
        with pytest.raises(ValueError):
            vm.url(bad)
    with pytest.raises(ValueError):
        vm.url(3000, "api")


VMS = {
    "vms": [
        {
            "vm_name": "web-1",
            "ssh_dest": "web-1.exe.xyz",
            "https_url": "https://web-1.exe.xyz",
            "status": "running",
            "region": "lax",
            "tags": ["web"],
            "comment": "prod",
            "created_at": "2026-01-02T03:04:05Z",
        },
        {"vm_name": "web-2", "ssh_dest": "web-2.exe.xyz", "https_url": "https://web-2.exe.xyz", "status": "paused"},
    ]
}


def test_vms(api):
    exe, _server = api.client({"ls": VMS, "ls 'web-*'": {"vms": VMS["vms"][1:]}})
    vms = api(exe.vms())
    assert [vm.name for vm in vms] == ["web-1", "web-2"]
    first = vms[0]
    assert first.status == "running"
    assert first.ssh_dest == "web-1.exe.xyz"
    assert first.https_url == "https://web-1.exe.xyz"
    assert first.tags == ("web",)
    assert first.comment == "prod"
    assert first.region == "lax"
    assert first.created_at is not None and first.created_at.year == 2026
    assert repr(first) == f"<{type(first).__name__} 'web-1' status='running'>"
    assert [vm.name for vm in api(exe.vms("web-*"))] == ["web-2"]


def test_vms_rejects_malformed_listings(api):
    exe, _ = api.client({"ls": {"vms": [{"vm_name": "-bad"}]}})
    with pytest.raises(exedev.ProtocolError):
        api(exe.vms())


def test_delete_many(api):
    result = {"deleted": ["a"], "failed": ["b"], "not_found": ["b"], "errors": {"b": "not found"}}
    exe, server = api.client({"rm a b": result})
    b = api(exe.vm("b"))
    assert api(exe.delete("a", b, "a")) == result
    assert server.commands == ["ls b", "rm a b"]


def test_delete_nothing(api):
    exe, server = api.client({})
    assert api(exe.delete()) == {"deleted": [], "failed": [], "not_found": [], "errors": {}}
    assert server.commands == []


def test_closes_only_its_own_http_client():
    given = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    with exedev.Client("tok", http_client=given) as exe:
        exe.whoami()
    assert not given.is_closed
    with exedev.Client("tok") as exe:
        own = exe._t._http
    assert own.is_closed


def test_async_client_closes_its_http_client():
    async def main() -> httpx.AsyncClient:
        async with exedev.AsyncClient("tok") as exe:
            return exe._t._http

    assert asyncio.run(main()).is_closed


def test_version():
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert exedev.__version__ == pyproject["project"]["version"]

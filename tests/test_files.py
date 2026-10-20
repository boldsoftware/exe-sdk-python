"""Moving files: write_file, read_file, upload, and download, against a local fake VM."""

from __future__ import annotations

import os
import random
import stat
from pathlib import Path, PurePosixPath

import pytest


def test_write_and_read(api, local):
    vm, machine, server = local
    api(vm.write_file("notes.txt", "héllo\n"))
    assert (machine.home / "notes.txt").read_text() == "héllo\n"
    assert api(vm.read_file("notes.txt")) == "héllo\n".encode()
    every_byte = bytes(range(256)) * 4
    api(vm.write_file(PurePosixPath("deep/er/data.bin"), every_byte, mode=0o755))
    path = machine.home / "deep/er/data.bin"
    assert path.read_bytes() == every_byte
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert api(vm.read_file("~/deep/er/data.bin")) == every_byte
    api(vm.write_file(str(machine.home / "abs.txt"), b""))
    assert api(vm.read_file(str(machine.home / "abs.txt"))) == b""
    assert [c.split()[2] for c in server.commands] == ["write", "read", "write", "read", "write", "read"]


def test_paths_are_not_options(api, local):
    vm, machine, _ = local
    api(vm.write_file("-n", "x"))
    assert (machine.home / "-n").read_text() == "x"
    assert api(vm.read_file("-n")) == b"x"


def test_large_files_go_in_parts(api, local):
    vm, machine, server = local
    data = random.Random(1).randbytes(300_000)
    api(vm.write_file("big.bin", data))
    assert (machine.home / "big.bin").read_bytes() == data
    assert api(vm.read_file("big.bin")) == data
    kinds = [c.split()[2] for c in server.commands]
    assert kinds == ["part"] * 7 + ["write", "read"]
    # Text compresses: it fits in one request.
    server.commands.clear()
    api(vm.write_file("big.txt", "line\n" * 200000))
    assert [c.split()[2] for c in server.commands] == ["write"]
    assert (machine.home / "big.txt").read_text() == "line\n" * 200000


def test_missing_files(api, local):
    vm, machine, _ = local
    with pytest.raises(FileNotFoundError) as info:
        api(vm.read_file("nope.txt"))
    assert info.value.filename == "nope.txt"
    (machine.home / "dir").mkdir()
    with pytest.raises(IsADirectoryError):
        api(vm.read_file("dir"))
    with pytest.raises(FileNotFoundError):
        api(vm.download("nope", machine.home.parent / "out"))


def test_bad_arguments(api, local):
    vm, _, server = local
    for bad in ["", None, b"x", "a\x00b"]:
        with pytest.raises(TypeError):
            api(vm.write_file(bad, "x"))
    with pytest.raises(TypeError):
        api(vm.write_file("x", 1))
    with pytest.raises(ValueError):
        api(vm.write_file("x", "x", mode="644"))
    assert server.commands == []


def tree(root: Path) -> dict[str, bytes | None]:
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))}


def test_upload_and_download_directories(api, local, tmp_path):
    vm, machine, _ = local
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref")
    (src / "node_modules" / "x").mkdir(parents=True)
    (src / "pkg" / "a.py").write_text("print('a')\n")
    (src / "pkg" / "a.pyc").write_bytes(b"\x00")
    (src / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (src / "run.sh").chmod(0o755)
    (src / "empty").mkdir()
    api(vm.upload(src, "proj", exclude=[".git", "node_modules", "*.pyc"]))
    assert tree(machine.home / "proj") == {
        "empty": None,
        "pkg": None,
        "pkg/a.py": b"print('a')\n",
        "run.sh": b"#!/bin/sh\necho hi\n",
    }
    assert os.access(machine.home / "proj" / "run.sh", os.X_OK)
    out = tmp_path / "out"
    api(vm.download("proj", out))
    assert tree(out) == tree(machine.home / "proj")
    assert os.access(out / "run.sh", os.X_OK)
    with pytest.raises(TypeError):
        api(vm.upload(src, "proj", exclude=".git"))


def test_upload_and_download_files(api, local, tmp_path):
    vm, machine, _ = local
    script = tmp_path / "tool.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o700)
    api(vm.upload(script, "bin/tool"))
    assert (machine.home / "bin/tool").read_text() == "#!/bin/sh\n"
    assert stat.S_IMODE((machine.home / "bin/tool").stat().st_mode) == 0o700
    api(vm.download("bin/tool", tmp_path / "copy"))
    assert (tmp_path / "copy").read_text() == "#!/bin/sh\n"

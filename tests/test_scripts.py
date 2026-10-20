"""The SDK's shell scripts, run here as a VM runs them."""

from __future__ import annotations

import os
import signal
import subprocess
import time

import pytest

from exedev import _wire


def gone(pgid: int, seconds: float = 5) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_run_script_passes_output_and_status_through(tmp_path):
    line = _wire.sh(_wire.RUN_SCRIPT, "echo out; echo err >&2; exit 7")
    done = subprocess.run(
        ["bash", "-c", line], check=False, capture_output=True, env={**os.environ, "SHELL": "/bin/bash"}
    )
    assert (done.returncode, done.stdout, done.stderr) == (7, b"out\n", b"err\n")


@pytest.mark.parametrize("login_shell_execs", [True, False])
def test_run_script_stops_everything_when_the_session_ends(tmp_path, login_shell_execs):
    # The VM kills the process it started for the session: the login shell,
    # or the script, when the login shell runs it with exec.
    pidfile = tmp_path / "pid"
    line = _wire.sh(_wire.RUN_SCRIPT, f"echo $$ > {pidfile}; sleep 300 & sleep 300")
    session = subprocess.Popen(
        ["bash", "-c", line if login_shell_execs else f"true; {line}"],
        env={**os.environ, "SHELL": "/bin/bash"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5
    while not pidfile.exists() or not pidfile.read_text().strip():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    pgid = int(pidfile.read_text())
    try:
        assert not gone(pgid, 0.2)
        session.kill()
        session.wait()
        # Promptly: a run() timeout should not run on for long.
        assert gone(pgid, 0.7)
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

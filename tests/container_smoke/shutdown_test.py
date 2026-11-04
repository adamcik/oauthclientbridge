"""Exercise the smoke script's shutdown verdict without starting containers.

The command substitutes provide container state and logs, but Bash executes the
real smoke script, including pipefail, log matching, and failure handling.
"""

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass(frozen=True)
class ShutdownCase:
    logs: str
    expected_status: int
    expected_error: str = ""
    container_status: int = 0
    log_status: int = 0


_COMMAND = r"""
import os
import signal
import socket
import sys
from pathlib import Path

command = Path(sys.argv[0]).name
args = sys.argv[1:]
if command == "python":
    if args[0] == "tests/container_smoke/smoke.py":
        sys.exit(0)
    os.execv(sys.executable, [sys.executable, *args])
elif command == "podman":
    if args[0] == "info":
        print("true")
    elif args[0] == "run" and "--detach" in args:
        mount = next(arg for arg in args if "dst=/run/asgi" in arg)
        directory = mount.split("src=", 1)[1].split(",", 1)[0]
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(Path(directory) / "server.sock"))
    elif args[:2] == ["container", "inspect"] and "--format" in args:
        print(os.environ["TEST_CONTAINER_STATUS"])
    elif args[0] == "logs":
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        for line in Path(os.environ["TEST_LOG_FILE"]).read_bytes().splitlines(keepends=True):
            os.write(1, line)
        sys.exit(int(os.environ["TEST_LOG_STATUS"]))
"""


@pytest.mark.skipif(sys.platform != "linux", reason="The smoke script requires Linux")
@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            ShutdownCase(logs="Application shutdown complete.\n", expected_status=0),
            id="completed",
        ),
        pytest.param(
            ShutdownCase(
                logs="Application shutdown complete.\n"
                + "trailing log output\n" * 65_536,
                expected_status=0,
            ),
            id="completed-with-buffered-output",
        ),
        pytest.param(
            ShutdownCase(
                logs="Shutting down\n",
                expected_status=1,
                expected_error="did not complete graceful application shutdown",
            ),
            id="completion-missing",
        ),
        pytest.param(
            ShutdownCase(
                logs="Application shutdown complete.\nERROR: failed cleanup\n",
                expected_status=1,
                expected_error="logged an unexpected runtime error",
            ),
            id="runtime-error-after-completion",
        ),
        pytest.param(
            ShutdownCase(
                logs="Application shutdown complete.\n",
                expected_status=1,
                expected_error="exited with status 137",
                container_status=137,
            ),
            id="container-killed",
        ),
        pytest.param(
            ShutdownCase(
                logs="Application shutdown complete.\n",
                expected_status=1,
                expected_error="did not complete graceful application shutdown",
                log_status=125,
            ),
            id="log-command-failed",
        ),
    ],
)
def test_shutdown_verdict(tmp_path: Path, case: ShutdownCase) -> None:
    commands = tmp_path / "bin"
    commands.mkdir()
    for name in ("podman", "python", "newuidmap"):
        command = commands / name
        command.write_text(f"#!{sys.executable}\n" + _COMMAND)
        command.chmod(0o755)

    logs = tmp_path / "container.log"
    logs.write_text(case.logs)
    result = subprocess.run(
        ["bash", "deploy/smoke-image.sh"],
        cwd=Path(__file__).resolve().parents[2],
        env={
            **os.environ,
            "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
            "SMOKE_RUNTIME": "asgi",
            "TEST_CONTAINER_STATUS": str(case.container_status),
            "TEST_LOG_FILE": str(logs),
            "TEST_LOG_STATUS": str(case.log_status),
        },
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == case.expected_status, result.stderr[-2000:]
    if case.expected_error:
        assert case.expected_error in result.stderr

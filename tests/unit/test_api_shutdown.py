from __future__ import annotations

import importlib
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from app.config import ApiConfig
from app.launcher import _API_STOP_GRACE_SECONDS

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_api_main_bounds_uvicorn_graceful_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.app as api_app

    monkeypatch.setattr(api_app, "create_app", lambda settings: object())
    monkeypatch.setenv("DATAOPS_API_GRACEFUL_SHUTDOWN_SECONDS", "7")
    sys.modules.pop("app.main", None)
    try:
        main_module = importlib.import_module("app.main")
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(
            main_module.uvicorn,
            "run",
            lambda *args, **kwargs: calls.append(kwargs),
        )

        main_module.main()
    finally:
        sys.modules.pop("app.main", None)

    assert len(calls) == 1
    assert calls[0]["timeout_graceful_shutdown"] == 7


def test_api_graceful_shutdown_finishes_before_launcher_force_kill() -> None:
    metadata = ApiConfig.model_fields["graceful_shutdown_seconds"].metadata
    upper_bound = next(item.le for item in metadata if hasattr(item, "le"))

    assert ApiConfig().graceful_shutdown_seconds == 10
    assert upper_bound < _API_STOP_GRACE_SECONDS


_LEAKY_API_CHILD = """
import socket

import app.api.app as api_app


async def tiny_app(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-length", b"2")]})
    await send({"type": "http.response.body", "body": b"ok"})


def reset_shutdown(self, how):
    # Same stack as the field api.log: Proactor _call_connection_lost ->
    # sock.shutdown -> WinError 10054, which skips Server._detach().
    raise ConnectionResetError(10054, "connection reset by peer")


api_app.create_app = lambda settings: tiny_app
socket.socket.shutdown = reset_shutdown

import app.main

app.main.main()
"""


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Proactor loop and CTRL_BREAK")
def test_api_stops_within_bound_when_proactor_leaks_a_reset_connection() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = dict(os.environ)
    env.update(
        {
            "DATAOPS_API_HOST": "127.0.0.1",
            "DATAOPS_API_PORT": str(port),
            "DATAOPS_API_GRACEFUL_SHUTDOWN_SECONDS": "2",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-c", _LEAKY_API_CHILD],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=cast(Any, subprocess).CREATE_NEW_PROCESS_GROUP,
    )
    try:
        _wait_for_port(port, process)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n")
            assert client.recv(1024).startswith(b"HTTP/1.1 200")
        time.sleep(0.5)

        started = time.monotonic()
        process.send_signal(cast(Any, signal).CTRL_BREAK_EVENT)
        process.wait(timeout=15)

        assert time.monotonic() - started < 10
        output = process.stdout.read() if process.stdout is not None else ""
        assert "timeout graceful shutdown exceeded" in output
    finally:
        if process.poll() is None:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )
            process.wait(timeout=5)


def _wait_for_port(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout is not None else ""
            raise AssertionError(f"API child exited early:\n{output}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("API child did not start listening")

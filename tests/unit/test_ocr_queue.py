from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from house_account.config import Settings
from house_account.ocr_queue import SubprocessOcrExecutor


def install_sleeping_child(
    monkeypatch: pytest.MonkeyPatch, started: threading.Event
) -> list[subprocess.Popen[bytes]]:
    original_popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def sleeping_child(*_: object, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = original_popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **kwargs,
        )
        children.append(process)
        started.set()
        return process

    monkeypatch.setattr("house_account.ocr_queue.subprocess.Popen", sleeping_child)
    return children


def test_timeout_terminates_child_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    started = threading.Event()
    children = install_sleeping_child(monkeypatch, started)
    settings = Settings(data_dir=data_dir, model_dir=tmp_path / "models")
    executor = SubprocessOcrExecutor(settings)

    with pytest.raises(TimeoutError):
        executor(tmp_path / "input.jpg", 0.1, threading.Event())

    assert started.is_set()
    assert children[0].poll() is not None


def test_shutdown_signal_terminates_child_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    started = threading.Event()
    children = install_sleeping_child(monkeypatch, started)
    stopping = threading.Event()
    settings = Settings(data_dir=data_dir, model_dir=tmp_path / "models")
    executor = SubprocessOcrExecutor(settings)
    failures: list[Exception] = []

    def run() -> None:
        try:
            executor(tmp_path / "input.jpg", 30, stopping)
        except Exception as error:
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(timeout=3)
    stopping.set()
    thread.join(timeout=4)

    assert not thread.is_alive()
    assert isinstance(failures[0], InterruptedError)
    assert children[0].poll() is not None

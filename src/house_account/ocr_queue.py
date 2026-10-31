from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from house_account.config import Settings
from house_account.domain.ocr import OcrResult
from house_account.ocr.extraction import extract_candidates
from house_account.ocr.serialization import result_from_dict, result_to_dict
from house_account.service import ReceiptService

LOGGER = logging.getLogger(__name__)


class OcrExecutor(Protocol):
    def __call__(
        self, image_path: Path, timeout_seconds: float, stopping: threading.Event
    ) -> OcrResult: ...


class SubprocessOcrExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def __call__(
        self, image_path: Path, timeout_seconds: float, stopping: threading.Event
    ) -> OcrResult:
        with tempfile.TemporaryDirectory(dir=self.settings.data_dir) as directory:
            result_path = Path(directory) / "result.json"
            command = [
                sys.executable,
                "-m",
                "house_account.ocr.worker",
                str(image_path.resolve()),
                "--model-dir",
                str(self.settings.model_dir),
                "--output",
                str(result_path),
            ]
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=self.settings.project_root,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=stderr,
                        start_new_session=True,
                    )
                except OSError as error:
                    raise RuntimeError("OCR子プロセスを開始できませんでした。") from error
                deadline = time.monotonic() + timeout_seconds
                while process.poll() is None:
                    if stopping.is_set():
                        self._stop_process(process)
                        raise InterruptedError
                    if time.monotonic() >= deadline:
                        self._stop_process(process)
                        raise TimeoutError
                    time.sleep(0.1)
                if process.returncode != 0:
                    stderr.seek(0, 2)
                    end = stderr.tell()
                    stderr.seek(max(0, end - 2_000))
                    detail = stderr.read().decode("utf-8", errors="replace").strip()
                    raise RuntimeError(detail[-1_000:] or "OCR子プロセスが失敗しました。")
            if not result_path.is_file():
                raise RuntimeError("OCR結果ファイルが作成されませんでした。")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            return result_from_dict(result)

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


class OcrQueue:
    def __init__(
        self,
        service: ReceiptService,
        settings: Settings,
        executor: OcrExecutor | None = None,
    ) -> None:
        self.service = service
        self.settings = settings
        self.executor = executor or SubprocessOcrExecutor(settings)
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._work, name="receipt-ocr", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _work(self) -> None:
        while not self._stopping.is_set():
            try:
                run = self.service.claim_next_run()
                if run is None:
                    self._stopping.wait(0.2)
                    continue
                self._process_run(run)
            except Exception:
                LOGGER.exception("OCRキューの処理に失敗しました。")
                self._stopping.wait(0.5)

    def _process_run(self, run: dict[str, Any]) -> None:
        run_id = str(run["id"])
        try:
            result = self.executor(
                run["image_path"],
                self.settings.ocr_timeout_seconds,
                self._stopping,
            )
        except TimeoutError:
            self.service.finish_run(
                run_id,
                "timeout",
                error_code="ocr_timeout",
                error_message=f"{self.settings.ocr_timeout_seconds:g}秒の制限を超えました。",
            )
            return
        except InterruptedError:
            self.service.mark_interrupted(run_id)
            return
        except Exception as error:
            if self._stopping.is_set():
                self.service.mark_interrupted(run_id)
            else:
                LOGGER.exception("OCR実行に失敗しました。")
                self.service.finish_run(
                    run_id,
                    "ocr_failed",
                    error_code="ocr_failed",
                    error_message=str(error)[:1_000] or "OCRを実行できませんでした。",
                )
            return
        if self._stopping.is_set():
            self.service.mark_interrupted(run_id)
            return
        raw = result_to_dict(result)
        self.service.record_raw_result(run_id, raw)
        try:
            candidates = [asdict(candidate) for candidate in extract_candidates(result)]
        except Exception as error:
            LOGGER.exception("OCR原文から候補を抽出できませんでした。")
            self.service.finish_run(
                run_id,
                "extraction_failed",
                error_code="extraction_failed",
                error_message=str(error)[:1_000] or "OCR候補を抽出できませんでした。",
            )
            return
        self.service.finish_run(
            run_id,
            "succeeded" if candidates else "no_candidates",
            candidates=candidates,
        )

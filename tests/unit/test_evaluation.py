import json
import subprocess
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from typing import Any

import pytest

from house_account.config import Settings
from house_account.ocr.engine import PaddleOcrEngine
from house_account.ocr.evaluation import (
    recognize_for_evaluation,
    score_review,
    score_review_to_file,
)
from house_account.ocr.models import MODEL_NAMES, REQUIRED_MODEL_FILES, ModelsNotPreparedError
from house_account.ocr.serialization import write_json


def create_review(tmp_path: Path, *, duration: float = 20) -> Path:
    receipts: list[dict[str, Any]] = []
    for index in range(5):
        write_json(
            tmp_path / f"result-{index}.json",
            {
                "status": "succeeded",
                "duration_seconds": duration,
                "candidates": [{"id": str(item)} for item in range(6)],
            },
        )
        receipts.append(
            {
                "id": f"receipt-{index}",
                "store_id": f"store-{index % 3}",
                "result": f"result-{index}.json",
                "expected_items": [
                    {"name": "架空の商品", "candidate_id": str(item), "needs_correction": item == 0}
                    for item in range(6)
                ],
            }
        )
    review_path = tmp_path / "review.json"
    write_json(review_path, {"receipts": receipts})
    return review_path


def test_score_aggregates_all_lines_and_counts_typos_separately(tmp_path: Path) -> None:
    result = score_review(create_review(tmp_path))
    assert result["accepted"]
    assert result["precision"] == result["recall"] == 1
    assert result["expected_line_count"] == 30
    assert result["correction_count"] == 5


def test_low_precision_and_recall_are_not_accepted(tmp_path: Path) -> None:
    review_path = create_review(tmp_path)
    review = json.loads(review_path.read_text())
    for receipt in review["receipts"]:
        for expected in receipt["expected_items"][:2]:
            expected["candidate_id"] = None
    write_json(review_path, review)
    result = score_review(review_path)
    assert not result["accepted"]
    assert result["precision"] == result["recall"] == 20 / 30


@pytest.mark.parametrize("duration", [300.001, 301])
def test_slow_inference_is_not_accepted(tmp_path: Path, duration: float) -> None:
    assert not score_review(create_review(tmp_path, duration=duration))["accepted"]


def test_incomplete_sample_is_not_accepted(tmp_path: Path) -> None:
    review_path = create_review(tmp_path)
    review = json.loads(review_path.read_text())
    review["receipts"] = review["receipts"][:1]
    write_json(review_path, review)
    result = score_review(review_path)
    assert result["precision"] == 1
    assert not result["sufficient_sample"]
    assert not result["accepted"]


@pytest.mark.parametrize("candidate_id", ["0", "not-an-id"])
def test_invalid_or_duplicate_mapping_is_rejected(tmp_path: Path, candidate_id: str) -> None:
    review_path = create_review(tmp_path)
    review = json.loads(review_path.read_text())
    review["receipts"][0]["expected_items"][1]["candidate_id"] = candidate_id
    write_json(review_path, review)
    with pytest.raises(ValueError, match="候補ID"):
        score_review(review_path)


def test_missing_models_fail_before_importing_or_downloading(tmp_path: Path) -> None:
    with pytest.raises(ModelsNotPreparedError, match="prepare_ocr_models.py"):
        PaddleOcrEngine(tmp_path).recognize(tmp_path / "unused.png")
    assert list(tmp_path.iterdir()) == []


def test_defaults_are_independent_of_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HOUSE_ACCOUNT_DATA_DIR", raising=False)
    monkeypatch.delenv("HOUSE_ACCOUNT_MODEL_DIR", raising=False)
    before = Settings.from_env()
    monkeypatch.chdir(tmp_path)
    assert Settings.from_env() == before
    monkeypatch.setenv("HOUSE_ACCOUNT_DATA_DIR", str(tmp_path / "isolated"))
    assert Settings.from_env().data_dir == tmp_path / "isolated"


def test_extraction_failure_preserves_raw_text(
    tmp_path: Path, fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in MODEL_NAMES:
        model_path = tmp_path / name
        model_path.mkdir()
        for filename in REQUIRED_MODEL_FILES:
            (model_path / filename).touch()
    raw = json.loads((fixture_dir / "ocr_result.json").read_text())["result"]
    output = tmp_path / "result.json"

    def recognize(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        write_json(Path(command[-1]), raw)
        return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")

    def fail_extraction(value: object) -> None:
        saved = json.loads(output.read_text())
        assert saved["status"] == "recognized"
        assert saved["raw"]["raw_text"] == raw["raw_text"]
        raise ValueError("抽出に失敗")

    monkeypatch.setattr(
        "house_account.ocr.evaluation.subprocess",
        SimpleNamespace(run=recognize, TimeoutExpired=subprocess.TimeoutExpired),
    )
    monkeypatch.setattr("house_account.ocr.evaluation.extract_candidates", fail_extraction)
    with pytest.raises(ValueError, match="抽出に失敗"):
        recognize_for_evaluation(tmp_path / "unused.png", output, Settings(tmp_path, tmp_path))
    saved = json.loads(output.read_text())
    assert saved["error_code"] == "extraction_failed"
    assert saved["raw"] == raw


def test_output_is_reserved_before_inference_and_rejects_concurrent_run(
    tmp_path: Path, fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in MODEL_NAMES:
        model_path = tmp_path / name
        model_path.mkdir()
        for filename in REQUIRED_MODEL_FILES:
            (model_path / filename).touch()
    raw = json.loads((fixture_dir / "ocr_result.json").read_text())["result"]
    output = tmp_path / "result.json"
    nested_errors: list[ValueError] = []

    def recognize(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        with pytest.raises(ValueError, match="出力先が既に存在") as error:
            recognize_for_evaluation(tmp_path / "unused.png", output, Settings(tmp_path, tmp_path))
        nested_errors.append(error.value)
        assert json.loads(output.read_text(encoding="utf-8"))["status"] == "running"
        write_json(Path(command[-1]), raw)
        return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "house_account.ocr.evaluation.subprocess",
        SimpleNamespace(run=recognize, TimeoutExpired=subprocess.TimeoutExpired),
    )
    recognize_for_evaluation(tmp_path / "unused.png", output, Settings(tmp_path, tmp_path))
    assert len(nested_errors) == 1
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "succeeded"


def test_score_output_is_exclusively_reserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "score.json"
    expected = {"accepted": True}
    monkeypatch.setattr("house_account.ocr.evaluation.score_review", lambda _: expected)

    score_review_to_file(tmp_path / "review.json", output)
    with pytest.raises(ValueError, match="出力先が既に存在"):
        score_review_to_file(tmp_path / "review.json", output)

    assert json.loads(output.read_text(encoding="utf-8")) == expected


def test_parallel_inference_only_starts_one_worker(
    tmp_path: Path, fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = Barrier(2)
    finish = Event()
    raw = json.loads((fixture_dir / "ocr_result.json").read_text())["result"]
    output = tmp_path / "results" / "result.json"
    worker_calls: list[list[str]] = []

    def check_models(model_dir: Path) -> None:
        ready.wait(timeout=5)

    def recognize(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        worker_calls.append(command)
        assert finish.wait(timeout=10)
        write_json(Path(command[-1]), raw)
        return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("house_account.ocr.evaluation.check_models", check_models)
    monkeypatch.setattr(
        "house_account.ocr.evaluation.subprocess",
        SimpleNamespace(run=recognize, TimeoutExpired=subprocess.TimeoutExpired),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                recognize_for_evaluation,
                tmp_path / "unused.png",
                output,
                Settings(tmp_path, tmp_path),
            )
            for _ in range(2)
        ]
        try:
            done, pending = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
            assert len(done) == len(pending) == 1
            with pytest.raises(ValueError, match="出力先が既に存在"):
                done.pop().result()
        finally:
            finish.set()
        pending.pop().result(timeout=5)

    assert len(worker_calls) == 1
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "succeeded"


@pytest.mark.parametrize("status", ["running", "succeeded", "failed"])
def test_existing_evaluation_record_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    output = tmp_path / "result.json"
    write_json(output, {"status": status, "raw": "保持する原文"})
    original = output.read_bytes()
    monkeypatch.setattr("house_account.ocr.evaluation.check_models", lambda _: None)

    def unexpected_inference(*args: Any, **kwargs: Any) -> None:
        pytest.fail("既存の出力先では推論を開始しない")

    monkeypatch.setattr(
        "house_account.ocr.evaluation.subprocess", SimpleNamespace(run=unexpected_inference)
    )
    with pytest.raises(ValueError, match="出力先が既に存在"):
        recognize_for_evaluation(tmp_path / "unused.png", output, Settings(tmp_path, tmp_path))
    assert output.read_bytes() == original


@pytest.mark.parametrize("failure", ["ocr_failed", "ocr_timeout"])
def test_inference_failure_keeps_a_record_and_rejects_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    output = tmp_path / "result.json"
    monkeypatch.setattr("house_account.ocr.evaluation.check_models", lambda _: None)

    def recognize(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if failure == "ocr_timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr="OCR失敗")

    monkeypatch.setattr(
        "house_account.ocr.evaluation.subprocess",
        SimpleNamespace(run=recognize, TimeoutExpired=subprocess.TimeoutExpired),
    )
    with pytest.raises(RuntimeError):
        recognize_for_evaluation(tmp_path / "unused.png", output, Settings(tmp_path, tmp_path))
    saved = output.read_bytes()
    record = json.loads(saved)
    assert record["status"] == "failed"
    assert record["error_code"] == failure
    with pytest.raises(ValueError, match="出力先が既に存在"):
        recognize_for_evaluation(tmp_path / "unused.png", output, Settings(tmp_path, tmp_path))
    assert output.read_bytes() == saved


def test_same_result_cannot_count_as_multiple_receipts(tmp_path: Path) -> None:
    path = create_review(tmp_path)
    review = json.loads(path.read_text())
    review["receipts"][1]["result"] = review["receipts"][0]["result"]
    write_json(path, review)
    with pytest.raises(ValueError, match="複数枚"):
        score_review(path)

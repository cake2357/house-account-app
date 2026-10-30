import json
import platform
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from house_account.config import Settings
from house_account.ocr.extraction import extract_candidates
from house_account.ocr.models import check_models
from house_account.ocr.serialization import result_from_dict, write_json


def recognize_for_evaluation(image_path: Path, output: Path, settings: Settings) -> None:
    """原文を先に記録し、抽出の失敗でも失わない。推論は子プロセスで制限する。"""
    check_models(settings.model_dir)
    if output.exists():
        raise ValueError("出力先が既に存在します。別のファイル名を指定してください。")
    started = time.monotonic()
    record: dict[str, Any] = {
        "status": "running",
        "evaluated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "candidates": [],
    }
    with tempfile.TemporaryDirectory() as temporary:
        raw_path = Path(temporary) / "raw.json"
        try:
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "house_account.ocr.worker",
                    str(image_path.resolve()),
                    "--model-dir",
                    str(settings.model_dir),
                    "--output",
                    str(raw_path),
                ],
                capture_output=True,
                text=True,
                timeout=settings.ocr_timeout_seconds,
                check=False,
            )
            record["duration_seconds"] = time.monotonic() - started
            if process.returncode != 0:
                record.update(status="failed", error_code="ocr_failed", error=process.stderr)
                write_json(output, record)
                raise RuntimeError(f"OCRが失敗しました。詳細: {output}")
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            record.update(status="recognized", raw=raw)
            write_json(output, record)
            candidates = extract_candidates(result_from_dict(raw))
            record.update(status="succeeded", candidates=[asdict(item) for item in candidates])
            write_json(output, record)
        except subprocess.TimeoutExpired as error:
            record.update(
                status="failed",
                error_code="ocr_timeout",
                duration_seconds=time.monotonic() - started,
                error=f"{settings.ocr_timeout_seconds}秒の制限を超えました。",
            )
            write_json(output, record)
            raise RuntimeError(f"OCRがタイムアウトしました。詳細: {output}") from error
        except Exception:
            if record["status"] == "recognized":
                record.update(status="failed", error_code="extraction_failed")
                write_json(output, record)
            raise


def score_review(review_path: Path) -> dict[str, Any]:
    """人が画像と照合した1対1の対応から行抽出を評価する。誤字は別に数える。"""
    review = json.loads(review_path.read_text(encoding="utf-8"))
    expected_count = candidate_count = matched_count = corrections = 0
    stores: set[str] = set()
    receipt_ids: set[str] = set()
    result_paths: set[Path] = set()
    durations: list[float] = []
    all_successful = True
    receipts = review["receipts"]
    if not receipts:
        raise ValueError("評価対象がありません。")
    for receipt in receipts:
        receipt_id = receipt["id"]
        if not receipt_id or receipt_id in receipt_ids:
            raise ValueError("レシートIDが空か重複しています。")
        receipt_ids.add(receipt_id)
        if not receipt["store_id"]:
            raise ValueError("店舗識別子が必要です。")
        stores.add(receipt["store_id"])
        result_path = (review_path.parent / receipt["result"]).resolve()
        if result_path in result_paths:
            raise ValueError("同じOCR結果を複数枚として評価できません。")
        result_paths.add(result_path)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        all_successful = all_successful and result["status"] == "succeeded"
        duration = float(result["duration_seconds"])
        if not 0 < duration < float("inf"):
            raise ValueError("所要時間が不正です。")
        durations.append(duration)
        candidate_ids = {item["id"] for item in result["candidates"]}
        candidate_count += len(result["candidates"])
        used: set[str] = set()
        for expected in receipt["expected_items"]:
            if not expected["name"].strip():
                raise ValueError("正解商品名を記録してください。")
            expected_count += 1
            matched = expected["candidate_id"]
            if matched is None:
                continue
            if matched not in candidate_ids or matched in used:
                raise ValueError("候補IDが不正か、1候補を複数の正解行へ対応させています。")
            if not isinstance(expected["needs_correction"], bool):
                raise ValueError("needs_correctionは真偽値で指定してください。")
            used.add(matched)
            matched_count += 1
            corrections += int(expected["needs_correction"])
    precision = matched_count / candidate_count if candidate_count else 0.0
    recall = matched_count / expected_count if expected_count else 0.0
    sufficient_sample = len(receipts) >= 5 and len(stores) >= 3 and expected_count >= 30
    return {
        "receipt_count": len(receipts),
        "store_count": len(stores),
        "expected_line_count": expected_count,
        "candidate_count": candidate_count,
        "matched_line_count": matched_count,
        "correction_count": corrections,
        "precision": precision,
        "recall": recall,
        "duration_seconds": durations,
        "sufficient_sample": sufficient_sample,
        "accepted": sufficient_sample
        and all_successful
        and precision >= 0.80
        and recall >= 0.80
        and all(duration <= 300 for duration in durations),
    }

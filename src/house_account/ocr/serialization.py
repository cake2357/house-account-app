import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from house_account.domain.ocr import OcrBlock, OcrResult


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def reserve_output(path: Path) -> None:
    """出力先を排他的に確保し、後続の原子的な保存による競合を防ぐ。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    except FileExistsError as error:
        raise ValueError("出力先が既に存在します。別のファイル名を指定してください。") from error
    os.close(descriptor)


def result_to_dict(result: OcrResult) -> dict[str, Any]:
    return {**asdict(result), "raw_text": result.raw_text}


def result_from_dict(value: dict[str, Any]) -> OcrResult:
    return OcrResult(
        blocks=tuple(
            OcrBlock(
                text=block["text"],
                confidence=block["confidence"],
                polygon=tuple((float(x), float(y)) for x, y in block["polygon"]),
            )
            for block in value["blocks"]
        ),
        models=value["models"],
        versions=value["versions"],
    )

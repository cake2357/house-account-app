"""OCR推論だけを行う子プロセス。候補抽出は原文保存後に呼び出し側で行う。"""

import argparse
from pathlib import Path

from house_account.ocr.engine import PaddleOcrEngine
from house_account.ocr.serialization import result_to_dict, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = PaddleOcrEngine(args.model_dir).recognize(args.image)
    write_json(args.output, result_to_dict(result))


if __name__ == "__main__":
    main()

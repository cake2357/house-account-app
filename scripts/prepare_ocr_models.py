"""通常の推論とは分離して、公式のPP-OCRv5モデルを事前取得する。"""

import argparse
from pathlib import Path

from house_account.config import Settings
from house_account.ocr.models import prepare_models


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=Settings.from_env().model_dir)
    args = parser.parse_args()
    manifest = prepare_models(args.model_dir.resolve())
    print(f"モデルを準備しました: {args.model_dir.resolve()}")
    print(" / ".join(manifest))


if __name__ == "__main__":
    main()

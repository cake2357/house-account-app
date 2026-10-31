"""ローカル画像のOCR結果を記録し、人の対応付けから精度を評価する。"""

import argparse
from pathlib import Path

from house_account.config import Settings
from house_account.ocr.evaluation import recognize_for_evaluation, score_review_to_file


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    recognize = commands.add_parser("recognize", help="画像1枚を読み取り、原文・候補を記録")
    recognize.add_argument("image", type=Path)
    recognize.add_argument("--output", type=Path, required=True)
    recognize.add_argument("--model-dir", type=Path, default=Settings.from_env().model_dir)
    score = commands.add_parser("score", help="人による対応付けを集計")
    score.add_argument("review", type=Path)
    score.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "recognize":
            settings = Settings.from_env()
            recognize_for_evaluation(
                args.image,
                args.output,
                Settings(settings.data_dir, args.model_dir.resolve()),
            )
        else:
            score_review_to_file(args.review, args.output)
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        parser.exit(1, f"{error}\n")
    print(f"記録しました: {args.output}")


if __name__ == "__main__":
    main()

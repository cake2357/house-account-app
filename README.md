# 家計簿アプリ

レシートをPC内のPaddleOCRで読み取り、人が商品名を確認して明細ごとに複数タグを
付けるローカルWebアプリ。Web画面、SQLite保存、OCRキュー、手動タグ編集を利用できます。
OCRの候補は必ず画像と照合してください。タグは自動付与しません。

## Webアプリの起動

```sh
uv sync --locked
uv run python main.py
```

ブラウザで `http://127.0.0.1:8000` を開きます。アプリはループバックに単一プロセスで
起動し、外部ネットワークへ公開しません。OCRモデルを準備していない場合、OCRは失敗状態に
なりますが、商品名は手入力して保存できます。

保存先は `HOUSE_ACCOUNT_DATA_DIR`、既定値はリポジトリ位置の `data/app/` です。
データベース、元画像、OCR用にEXIF回転と縮小を適用した画像が保存されます。
1プロセスだけが同じ保存先を使用できます。バックアップする場合は、アプリ終了後に
保存先全体をコピーしてください。

## セットアップとOCR検証

macOS arm64、Python 3.13、[uv](https://docs.astral.sh/uv/getting-started/installation/)を使う。
PaddleOCR 3.7.0・PaddlePaddle 3.3.1をuv.lockに固定している。

```sh
uv python install
uv sync --locked
# 初回のみネットワークが必要。公式モデル2個を事前取得する
uv run --locked python scripts/prepare_ocr_models.py
# 合成画像で動作確認（実画像はここで指定したローカルパスへ置き換える）
uv run --locked python scripts/evaluate_ocr.py recognize \
  tests/fixtures/synthetic_receipt.png --output artifacts/ocr-evaluation/synthetic.json
```

モデルは`PP-OCRv5_server_det`と日本語対応の`PP-OCRv5_server_rec`を明示する。
モデル仕様は[PaddleOCR公式ガイド](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/OCR.html)、
対応環境は[PaddlePaddle公式資料](https://www.paddlepaddle.org.cn/documentation/docs/en/install/pip/macos-pip_en.html)を参照。
通常の推論ではモデルの自動取得・接続確認を行わず、ローカルファイルを使用する。
未準備なら準備コマンドを案内する。外部OCRへ画像を送信しない。

モデルの既定保存先は`artifacts/ocr-models/`。
`HOUSE_ACCOUNT_MODEL_DIR`またはスクリプトの`--model-dir`で変更できる。
アプリデータの保存先は`HOUSE_ACCOUNT_DATA_DIR`、既定値は`data/app/`。
既定パスは実行時のカレントディレクトリに依存しない。

評価の出力にはOCR文字列・座標・信頼度・モデル名・依存版・環境・所要時間・候補が入る。
推論は子プロセスで300秒に制限する。原文を保存してから候補抽出を行い、抽出失敗でも原文を残す。
出力の上書きは拒否するため、再実行時は別の出力名を指定する。
推論開始前に出力先を排他的に確保し、同じ出力先への並行実行も拒否する。
推論中は`status: running`を記録する。処理を強制終了した場合も出力先は残るため、
記録を確認し、再実行には別の出力名を指定する。集計コマンドも既存の出力を上書きしない。

価格と対応しない行や信頼度0.80未満の行は「要確認」として候補に残す。
店舗の見出し等が残る場合も人が確認して除外する。商品名を推測したり、タグを自動付与したりしない。
合計ラベルと金額からなる行全体が一致した場合に、その後の会計欄を除外する。
先頭位置や数量行との位置関係だけでは見出しと判定せず、不明な行は要確認候補に残す。
原文はすべて保持する。合計欄の判定も誤る場合があるため、画像との照合が必要。

失敗時は出力JSONの`error_code`を確認する。`ocr_failed`は依存・モデル・入力画像、
`ocr_timeout`は300秒制限、`extraction_failed`は候補抽出の失敗を表す。
抽出失敗でも`raw`に原文と座標が残る。モデルが破損している場合は該当するモデルの
ディレクトリを削除して準備スクリプトを再実行する。実画像と評価記録をバックアップする場合は、
OCRコマンドの終了後に`data/`と`artifacts/ocr-evaluation/`をまとめてコピーする。

## 実画像の評価

実画像と評価結果は`data/`または`artifacts/`以下へ置く。Gitには含めない。
各画像に`receipt-01`等の識別子を付けて上記の`recognize`を実行し、結果JSONと画像を照合する。
レビューJSONを次の形式で作る（名称は正解商品名、候補IDは結果JSONのcandidates配列のID）。

```json
{
  "receipts": [
    {
      "id": "receipt-01",
      "store_id": "store-a",
      "result": "receipt-01.json",
      "expected_items": [
        {"name": "牛乳", "candidate_id": "結果JSONの候補ID", "needs_correction": false},
        {"name": "食パン", "candidate_id": null, "needs_correction": false}
      ]
    }
  ]
}
```

`result`はレビューJSONからの相対パス。正解の購入明細をすべて記載し、
抽出されなかった行には`candidate_id: null`を指定する。1候補を複数行に対応させない。
文字の誤りがあっても対応する商品行なら対応付け、`needs_correction: true`で別集計する。
候補に余分な見出しが残っていれば適合率の分母に含める。

```sh
uv run --locked python scripts/evaluate_ocr.py score \
  artifacts/ocr-evaluation/review.json --output artifacts/ocr-evaluation/score.json
```

全画像通算で適合率・再現率80%以上、各画像300秒以内、3店舗以上・5枚以上・
30正解商品行以上を満たしたときにのみ`accepted: true`となる。
先行する3枚の評価と、複数店舗での最終評価（[Issue #3](https://github.com/cake2357/house-account-app/issues/3)）を区別する。

## 開発と検証

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv build
```

設定はpyproject.toml、依存版はuv.lock。両方を同時に更新する。
CIは固定OCR結果で検査し、モデルを取得しない。[テストの詳細](tests/README.md)。
同梱の.agentsはアプリの品質検査から除外し、app・src・scripts・testsは検査する。

app/はWeb層、src/house_account/は業務処理、scripts/は補助コマンド。
スクリプトは業務処理を呼び出す。画面・保存・OCR・タグの仕様は
[実装計画](docs/plans/PLAN.md)、追加画像によるOCR精度の最終評価は
[Issue #3](https://github.com/cake2357/house-account-app/issues/3)で管理する。

## PRの自動レビュー・修正

チェックアウト済みPRをCodex CLIでレビューし、指摘と検証失敗を最大3回修正します。
レビューは `gpt-6.1-sol`、修正は `gpt-6-luna` を既定値とし、
`codex-review.toml` またはCLI引数で個別に変更できます。
`--pr` を指定すると、レビュー・修正ごとの結果を対象PRへコメントします。
PR #17のブランチでコメントを残す場合は、次のように実行します。

```sh
codex login status
gh auth status
gh pr checkout 17
git fetch origin main
uv run --locked python scripts/codex_pr_review_loop.py --pr 17 --dry-run
uv run --locked python scripts/codex_pr_review_loop.py --pr 17
```

PRブランチ上で、未コミット・未追跡の変更がない状態から実行してください。
修正は未コミットで作業ツリーに残り、レビューコメントにもその旨を記載します。
`--pr` を省略すると、コメントを投稿せずに実行します。
VS Codeの「タスクの実行」→「PR: Codexレビュー・修正とコメント」からも起動でき、
コメント先のPR番号を入力できます（初期値は17）。
導入、PRの選択、設定、ログ、終了状態は[自動レビューの手順](docs/codex-pr-review.md)を参照してください。

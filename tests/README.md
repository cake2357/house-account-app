# テスト

Python 3.13とuvで実行する。固定OCR結果と合成画像だけをGitに保存し、通常テストは
実OCRのimport・モデル取得・推論を行わない。

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
```

`testpaths`は`tests/unit`と`tests/integration`。工程0では単体テストを実装し、
工程1から実SQLiteと一時保存先を使う結合テストを追加する。
ブラウザテストは工程2から導入し、`tests/browser`を明示して実行する。

`fixtures/ocr_result.json`は架空の文字列・座標・信頼度・期待商品名を持つ。
`fixtures/synthetic_receipt.png`は架空の日本語レシート。Arial Unicodeで描画した
900×1000pxの合成画像で、実際の購入情報は含まない。

実OCRの評価はREADMEに記載したコマンドで別途実施する。
`data/valid/raw/`の実レシートと`artifacts/ocr-evaluation/`の原文・候補・対応付けは
Git管理外。通常CIの成功と実画像の受け入れ評価は区別する。

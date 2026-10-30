# my-template

Python 3.13 と uv を使う、機械学習アルゴリズム・システム開発用テンプレートです。
依存管理、Ruff、mypy、GitHub Actions の共通基盤を用意しています。
学習・評価・推論の処理は、利用するプロジェクトに合わせて追加してください。

## セットアップ

[uv をインストール](https://docs.astral.sh/uv/getting-started/installation/)し、
リポジトリのルートで次を実行してください。

```sh
uv python install
uv sync --locked
uv run --locked python main.py
```

`.python-version` で指定した Python を使い、`.venv/` に仮想環境を作成します。
`uv sync` は dev 依存もインストールします。以降のコマンドは `uv run` 経由で実行できます。
CI では uv 0.11.25 と Python 3.13 を使用します。

## 開発時のチェック

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
```

自動修正と整形は次のコマンドで行います。

```sh
uv run --locked ruff check --fix .
uv run --locked ruff format .
```

設定は `pyproject.toml` に集約しています。

- Ruff: 基本エラー、未使用コード、import 順序、バグを招きやすい記述、Python 構文を検査します。
  整形の行長は 100 文字を目安にします。
- mypy: 関数の型注釈を必須にし、戻り値の `Any` や不要な型エラー抑制などを検査します。
  型情報が不足する外部ライブラリは、スタブやライブラリ単位の設定で対応してください。
- `data/`、`notebooks/`、`artifacts/` は両ツールのチェック対象外です。

VS Code は `.venv/` の Python を選択してください。
推奨拡張機能を入れると、環境内の Ruff・mypy を使い、Python ファイルの保存時に整形と
修正可能な問題の修正、import の整理を行います。

## 依存関係の管理

依存の追加には `uv add` を使います。以下は numpy と pytest を指定する例です。

```sh
uv add numpy
uv add --dev pytest
```

依存定義を手動で変更した場合は `uv lock`、特定の依存を更新する場合は
`uv lock --upgrade-package numpy` のように実行し、`uv sync --locked` で環境に反映してください。
`pyproject.toml` と `uv.lock` はセットで Git 管理します。`uv.lock` は uv で更新してください。

## ディレクトリ構成

| パス | 用途 |
| --- | --- |
| `main.py` | プロジェクト固有の処理を呼び出す実行入口 |
| `src/` | 再利用する前処理、モデル、評価などのコード |
| `scripts/` | データ取得などの補助スクリプト |
| `notebooks/` | 探索、分析、可視化用の Notebook |
| `data/{train,valid,test}/raw/` | 分割ごとの原データ |
| `data/{train,valid,test}/intermediate/` | 中間データ |
| `data/{train,valid,test}/preprocessed/` | 前処理済みデータ |
| `artifacts/` | 実行時に作成するモデル、設定、指標などの成果物 |
| `.github/workflows/ci.yml` | Ruff・mypy を実行する CI |

Notebook で繰り返し使う処理は `src/` に移してください。
Python パッケージとしてインストールする場合は、パッケージ名とビルド設定を定義してください。

### データと成果物

`data/` はディレクトリ構造を保つ `.gitkeep` のみを Git 管理します。
データの取得元、版、分割方法、前処理手順はプロジェクトごとに記録してください。
モデルや指標などの実験出力は `artifacts/`、ログは `logs/` に保存してください。
これらの出力先と `.env` 系のファイルは Git 管理外です。`.env.example` は管理できます。

## 新しいプロジェクトで変更する箇所

1. `pyproject.toml` の `name`、`description`、必要な依存を更新します。
2. この README のタイトル、プロジェクト概要、実行方法を更新します。
3. `main.py` に実行処理を記述し、必要に応じて `src/` に分割します。
4. `.github/copilot-instructions.md` のタイトルとプロジェクト概要を記述します。
5. `uv lock` と `uv sync --locked` を実行し、開発時のチェックを通します。

Python の基準版を変更する場合は、`.python-version`、`requires-python`、Ruff の
`target-version`、mypy の `python_version` を合わせて更新してください。

## CI とテスト

push、Pull Request、手動実行で、ロックファイルからの環境構築、Ruff の検査・整形確認、
mypy の型検査を実行します。

pytest は dev 依存に含めています。最初の機能追加に合わせて `tests/` にテストを作成し、
`uv run --locked pytest` を CI に追加してください。

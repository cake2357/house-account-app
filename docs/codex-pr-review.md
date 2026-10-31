# Codex CLIによるPRレビュー・修正

ローカルにチェックアウトしたPRの差分をレビューし、指摘と検証失敗を修正します。
通常の `codex exec --output-schema` を使い、Pythonが終了条件を判定します。
Python 3.13以上、macOSまたはLinux、Git、Codex CLIが必要です。
スクリプト本体は標準ライブラリだけを使います。

## 初回設定

Codex CLIがない環境では、Node.js/npmを用意してインストールします。

```sh
npm install -g @openai/codex
codex login
codex login status
codex exec --help
uv sync --locked
```

既存のCodex CLI認証を再利用します。`~/.codex/config.toml` の書き換えや
APIキーのリポジトリへの保存は不要です。CLIがPATHにない場合は
`--codex-bin /absolute/path/to/codex` を指定してください。
今回確認したCLIは `0.164.0-alpha.11.1` です。
`--output-schema`、`--output-last-message`、`--ephemeral` と
`read-only` / `workspace-write` に対応する版を使ってください。

## 対象PRの準備と実行

例の `123` と `main` は対象PRの番号とbaseブランチに置き換えます。
GitHub CLIでの操作は利用者が行います。スクリプトはPR取得・checkout・fetchを行いません。

```sh
gh pr checkout 123
git fetch origin main
git status --short
uv run --locked python scripts/codex_pr_review_loop.py --base origin/main --dry-run
uv run --locked python scripts/codex_pr_review_loop.py --base origin/main
```

未コミット変更（未追跡ファイルを含む）があると開始しません。
必要な変更は確認してコミットするか、別のworktreeを使ってください。
テスト等が作るキャッシュはあらかじめ `.gitignore` に登録してください。
main/master、baseと同一コミット、detached HEADでは実行しません。
同じ作業ツリーでの二重起動も拒否します。実行中は別のエディター・プロセスから
対象ファイルを編集しないでください。

スクリプトがまだ含まれないPRでも、外側にある本スクリプトから起動できます。
`--repo` の先でチェックを実行し、モデルがその作業ツリーを編集します。

```sh
python3 /path/to/automation/scripts/codex_pr_review_loop.py \
  --repo /path/to/pr-checkout \
  --config /path/to/automation/codex-review.toml \
  --base origin/main
```

## 設定

優先順位はCLI引数、`--config` で指定したTOML（省略時は対象repoの
`codex-review.toml`）、組み込み既定値の順です。設定ファイルがない場合は
既定値で動作しますが、明示したファイルがない場合はエラーにします。

| 設定 | 既定値 | 内容 |
| --- | --- | --- |
| `review_model` | `gpt-6.1-sol` | レビュー用。`--review-model` で上書き |
| `fix_model` | `gpt-6-luna` | 修正用。`--fix-model` で上書き |
| `base` | `origin/main` | PRのbase ref。開始時のmerge-baseを固定 |
| `max_fixes` | `3` | 修正回数。0ならレビューと検証だけ |
| `codex_timeout` | `900` | 各Codex呼び出しの秒数上限 |
| `check_timeout` | `300` | 各検証コマンドの秒数上限 |
| `checks` | Ruff検査・整形確認・mypy・pytest | 空のリストは不可 |

モデル名はアカウントで利用可能なIDに変更できます。利用可否は実際のCLI応答で
確認してください。`review_model` という設定名を使っていますが、これは本スクリプトの
設定で、各呼び出しの `--model` に渡します。

```sh
uv run --locked python scripts/codex_pr_review_loop.py \
  --review-model gpt-6.1-sol --fix-model gpt-6-luna --max-fixes 3

# 指定したコマンドで検証リスト全体を置き換える
uv run --locked python scripts/codex_pr_review_loop.py \
  --check 'uv run --locked ruff check .' \
  --check 'uv run --locked pytest -q'
```

コマンドは引数配列として実行します。`&&`、パイプ、環境変数展開はしません。
`ruff format --check` のようなファイルを書き換えない検証を指定してください。
検証はホスト上で実行するため、自分で確認したリポジトリとコマンドを対象にします。

VS Codeでは「ターミナル → タスクの実行」から
「PR: Codexレビュー設定の確認」または「PR: Codexレビュー・修正」を選べます。
モデルやbaseの変更は `codex-review.toml` を編集して保存・コミットします。

## 処理と終了条件

各ラウンドで全チェックを実行し、レビュー結果をJSONで取得します。
差分は固定merge-baseから現在の作業ツリーまでで、修正後の未コミット変更と
未追跡の新規ファイルもレビュー対象です。レビューのJSONはPython側でも型・必須キーを検査します。

指摘またはチェック失敗があれば修正モデルへ渡します。失敗ログは末尾16,000文字と
完全なログのパスを渡し、修正後に全チェックとレビューをやり直します。
最大3回の修正では最大4回のレビューとなり、最後の修正も検証します。

| 結果 | 終了コード | 意味 |
| --- | --- | --- |
| `success` | 0 | 指摘0件かつ全チェック成功 |
| `max_fixes` | 1 | 修正上限に達し、指摘またはチェック失敗が残る |
| `stalled` | 1 | 修正による変更なし、または以前の状態へ戻った |
| `needs_human` | 1 | レビューまたは修正で仕様判断等が必要 |
| `error` | 2 | 不正JSON、CLI失敗、時間切れ、Git状態の変化等 |
| `interrupted` | 130 | Ctrl+Cによる中断 |

`--dry-run` は設定・Git状態だけを表示して0で終了します。AI・検証は実行しません。
タイムアウトとCtrl+Cでは実行中のプロセスグループを終了します。
どの終了状態でも修正済みファイルはそのまま残し、自動で巻き戻しません。
再実行前に差分を確認・コミットしてください。

## ログと権限

開始時に表示されるOSの一時ディレクトリ `codex-pr-review-*` に、設定、各呼び出しの
プロンプト・Schema・JSON回答・出力ログ、検証結果、最終 `result.json` を保存します。
ログにはコードや指摘が含まれます。残す場合は自分の保管先へコピーしてください。
ログは対象リポジトリの外に置き、レビュー差分や変更停滞の判定から除外します。

レビューは `read-only`、修正は `workspace-write`、承認要求は `never` です。
修正用シェルのネットワークアクセスは無効にします。モデル呼び出し自体にはネットワークが必要です。
既存のCodex設定・MCP連携等は引き継ぐため、この設定だけで全外部操作を隔離するものではありません。
commit・stage・push・PR投稿はプロンプトで禁止し、HEAD・ブランチ・indexの変更を検出したら停止します。
無権限化を保証する仕組みではないため、不要な外部連携はCodex側で無効にしてください。

最後に `git diff` と `git status --short` で新規ファイルも確認し、人がcommit・push・mergeを判断します。
GitHub上の既存レビューコメントの取得や解決、PR更新イベントからの自動起動は含みません。

## 検証と参考資料

テストは一時Gitリポジトリと模擬CLIで実行します。モデルのレビュー品質と
特定アカウントでのモデル利用可否は、これらのテストでは保証しません。

2026-10-31、macOS arm64・Python 3.13.14・Codex CLI 0.164.0-alpha.11.1で
一時リポジトリを使った実モデルの動作確認も行いました。
`gpt-6.1-sol` が加算を減算に変えた不具合を1件指摘し、`gpt-6-luna` が
修正と新規の回帰テストを作成しました。再検証成功・再レビュー指摘0件で終了し、
変更が未コミットのまま残ることを確認しました。検証コマンドは
`python -B -c 'from calculator import add; assert add(2, 3) == 5'` です。
これは小規模な疎通確認であり、既存アプリのPR全体への実モデル実行は未実施です。

```sh
uv run --locked pytest -q
```

- [OpenAI公式: 非対話モード・構造化出力・認証](https://learn.chatgpt.com/docs/non-interactive-mode)
- [OpenAI公式: CLIオプション](https://learn.chatgpt.com/docs/developer-commands?surface=cli)

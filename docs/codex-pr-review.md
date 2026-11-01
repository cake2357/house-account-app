# Codex CLIによるPRレビュー・修正

ローカルにチェックアウトしたPRの差分をレビューし、指摘と検証失敗を修正します。
通常の `codex exec --output-schema` を使い、Pythonが終了条件を判定します。
`--pr` でPR番号を指定すると、各レビュー・修正結果をPRにコメントします。
`--publish-fixes` を併用すると、指摘0件かつ全検証成功後の修正をcommit・pushします。
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

## レビュー・修正結果をPRにコメントする

GitHub CLIの認証と、対象PRへのコメント権限が必要です。
PR番号は `--repo` の作業ツリーからGitHub CLIが選択するリポジトリ内で解釈します。
必要に応じて、事前に `gh repo set-default OWNER/REPO` で対象を選択してください。

```sh
gh auth status
gh pr checkout 123
git fetch origin main
uv run --locked python scripts/codex_pr_review_loop.py --base origin/main --pr 123 --publish-fixes
```

開始時にPRがオープン中で、PRのheadブランチ名・コミットSHAがローカルの
ブランチ・HEADと一致することを確認します。一致しない場合はAI・検証を実行せず停止します。
途中のコメントには修正が未反映であること、反映後の最終コメントにはコミットSHAを記載します。

| タイミング | コメント内容 |
| --- | --- |
| 各レビュー後 | 回数、モデル、開始時のHEAD、要約、重要度・ファイル・行番号付きの指摘、検証コマンドごとの成否 |
| 各修正後 | 回数、モデル、開始時のHEAD、修正要約、次のラウンドで再検証する旨 |
| 人の判断が必要・停滞・修正上限 | 該当するレビューまたは修正のコメントに停止理由を記載 |
| 最終レビュー後 | 指摘0件かつ全検証成功、反映したSHA、または残る指摘・検証失敗を記載 |

例として1回の修正で成功した場合、「レビュー1 → 修正1 → レビュー2」の
3件をPRの会話コメントに追加します。レビュー承認や行単位のインラインコメントは作りません。
モデルは投稿を行わず、Python側が `gh pr comment --body-file` で投稿します。
検証ログの全文は投稿せず、ローカルの実行ログに保持します。

`--pr` を省略するとGitHub CLIを呼び出さず、従来どおりローカルで実行します。
実行開始時に「PRコメント: 有効」とコメント先URL、または
「PRコメント: 無効（ローカルのみ）」と `--pr <PR番号>` の案内を表示します。
ローカル実行の終了時にも、コメントを投稿していないことを表示します。
`--dry-run --pr 123` はPR番号を含む設定を表示するだけで、GitHubへの問い合わせや投稿も行いません。
GitHub CLIがPATHにない場合は `--gh-bin /absolute/path/to/gh` を指定できます。

### 検証済みの修正をcommit・pushする

`--pr 123 --publish-fixes` は、全チェック成功・再レビュー指摘0件になった時点で、
Python側が今回の修正を1コミットにまとめてpushします。通常のVS Codeタスクでも有効です。
`--pr` のみならコメント投稿だけを行い、修正は未コミットで残します。
`--publish-fixes` 単独はエラーです。`--dry-run` ではGitHubへの接続・commit・pushは行いません。

Gitのコミット用ユーザー設定と、PRのheadリポジトリへのpush権限が必要です。
push先は現在のブランチのupstream remote（未設定なら `origin`）で、
HTTPSまたはSSHのURLがPRのheadリポジトリを指すことを確認します。
forkのPRはforkを指すremoteをupstreamに設定してください。
複数のpush先URLがあるremoteは使用しません。

反映直前にPRの状態・ブランチ・HEAD、remoteのHEAD、検証後のファイル変更を確認します。
一時indexで検証した内容を固定し、commit後も内容・親コミット・作業ツリーを確認します。
pushは対象ブランチだけに行い、force pushはしません。修正不要なら新しいコミットは作りません。
検証失敗、停滞、修正上限、人の判断待ちの場合もcommit・pushしません。

失敗時は `error` で停止し、変更や作成済みコミットを保持します。自動で再pushしません。
`publication/result.json` の `preparing` / `committed` / `pushed`、`head` と、
`stage.log` / `commit.log` / `push.log`、各コマンドのJSONを確認してください。
commit失敗ではステージ済み変更が残る場合があります。pushの通信失敗では反映済みの可能性もあるため、
ローカルとremoteのHEADを確認してから再開します。push後のコメント投稿失敗でもcommitは取り消しません。

投稿が失敗・時間切れになった場合は `error`（終了コード2）で停止します。
投稿本文 `comment.md`、コマンド `comment-command.json`、応答 `comment.log` を
各 `review-*` / `fix-*` ディレクトリに保存し、修正済みファイルも残します。
通信失敗時に投稿が完了している場合があるため、自動再投稿は行いません。
PR上のコメントを確認してから再開してください。

### レビューは動いたのにコメントがない場合

実行ログの `settings.json` で `pr` を確認してください。`null` なら
`--pr` が指定されていないため、投稿処理は実行されていません。
この場合、過去のレビュー結果はローカルの `review-*/answer.json` に残っています。
次の実行では `--pr <PR番号>` またはコメントを投稿するVS Codeタスクを使ってください。
修正済みの未コミット変更がある場合は、内容を確認・コミット・pushし、
PRのHEADと一致したクリーンな状態にしてから実行します。

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

VS Codeでは「ターミナル → タスクの実行」から次のタスクを選べます。

| タスク | 動作 |
| --- | --- |
| PR: Codexレビュー・修正 | PR番号を入力し、検証済み修正をcommit・pushして結果をコメントする |
| PR: Codexレビュー・修正とコメント | 通常のレビュー・修正タスクと同じ動作（従来名も利用可能） |
| PR: Codexレビュー・修正（ローカルのみ） | コメントを投稿せずにレビュー・修正する |
| PR: Codexレビュー設定の確認 | 設定・Git状態を表示する。AI・検証・GitHubへの接続は行わない |

コメント先にはチェックアウト中のPR番号を入力します。初期値はありません。
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

`--dry-run` は設定・Git状態だけを表示して0で終了します。AI・検証・GitHubへの接続は実行しません。
タイムアウトとCtrl+Cでは実行中のプロセスグループを終了します。
修正済みファイルや作成済みコミットは自動で巻き戻しません。
自動反映を使わない場合や途中停止した場合は、再実行前に差分・HEADを確認してください。

## ログと権限

開始時に表示されるOSの一時ディレクトリ `codex-pr-review-*` に、設定、各呼び出しの
プロンプト・Schema・JSON回答・出力ログ、検証結果、最終 `result.json` を保存します。
ログにはコードや指摘が含まれます。残す場合は自分の保管先へコピーしてください。
ログは対象リポジトリの外に置き、レビュー差分や変更停滞の判定から除外します。

レビューは `read-only`、修正は `workspace-write`、承認要求は `never` です。
修正用シェルのネットワークアクセスは無効にします。モデル呼び出し自体にはネットワークが必要です。
既存のCodex設定・MCP連携等は引き継ぐため、この設定だけで全外部操作を隔離するものではありません。
モデルによるcommit・stage・push・PR/コメント投稿はプロンプトで禁止し、
HEAD・ブランチ・indexの変更を検出したら停止します。
`--pr` 指定時のコメント投稿と、`--publish-fixes` 指定時の検証後のcommit・pushはPython側で行います。
無権限化を保証する仕組みではないため、不要な外部連携はCodex側で無効にしてください。

最後に `git diff`、`git status --short`、反映したコミットを確認し、人がmergeを判断します。
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

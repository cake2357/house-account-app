from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import codex_pr_review_loop as loop

FAKE_CODEX = r"""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
assert args[:3] == ["--ask-for-approval", "never", "exec"]
assert "--output-schema" in args
assert "--dangerously-bypass-approvals-and-sandbox" not in args
repo = Path(args[args.index("--cd") + 1])
state = Path(os.environ["FAKE_STATE"])
scenario = os.environ.get("FAKE_SCENARIO", "success")
review = args[args.index("--sandbox") + 1] == "read-only"
assert args[args.index("--model") + 1] == ("review-model" if review else "fix-model")
prompt = sys.stdin.read()
with state.open("a") as output:
    output.write(json.dumps({"review": review, "prompt": prompt}) + "\n")
answer = {"needs_human": False, "reason": "", "summary": os.environ.get("FAKE_SUMMARY", "result")}
fixed = (repo / "new.py").exists()
if review:
    answer["findings"] = [] if fixed or scenario in ("checks_only", "clean") else [
        {"priority": "P1", "file": "app.py", "line": 1, "message": "repair this"}
    ]
    if scenario == "human":
        answer.update(needs_human=True, reason="仕様を確認してください")
    if scenario == "malformed":
        answer = {"findings": []}
    if scenario == "review_writes":
        (repo / "app.py").write_text("changed")
    if scenario == "oscillate":
        answer["findings"] = [{"priority": "P1", "file": "app.py", "line": 1, "message": "x"}]
else:
    if scenario == "fix_human":
        answer.update(needs_human=True, reason="要判断")
    elif scenario == "commit":
        import subprocess
        (repo / "new.py").write_text("ok")
        subprocess.run(["git", "add", "new.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "unexpected"], cwd=repo, check=True)
    elif scenario == "oscillate" and fixed:
        (repo / "new.py").unlink()
    elif scenario != "no_change":
        (repo / "new.py").write_text("ok")
if scenario == "cli_error":
    sys.exit(5)
path = Path(args[args.index("--output-last-message") + 1])
if scenario == "missing_answer":
    sys.exit(0)
if scenario == "invalid_json":
    path.write_text("{invalid")
    sys.exit(0)
path.write_text(json.dumps(answer))
"""


FAKE_GH = r"""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
state = Path(os.environ["FAKE_GH_STATE"])
scenario = os.environ.get("FAKE_PR_SCENARIO", "success")
call = {"args": args}
if args[:2] == ["pr", "comment"]:
    assert args[2] == "https://github.com/example/repo/pull/123"
    assert args[3] == "--body-file"
    call["body"] = Path(args[4]).read_text()
previous = [json.loads(line) for line in state.read_text().splitlines()] if state.exists() else []
with state.open("a") as output:
    output.write(json.dumps(call) + "\n")
if args[:2] == ["pr", "view"]:
    assert args == ["pr", "view", "123", "--json", "url,state,headRefName,headRefOid"]
    value = {
        "url": "https://github.com/example/repo/pull/123",
        "state": "OPEN",
        "headRefName": "feat/123-test",
        "headRefOid": os.environ["FAKE_PR_HEAD"],
    }
    if scenario == "closed":
        value["state"] = "CLOSED"
    elif scenario == "branch":
        value["headRefName"] = "other-branch"
    elif scenario == "head":
        value["headRefOid"] = "0" * 40
    elif scenario == "malformed":
        value = []
    elif scenario == "invalid_json":
        print("{invalid")
        sys.exit(0)
    elif scenario == "error":
        print("not authenticated", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(value))
elif args[:2] == ["pr", "comment"]:
    index = 1 + sum("body" in entry for entry in previous)
    if str(index) == os.environ.get("FAKE_GH_FAIL_AT"):
        print("comment rejected", file=sys.stderr)
        sys.exit(1)
    print(f"https://github.com/example/repo/pull/123#issuecomment-{index}")
else:
    raise AssertionError(args)
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    loop.git(path, "init", "-b", "main")
    loop.git(path, "config", "user.email", "test@example.invalid")
    loop.git(path, "config", "user.name", "Test")
    loop.git(path, "config", "commit.gpgsign", "false")
    (path / "app.py").write_text("initial\n")
    loop.git(path, "add", ".")
    loop.git(path, "commit", "-m", "base")
    loop.git(path, "checkout", "-b", "feat/123-test")
    (path / "app.py").write_text("broken\n")
    loop.git(path, "commit", "-am", "feature")
    return path


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "codex"
    path.write_text(f"#!{sys.executable}\n" + FAKE_CODEX)
    path.chmod(0o755)
    monkeypatch.setenv("FAKE_STATE", str(tmp_path / "calls.jsonl"))
    return path


@pytest.fixture
def fake_gh(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "gh"
    path.write_text(f"#!{sys.executable}\n" + FAKE_GH)
    path.chmod(0o755)
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "gh-calls.jsonl"))
    monkeypatch.setenv("FAKE_PR_HEAD", loop.git(repo, "rev-parse", "HEAD").decode().strip())
    return path


def arguments(repo: Path, fake_codex: Path) -> list[str]:
    check = f"{sys.executable} -c \"from pathlib import Path; assert Path('new.py').exists()\""
    return [
        "--repo",
        str(repo),
        "--base",
        "main",
        "--codex-bin",
        str(fake_codex),
        "--review-model",
        "review-model",
        "--fix-model",
        "fix-model",
        "--check",
        check,
    ]


@pytest.mark.parametrize(
    ("scenario", "status", "code", "calls"),
    [
        ("success", "success", 0, 3),
        ("checks_only", "success", 0, 3),
        ("human", "needs_human", 1, 1),
        ("fix_human", "needs_human", 1, 2),
        ("no_change", "stalled", 1, 2),
        ("oscillate", "stalled", 1, 4),
        ("malformed", "error", 2, 1),
        ("missing_answer", "error", 2, 1),
        ("invalid_json", "error", 2, 1),
        ("cli_error", "error", 2, 1),
        ("review_writes", "error", 2, 1),
        ("commit", "error", 2, 2),
    ],
)
def test_end_to_end(
    repo: Path,
    fake_codex: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: str,
    status: str,
    code: int,
    calls: int,
) -> None:
    monkeypatch.setenv("FAKE_SCENARIO", scenario)
    original_head = loop.git(repo, "rev-parse", "HEAD")
    assert loop.main(arguments(repo, fake_codex)) == code
    output = capsys.readouterr()
    directory = Path(output.out.split("ログ: ", 1)[1].splitlines()[0])
    assert json.loads((directory / "result.json").read_text())["status"] == status
    invocations = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert len(invocations) == calls
    assert "git ls-files --others" in invocations[0]["prompt"]
    if status == "success":
        assert "AssertionError" in invocations[1]["prompt"]
        assert (repo / "new.py").exists()
        assert loop.git(repo, "rev-parse", "HEAD") == original_head
        assert b"?? new.py" in loop.git(repo, "status", "--porcelain")
        assert (directory / "review-01" / "answer.json").exists()


@pytest.mark.parametrize("max_fixes", [0, 1])
def test_maximum_is_rechecked(
    repo: Path, fake_codex: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], max_fixes: int
) -> None:
    args = arguments(repo, fake_codex) + [
        "--max-fixes",
        str(max_fixes),
        "--check",
        f'{sys.executable} -c "raise ValueError()"',
    ]
    assert loop.main(args) == 1
    assert "max_fixes:" in capsys.readouterr().out
    assert len((tmp_path / "calls.jsonl").read_text().splitlines()) == 2 * max_fixes + 1


def test_clean_review_and_passing_checks_need_no_fix(
    repo: Path, fake_codex: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SCENARIO", "clean")
    args = arguments(repo, fake_codex)[:-2] + ["--check", f'{sys.executable} -c "pass"']
    assert loop.main(args) == 0
    assert len((tmp_path / "calls.jsonl").read_text().splitlines()) == 1
    assert not loop.git(repo, "status", "--porcelain")


@pytest.mark.parametrize("kind", ["untracked", "unstaged", "staged", "detached", "base", "missing"])
def test_preflight_rejects_unsafe_start(
    repo: Path, fake_codex: Path, tmp_path: Path, kind: str
) -> None:
    args = arguments(repo, fake_codex)
    if kind == "untracked":
        (repo / "new.py").touch()
    elif kind in ("unstaged", "staged"):
        (repo / "app.py").write_text("edited")
        if kind == "staged":
            loop.git(repo, "add", ".")
    elif kind == "detached":
        loop.git(repo, "checkout", "--detach")
    elif kind == "base":
        loop.git(repo, "checkout", "main")
    else:
        args += ["--base", "missing-branch"]
    assert loop.main(args) == 2
    assert not (tmp_path / "calls.jsonl").exists()


def test_dry_run_and_config_overrides(
    repo: Path, fake_codex: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "settings.toml"
    config.write_text('review_model = "from-config"\nmax_fixes = 7\nbase = "main"\n')
    args = ["--repo", str(repo), "--config", str(config), "--dry-run", "--fix-model", "custom"]
    assert loop.main(args) == 0
    metadata = json.loads(capsys.readouterr().out)
    assert metadata["review_model"] == "from-config"
    assert metadata["fix_model"] == "custom"
    assert metadata["max_fixes"] == 7
    assert metadata["merge_base"] == loop.git(repo, "rev-parse", "main").decode().strip()
    assert not (tmp_path / "calls.jsonl").exists()


@pytest.mark.parametrize(
    "content",
    [
        "max_fixes = -1",
        "max_fixes = true",
        "checks = []",
        'checks = ["pytest"]',
        "codex_timeout = 0",
        'review_model = ""',
        "typo = 1",
        "not toml",
    ],
)
def test_invalid_config(repo: Path, tmp_path: Path, content: str) -> None:
    config = tmp_path / "config.toml"
    config.write_text(content)
    assert loop.main(["--repo", str(repo), "--config", str(config), "--dry-run"]) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"findings": []},
        {"needs_human": "false", "reason": "", "summary": "", "findings": []},
        {"needs_human": True, "reason": "", "summary": "", "findings": []},
        {"needs_human": False, "reason": "", "summary": "", "findings": [{}]},
        {"needs_human": False, "reason": "", "summary": "", "findings": None},
    ],
)
def test_rejects_invalid_answer(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "answer.json"
    loop.write_json(path, payload)
    with pytest.raises(loop.LoopError):
        loop.read_answer(path, True)


def test_snapshot_covers_untracked_edits_and_mode(repo: Path) -> None:
    base = loop.git(repo, "rev-parse", "main").decode().strip()
    states = [loop.snapshot(repo, base)]
    path = repo / "new.py"
    path.write_text("one")
    states.append(loop.snapshot(repo, base))
    path.write_text("two")
    states.append(loop.snapshot(repo, base))
    path.chmod(0o755)
    states.append(loop.snapshot(repo, base))
    assert len(set(states)) == 4
    path.unlink()
    assert loop.snapshot(repo, base) == states[0]


def test_check_must_not_modify_code(repo: Path, fake_codex: Path, tmp_path: Path) -> None:
    args = arguments(repo, fake_codex) + [
        "--check",
        f'''{sys.executable} -c "from pathlib import Path; Path('app.py').write_text('x')"''',
    ]
    assert loop.main(args) == 2
    assert not (tmp_path / "calls.jsonl").exists()


def test_process_timeout_keeps_log(tmp_path: Path) -> None:
    log = tmp_path / "process.log"
    with pytest.raises(loop.LoopError, match="時間切れ"):
        loop.run_process(
            [sys.executable, "-u", "-c", "import time; print('started'); time.sleep(20)"],
            tmp_path,
            log,
            1,
        )
    assert "started" in log.read_text()


def test_same_worktree_lock(repo: Path, fake_codex: Path) -> None:
    lock_path = (
        Path(loop.git(repo, "rev-parse", "--absolute-git-dir").decode().strip())
        / "codex-review.lock"
    )
    with lock_path.open("a") as lock:
        loop.fcntl.flock(lock, loop.fcntl.LOCK_EX | loop.fcntl.LOCK_NB)
        assert loop.main(arguments(repo, fake_codex)) == 2


def test_check_is_not_executed_through_shell(tmp_path: Path) -> None:
    log = tmp_path / "process.log"
    payload = "$(touch should-not-exist); echo no"
    assert (
        loop.run_process(
            [sys.executable, "-c", "import sys; print(sys.argv[1])", payload], tmp_path, log, 5
        )
        == 0
    )
    assert payload in log.read_text()
    assert not (tmp_path / "should-not-exist").exists()


def test_pr_comments_follow_review_fix_and_recheck(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = "日本語の要約\n$(touch should-not-exist); `touch should-not-exist`"
    monkeypatch.setenv("FAKE_SUMMARY", summary)
    args = arguments(repo, fake_codex) + ["--pr", "123", "--gh-bin", str(fake_gh)]
    assert loop.main(args) == 0
    calls = [json.loads(line) for line in (tmp_path / "gh-calls.jsonl").read_text().splitlines()]
    assert len(calls) == 4
    comments = calls[1:]
    assert [entry["body"].splitlines()[0] for entry in comments] == [
        "## Codex レビュー 1",
        "## Codex 修正 1",
        "## Codex レビュー 2",
    ]
    first, fix, final = [entry["body"] for entry in comments]
    assert "**P1** `app.py:1`: repair this" in first
    assert "失敗:" in first and "終了コード 1" in first
    assert "review-model" in first
    assert "fix-model" in fix
    assert "次のラウンドで再検証" in fix
    assert "### 検証結果" not in fix
    assert "指摘0件・すべての検証が成功" in final
    assert "成功:" in final and "終了コード 0" in final
    for entry in comments:
        assert summary in entry["body"]
        assert "未コミット・未push" in entry["body"]
        assert loop.git(repo, "rev-parse", "HEAD").decode().strip() in entry["body"]
        body = Path(entry["args"][4])
        assert body.read_text() == entry["body"]
        assert "issuecomment-" in body.with_name("comment.log").read_text()
        assert body.with_name("comment-command.json").exists()
    assert not (repo / "should-not-exist").exists()
    assert (repo / "new.py").exists()


@pytest.mark.parametrize("label", ["PR: Codexレビュー・修正", "PR: Codexレビュー・修正とコメント"])
def test_vscode_review_tasks_post_to_pr(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    label: str,
) -> None:
    config = Path(__file__).resolve().parents[2] / ".vscode" / "tasks.json"
    task = next(task for task in json.loads(config.read_text())["tasks"] if task["label"] == label)
    task_args = [arg.replace("${input:prNumber}", "123") for arg in task["args"][4:]]
    assert loop.main(arguments(repo, fake_codex) + task_args + ["--gh-bin", str(fake_gh)]) == 0
    output = capsys.readouterr().out
    assert "PRコメント: 有効" in output
    assert "コメント先: https://github.com/example/repo/pull/123" in output
    assert output.index("PRコメント: 有効") < output.index("レビュー 1 / 修正済み 0")
    calls = [json.loads(line) for line in (tmp_path / "gh-calls.jsonl").read_text().splitlines()]
    assert sum("body" in entry for entry in calls) == 3


@pytest.mark.parametrize("use_task", [False, True])
def test_local_mode_explains_why_no_comments_are_posted(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    use_task: bool,
) -> None:
    args = arguments(repo, fake_codex) + ["--gh-bin", str(fake_gh)]
    if use_task:
        config = Path(__file__).resolve().parents[2] / ".vscode" / "tasks.json"
        task = next(
            task
            for task in json.loads(config.read_text())["tasks"]
            if task["label"] == "PR: Codexレビュー・修正（ローカルのみ）"
        )
        args += task["args"][4:]
    assert loop.main(args) == 0
    output = capsys.readouterr().out
    assert "PRコメント: 無効（ローカルのみ）" in output
    assert "--pr <PR番号>" in output
    assert output.index("PRコメント: 無効") < output.index("レビュー 1 / 修正済み 0")
    assert "PRコメントは投稿していません" in output
    assert not (tmp_path / "gh-calls.jsonl").exists()


@pytest.mark.parametrize(
    ("scenario", "count", "message"),
    [
        ("human", 1, "仕様を確認してください"),
        ("fix_human", 2, "要判断"),
        ("no_change", 2, "停滞"),
    ],
)
def test_pr_comments_include_human_decisions_and_stalls(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    count: int,
    message: str,
) -> None:
    monkeypatch.setenv("FAKE_SCENARIO", scenario)
    args = arguments(repo, fake_codex) + ["--pr", "123", "--gh-bin", str(fake_gh)]
    assert loop.main(args) == 1
    calls = [json.loads(line) for line in (tmp_path / "gh-calls.jsonl").read_text().splitlines()]
    comments = [entry["body"] for entry in calls if "body" in entry]
    assert len(comments) == count
    assert message in comments[-1]


@pytest.mark.parametrize(
    "scenario", ["closed", "branch", "head", "malformed", "invalid_json", "error"]
)
def test_pr_must_match_checkout_before_model_execution(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    monkeypatch.setenv("FAKE_PR_SCENARIO", scenario)
    args = arguments(repo, fake_codex) + ["--pr", "123", "--gh-bin", str(fake_gh)]
    assert loop.main(args) == 2
    calls = (tmp_path / "gh-calls.jsonl").read_text().splitlines()
    assert len(calls) == 1
    assert not (tmp_path / "calls.jsonl").exists()
    assert not loop.git(repo, "status", "--porcelain")


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_comment_failure_stops_loop_and_preserves_body_and_repairs(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fail_at: int,
) -> None:
    monkeypatch.setenv("FAKE_GH_FAIL_AT", str(fail_at))
    args = arguments(repo, fake_codex) + ["--pr", "123", "--gh-bin", str(fake_gh)]
    assert loop.main(args) == 2
    output = capsys.readouterr()
    assert "PRコメントの投稿に失敗" in output.err
    directory = Path(output.out.split("ログ: ", 1)[1].splitlines()[0])
    assert json.loads((directory / "result.json").read_text())["status"] == "error"
    calls = [json.loads(line) for line in (tmp_path / "gh-calls.jsonl").read_text().splitlines()]
    comments = [entry for entry in calls if "body" in entry]
    assert len(comments) == fail_at
    assert len((tmp_path / "calls.jsonl").read_text().splitlines()) == fail_at
    body = Path(comments[-1]["args"][4])
    assert body.read_text() == comments[-1]["body"]
    assert "comment rejected" in body.with_name("comment.log").read_text()
    assert (repo / "new.py").exists() == (fail_at > 1)


@pytest.mark.parametrize("dry_run", [False, True])
def test_github_is_not_called_without_pr_or_during_dry_run(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    tmp_path: Path,
    dry_run: bool,
) -> None:
    args = arguments(repo, fake_codex) + ["--gh-bin", str(fake_gh)]
    if dry_run:
        args += ["--pr", "123", "--dry-run"]
    assert loop.main(args) == 0
    assert not (tmp_path / "gh-calls.jsonl").exists()
    assert (tmp_path / "calls.jsonl").exists() != dry_run


@pytest.mark.parametrize("max_fixes", [0, 1])
def test_last_review_comments_on_remaining_failures(
    repo: Path, fake_codex: Path, fake_gh: Path, tmp_path: Path, max_fixes: int
) -> None:
    args = arguments(repo, fake_codex) + [
        "--pr",
        "123",
        "--gh-bin",
        str(fake_gh),
        "--max-fixes",
        str(max_fixes),
        "--check",
        f'{sys.executable} -c "raise ValueError()"',
    ]
    assert loop.main(args) == 1
    calls = [json.loads(line) for line in (tmp_path / "gh-calls.jsonl").read_text().splitlines()]
    comments = [entry["body"] for entry in calls if "body" in entry]
    assert len(comments) == 2 * max_fixes + 1
    assert "修正上限に到達" in comments[-1]
    assert "失敗:" in comments[-1]


@pytest.mark.parametrize("number", ["0", "-1"])
def test_rejects_invalid_pr_number(
    repo: Path, fake_codex: Path, fake_gh: Path, tmp_path: Path, number: str
) -> None:
    args = arguments(repo, fake_codex) + ["--pr", number, "--gh-bin", str(fake_gh)]
    assert loop.main(args) == 2
    assert not (tmp_path / "gh-calls.jsonl").exists()
    assert not (tmp_path / "calls.jsonl").exists()


@pytest.mark.parametrize(
    ("scenario", "comments"), [("malformed", 0), ("review_writes", 0), ("commit", 1)]
)
def test_invalid_model_results_are_not_commented(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    comments: int,
) -> None:
    monkeypatch.setenv("FAKE_SCENARIO", scenario)
    args = arguments(repo, fake_codex) + ["--pr", "123", "--gh-bin", str(fake_gh)]
    assert loop.main(args) == 2
    calls = [json.loads(line) for line in (tmp_path / "gh-calls.jsonl").read_text().splitlines()]
    assert sum("body" in entry for entry in calls) == comments

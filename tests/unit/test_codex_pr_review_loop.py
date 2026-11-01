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
    assert args[2] in ("123", "https://github.com/example/repo/pull/123")
    if args[4] == "headRepository,headRepositoryOwner":
        print(json.dumps({"headRepository": {"name": "repo"}, "headRepositoryOwner": {"login": "example"}}))
        sys.exit(0)
    assert args[3:] == ["--json", "url,state,headRefName,headRefOid"]
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
    elif scenario == "moved_after_review" and any("body" in entry for entry in previous):
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


@pytest.fixture
def push_remote(repo: Path, tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    loop.git(remote, "init", "--bare")
    transport = tmp_path / "local-ssh"
    transport.write_text(
        f"#!{sys.executable}\n"
        "import os, shlex, sys\n"
        "args = shlex.split(sys.argv[-1])\n"
        "command = args[0]\n"
        "assert command in ('git-upload-pack', 'git-receive-pack')\n"
        "assert args[1].lstrip('/') == 'example/repo.git'\n"
        f"os.execvp(command, [command, {str(remote)!r}])\n"
    )
    transport.chmod(0o755)
    loop.git(repo, "config", "core.sshCommand", str(transport))
    loop.git(repo, "config", "ssh.variant", "ssh")
    loop.git(repo, "remote", "add", "origin", "git@github.com:example/repo.git")
    loop.git(repo, "push", "-u", "origin", "HEAD")
    return remote


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
    push_remote: Path,
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
    head = loop.git(repo, "rev-parse", "HEAD")
    assert loop.git(push_remote, "rev-parse", "refs/heads/feat/123-test") == head
    final = [entry["body"] for entry in calls if "body" in entry][-1]
    assert "修正をcommit・pushしました" in final
    assert head.decode().strip() in final
    assert not loop.git(repo, "status", "--porcelain")


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


def publication_arguments(repo: Path, fake_codex: Path, fake_gh: Path) -> list[str]:
    return arguments(repo, fake_codex) + [
        "--pr",
        "123",
        "--gh-bin",
        str(fake_gh),
        "--publish-fixes",
    ]


@pytest.mark.parametrize("scenario", ["human", "fix_human", "no_change", "oscillate"])
def test_unsuccessful_repairs_are_never_committed_or_pushed(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    push_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    original = loop.git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("FAKE_SCENARIO", scenario)
    assert loop.main(publication_arguments(repo, fake_codex, fake_gh)) == 1
    assert loop.git(repo, "rev-parse", "HEAD") == original
    assert loop.git(push_remote, "rev-parse", "refs/heads/feat/123-test") == original


def test_failed_checks_prevent_publication(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    push_remote: Path,
) -> None:
    original = loop.git(repo, "rev-parse", "HEAD")
    args = publication_arguments(repo, fake_codex, fake_gh) + [
        "--max-fixes",
        "1",
        "--check",
        f'{sys.executable} -c "raise ValueError()"',
    ]
    assert loop.main(args) == 1
    assert (repo / "new.py").exists()
    assert loop.git(repo, "rev-parse", "HEAD") == original
    assert loop.git(push_remote, "rev-parse", "refs/heads/feat/123-test") == original


def test_success_without_repairs_does_not_create_a_commit(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    push_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = loop.git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("FAKE_SCENARIO", "clean")
    args = arguments(repo, fake_codex)[:-2] + [
        "--check",
        f'{sys.executable} -c "pass"',
        "--pr",
        "123",
        "--gh-bin",
        str(fake_gh),
        "--publish-fixes",
    ]
    assert loop.main(args) == 0
    assert loop.git(repo, "rev-parse", "HEAD") == original
    assert loop.git(push_remote, "rev-parse", "refs/heads/feat/123-test") == original
    assert not loop.git(repo, "status", "--porcelain")


@pytest.mark.parametrize("dry_run", [False, True])
def test_publication_requires_pr_and_dry_run_has_no_side_effects(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    tmp_path: Path,
    dry_run: bool,
) -> None:
    original = loop.git(repo, "rev-parse", "HEAD")
    args = arguments(repo, fake_codex) + ["--publish-fixes", "--gh-bin", str(fake_gh)]
    if dry_run:
        args += ["--pr", "123", "--dry-run"]
    assert loop.main(args) == (0 if dry_run else 2)
    assert not (tmp_path / "gh-calls.jsonl").exists()
    assert not (tmp_path / "calls.jsonl").exists()
    assert loop.git(repo, "rev-parse", "HEAD") == original
    assert not loop.git(repo, "status", "--porcelain")


@pytest.mark.parametrize(
    "url",
    [
        "git@other.example:example/repo.git",
        "https://github.com/example/other.git",
        "ssh://git@github.com/other/repo.git",
        "/tmp/local.git",
    ],
)
def test_publish_remote_must_match_pr_head_repository(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    push_remote: Path,
    tmp_path: Path,
    url: str,
) -> None:
    loop.git(repo, "remote", "set-url", "origin", url)
    assert loop.main(publication_arguments(repo, fake_codex, fake_gh)) == 2
    assert not (tmp_path / "calls.jsonl").exists()
    assert not loop.git(repo, "status", "--porcelain")


def test_pr_head_change_during_repairs_prevents_commit_and_push(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    push_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = loop.git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("FAKE_PR_SCENARIO", "moved_after_review")
    assert loop.main(publication_arguments(repo, fake_codex, fake_gh)) == 2
    assert (repo / "new.py").exists()
    assert loop.git(repo, "rev-parse", "HEAD") == original
    assert loop.git(push_remote, "rev-parse", "refs/heads/feat/123-test") == original


@pytest.mark.parametrize("failure", ["commit", "push", "hook_changes_files"])
def test_publication_failure_preserves_repairs_and_records_state(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    push_remote: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    original = loop.git(repo, "rev-parse", "HEAD")
    if failure == "push":
        hook = push_remote / "hooks" / "pre-receive"
    else:
        hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\nprintf 'unreviewed\\n' > app.py\ngit add app.py\n"
        if failure == "hook_changes_files"
        else "#!/bin/sh\nexit 1\n"
    )
    hook.chmod(0o755)
    assert loop.main(publication_arguments(repo, fake_codex, fake_gh)) == 2
    output = capsys.readouterr()
    directory = Path(output.out.split("ログ: ", 1)[1].splitlines()[0])
    state = json.loads((directory / "publication" / "result.json").read_text())
    assert state["status"] == ("preparing" if failure == "commit" else "committed")
    assert json.loads((directory / "result.json").read_text())["status"] == "error"
    assert (repo / "new.py").read_text() == "ok"
    assert loop.git(push_remote, "rev-parse", "refs/heads/feat/123-test") == original
    assert (loop.git(repo, "rev-parse", "HEAD") == original) == (failure == "commit")
    calls = [json.loads(line) for line in (tmp_path / "gh-calls.jsonl").read_text().splitlines()]
    assert sum("body" in entry for entry in calls) == 2


def test_files_changed_after_review_are_not_committed(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    push_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = loop.git(repo, "rev-parse", "HEAD")
    resolve = loop.resolve_push_remote
    invocations = 0

    def change_after_review(repo: Path, comments: loop.PRComments) -> str:
        nonlocal invocations
        invocations += 1
        if invocations == 2:
            (repo / "app.py").write_text("unreviewed")
        return resolve(repo, comments)

    monkeypatch.setattr(loop, "resolve_push_remote", change_after_review)
    assert loop.main(publication_arguments(repo, fake_codex, fake_gh)) == 2
    assert loop.git(repo, "rev-parse", "HEAD") == original
    assert loop.git(push_remote, "rev-parse", "refs/heads/feat/123-test") == original
    assert (repo / "app.py").read_text() == "unreviewed"


@pytest.mark.parametrize("when", ["after_review", "after_commit"])
def test_remote_head_change_is_preserved(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    push_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    original = loop.git(repo, "rev-parse", "HEAD")
    resolve = loop.resolve_push_remote
    invocations = 0
    concurrent = (
        loop.git(
            push_remote,
            "-c",
            "user.name=Other",
            "-c",
            "user.email=other@example.invalid",
            "commit-tree",
            f"{original.decode().strip()}^{{tree}}",
            "-p",
            original.decode().strip(),
            "-m",
            "concurrent",
        )
        .decode()
        .strip()
    )

    def advance_remote(repo: Path, comments: loop.PRComments) -> str:
        nonlocal invocations
        invocations += 1
        if invocations == (2 if when == "after_review" else 3):
            loop.git(push_remote, "update-ref", "refs/heads/feat/123-test", concurrent)
        return resolve(repo, comments)

    monkeypatch.setattr(loop, "resolve_push_remote", advance_remote)
    assert loop.main(publication_arguments(repo, fake_codex, fake_gh)) == 2
    assert (
        loop.git(push_remote, "rev-parse", "refs/heads/feat/123-test").decode().strip()
        == concurrent
    )
    assert (loop.git(repo, "rev-parse", "HEAD") == original) == (when == "after_review")
    assert (repo / "new.py").read_text() == "ok"


@pytest.mark.parametrize("change", ["tracked", "deleted", "executable_symlink"])
def test_published_tree_contains_all_verified_file_changes(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    push_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    call = loop.codex_call

    def edit_files(
        repo: Path,
        directory: Path,
        settings: loop.Settings,
        executable: str,
        prompt: str,
        review: bool,
    ) -> dict[str, object]:
        answer = call(repo, directory, settings, executable, prompt, review)
        if not review:
            if change == "tracked":
                (repo / "app.py").write_text("repaired\n")
            elif change == "deleted":
                (repo / "app.py").unlink()
            else:
                (repo / "new.py").chmod(0o755)
                (repo / "alias.py").symlink_to("new.py")
        return answer

    monkeypatch.setattr(loop, "codex_call", edit_files)
    assert loop.main(publication_arguments(repo, fake_codex, fake_gh)) == 0
    ref = "refs/heads/feat/123-test"
    assert loop.git(push_remote, "show", f"{ref}:new.py") == b"ok"
    if change == "tracked":
        assert loop.git(push_remote, "show", f"{ref}:app.py") == b"repaired\n"
    elif change == "deleted":
        assert b"app.py" not in loop.git(push_remote, "ls-tree", "--name-only", ref)
    else:
        assert b"100755" in loop.git(push_remote, "ls-tree", ref, "new.py")
        assert b"120000" in loop.git(push_remote, "ls-tree", ref, "alias.py")
    assert not loop.git(repo, "status", "--porcelain")


def test_final_comment_failure_keeps_pushed_commit_and_publication_log(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    push_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FAKE_GH_FAIL_AT", "3")
    original = loop.git(repo, "rev-parse", "HEAD")
    assert loop.main(publication_arguments(repo, fake_codex, fake_gh)) == 2
    output = capsys.readouterr()
    directory = Path(output.out.split("ログ: ", 1)[1].splitlines()[0])
    head = loop.git(repo, "rev-parse", "HEAD")
    assert head != original
    assert loop.git(push_remote, "rev-parse", "refs/heads/feat/123-test") == head
    state = json.loads((directory / "publication" / "result.json").read_text())
    assert state["status"] == "pushed" and state["head"] == head.decode().strip()
    assert not loop.git(repo, "status", "--porcelain")


def test_publication_uses_push_url_when_fetch_url_points_elsewhere(
    repo: Path,
    fake_codex: Path,
    fake_gh: Path,
    push_remote: Path,
) -> None:
    loop.git(repo, "remote", "set-url", "origin", "git@github.com:example/base.git")
    loop.git(repo, "remote", "set-url", "--push", "origin", "git@github.com:example/repo.git")
    assert loop.main(publication_arguments(repo, fake_codex, fake_gh)) == 0
    assert loop.git(push_remote, "rev-parse", "refs/heads/feat/123-test") == loop.git(
        repo, "rev-parse", "HEAD"
    )

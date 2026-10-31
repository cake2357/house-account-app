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
answer = {"needs_human": False, "reason": "", "summary": "result"}
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

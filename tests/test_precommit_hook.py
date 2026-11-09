"""The commit-test hook runs pytest only for commands that run `git commit`."""
import json

import pytest

from scripts import precommit_tests as hook


@pytest.mark.parametrize("cmd", [
    "git commit -F msg.txt",
    "git add -A && git commit -m 'x'",
    "git -C C:/repo commit -m x",
    "git --no-pager commit --amend",
    "cd repo; git commit -q",
])
def test_commit_commands_are_seen(cmd):
    assert hook.is_commit(cmd)


@pytest.mark.parametrize("cmd", [
    "git status",
    "git log --oneline -5",
    "Get-Content logs\\deploy.log -Tail 3; Get-Date",
    "Get-CimInstance Win32_Process | Select ProcessId,CommandLine | ft",
    "for f in a b; do sed -n '1,5p' \"$f\"; done",
    "git show 7995414 --stat",
    "echo committed",
])
def test_other_commands_are_not(cmd):
    assert not hook.is_commit(cmd)


def _stdin(cmd):
    return json.dumps({"tool_name": "PowerShell", "tool_input": {"command": cmd}})


def test_non_commit_skips_the_tests(monkeypatch):
    monkeypatch.setattr(hook, "run_tests", lambda: pytest.fail("tests should not run"))
    assert hook.main(_stdin("Get-Content logs\\bot.log -Tail 5")) == 0


def test_commit_runs_the_tests(monkeypatch):
    monkeypatch.setattr(hook, "run_tests", lambda: 2)
    assert hook.main(_stdin("git commit -F msg.txt")) == 2


def test_unreadable_input_still_runs_the_tests(monkeypatch):
    monkeypatch.setattr(hook, "run_tests", lambda: 0)
    assert hook.main("not json") == 0
    monkeypatch.setattr(hook, "run_tests", lambda: 2)
    assert hook.main("") == 2

"""Change 3: a fresh-process reviewer after each justloop step."""

from __future__ import annotations

import subprocess
import sys

import pytest

import pralph.loop as loop
import pralph.runner as runner
from pralph import justloop_review as jr
from pralph import justloop_steps as js
from pralph.models import IterationResult, PhaseState
from pralph.runner import ClaudeResult


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(jsonl_state):
    d = jsonl_state.project_dir
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "t")
    (d / "loop-plan.md").write_text("## Steps\n\n- [ ] one\n- [ ] two\n\n## Progress notes\n")
    (d / ".gitignore").write_text(".pralph/\nreview-count\n")
    _git(d, "add", ".")
    _git(d, "commit", "-qm", "init")
    return d


def _commit(repo, name):
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD")


def _run(state, monkeypatch, steps, **kwargs):
    """Scripted loop: each entry is (step line, file to commit or None, status)."""
    calls = []

    def fake_run(prompt, **kw):
        calls.append(prompt)
        step, name, status = steps[len(calls) - 1]
        sha = _commit(state.project_dir, name) if name else ""
        structured = {"step": step, "status": status, "commit": sha, "note": f"did {name}",
                      "blocker": "", "needs_human": []}
        return ClaudeResult(success=True, structured_output=structured, cost_usd=0.01)

    monkeypatch.setattr(loop, "run_with_retry", fake_run)
    ps = loop.run_justloop(state, user_prompt="do it", max_iterations=len(steps) + 2, cooldown=0, **kwargs)
    return ps, calls


# A review command that reports findings the first time and passes the second
_FLAKY_REVIEW = (
    f"{sys.executable} -c \"import os,sys;p='review-count';n=int(open(p).read()) if os.path.exists(p) else 0;"
    "open(p,'w').write(str(n+1));"
    "print('1. tests not wired into CI') if n==0 else None;"
    "print(os.environ['PRALPH_BASE'][:7], os.environ['PRALPH_COMMIT'][:7], file=sys.stderr);"
    "sys.exit(1 if n==0 else 0)\""
)


class TestReviewCommandLoop:
    def test_findings_block_tick_until_clean(self, jsonl_state, repo, monkeypatch, capsys):
        steps = [("- [ ] one", "a.py", "done"), ("- [ ] one", "a_fix.py", "done"),
                 ("- [ ] two", "b.py", "done")]
        ps, calls = _run(jsonl_state, monkeypatch, steps, plan_file="loop-plan.md",
                         review_cmd=_FLAKY_REVIEW)
        assert len(calls) == 3
        assert ps.completion_reason == "loop_complete"
        # The second prompt opens its step context with the findings
        assert "Fix these review findings for step 1, then commit:\n1. tests not wired into CI" in calls[1]
        assert "(review fixes)" in capsys.readouterr().out
        plan = (repo / "loop-plan.md").read_text()
        assert "- [x] one" in plan and "- [x] two" in plan
        assert "Step 1 (review findings): did a.py" in plan
        assert ps.steps["#1 one"].review_findings == ""

    def test_step_not_ticked_after_findings(self, jsonl_state, repo, monkeypatch):
        steps = [("- [ ] one", "a.py", "done")]
        ps, calls = _run(jsonl_state, monkeypatch, steps, plan_file="loop-plan.md",
                         review_cmd="echo '1. missing test'; exit 1", max_step_attempts=1)
        assert "- [ ] one" in (repo / "loop-plan.md").read_text()
        assert ps.completion_reason == "review_not_passing"
        assert "step 1 still has review findings after 1 attempts: 1. missing test" in ps.last_error

    def test_review_never_passing_stops_at_cap(self, jsonl_state, repo, monkeypatch):
        steps = [("- [ ] one", f"a{i}.py", "done") for i in range(3)]
        ps, calls = _run(jsonl_state, monkeypatch, steps, plan_file="loop-plan.md",
                         review_cmd="echo nope; exit 1", max_step_attempts=3)
        assert len(calls) == 3
        assert ps.completion_reason == "review_not_passing"

    def test_review_covers_step_and_fixes(self, jsonl_state, repo, monkeypatch):
        base = _git(repo, "rev-parse", "HEAD")
        seen = []
        real = jr.run_review_cmd

        def spy(cmd, project_dir, **kw):
            seen.append((kw["base"], kw["commit"], kw["step"], kw["step_number"]))
            return real(cmd, project_dir, **kw)

        monkeypatch.setattr(jr, "run_review_cmd", spy)
        steps = [("- [ ] one", "a.py", "done"), ("- [ ] one", "a_fix.py", "done"),
                 ("- [ ] two", "b.py", "done")]
        _run(jsonl_state, monkeypatch, steps, plan_file="loop-plan.md", review_cmd=_FLAKY_REVIEW)
        assert seen[0][0] == base and seen[1][0] == base  # fix reviewed together with the step
        assert seen[2][0] == seen[1][1]                    # next step starts at the clean commit
        assert seen[0][2:] == ("- [ ] one", 1)

    def test_crashing_review_command_stops_with_review_error(self, jsonl_state, repo, monkeypatch):
        steps = [("- [ ] one", "a.py", "done")]
        ps, calls = _run(jsonl_state, monkeypatch, steps, plan_file="loop-plan.md",
                         review_cmd="definitely-not-a-command-xyz")
        assert len(calls) == 1
        assert ps.completion_reason == "review_error"
        assert "exited 127" in ps.last_error
        assert "- [ ] one" in (repo / "loop-plan.md").read_text()  # never auto-approved

    @pytest.mark.parametrize("code", [2, 3])
    def test_other_exit_codes_are_errors(self, jsonl_state, repo, monkeypatch, code):
        steps = [("- [ ] one", "a.py", "done")]
        ps, calls = _run(jsonl_state, monkeypatch, steps, plan_file="loop-plan.md",
                         review_cmd=f"echo 'round limit reached' >&2; exit {code}")
        assert len(calls) == 1
        assert ps.completion_reason == "review_error"
        assert f"exited {code}: round limit reached" in ps.last_error
        assert "- [ ] one" in (repo / "loop-plan.md").read_text()
        checklist = (repo / ".pralph" / "needs-human.md").read_text()
        assert "Review of this step did not complete" in checklist and "(step 1)" in checklist

    def test_findings_on_stderr_when_stdout_empty(self, repo):
        out = jr.run_review_cmd("echo 'fix x' >&2; exit 1", str(repo), step="s", step_number=1, commit="c", base="b")
        assert out.verdict == "findings" and out.findings == "fix x"

    def test_review_timeout_is_an_error(self, repo):
        out = jr.run_review_cmd("sleep 30", str(repo), step="s", step_number=1, commit="c", base="b", timeout=1)
        assert out.verdict == "error" and "timed out" in out.findings

    def test_signal_is_an_error(self, repo):
        out = jr.run_review_cmd("kill -9 $$", str(repo), step="s", step_number=1, commit="c", base="b")
        assert out.verdict == "error"

    def test_findings_capped(self, repo):
        out = jr.run_review_cmd(f"{sys.executable} -c \"print('x'*9000)\"; exit 1", str(repo),
                                step="s", step_number=1, commit="c", base="b")
        assert out.verdict == "findings"
        assert len(out.findings) <= jr.MAX_FINDINGS_CHARS
        assert out.findings.endswith("[... findings truncated by pralph]")

    def test_no_new_commit_skips_review(self, jsonl_state, repo, monkeypatch):
        steps = [("- [ ] one", None, "done"), ("- [ ] two", "b.py", "done")]
        ps, calls = _run(jsonl_state, monkeypatch, steps, plan_file="loop-plan.md",
                         review_cmd="echo bad; exit 1", max_step_attempts=1)
        plan = (repo / "loop-plan.md").read_text()
        assert "- [x] one" in plan and "- [ ] two" in plan
        assert ps.completion_reason == "review_not_passing"

    def test_review_every_defers_and_last_step_always_reviewed(self, jsonl_state, repo, monkeypatch):
        (repo / "loop-plan.md").write_text("- [ ] one\n- [ ] two\n- [ ] three\n")
        seen = []
        monkeypatch.setattr(jr, "run_review_cmd",
                            lambda cmd, d, **kw: seen.append(kw["step_number"]) or jr.ReviewOutcome("clean"))
        steps = [("- [ ] one", "a.py", "done"), ("- [ ] two", "b.py", "done"), ("- [ ] three", "c.py", "done")]
        ps, _ = _run(jsonl_state, monkeypatch, steps, plan_file="loop-plan.md",
                     review_cmd="true", review_every=5)
        assert seen == [3]
        assert ps.completion_reason == "loop_complete"

    def test_plain_mode_findings_reach_next_prompt(self, jsonl_state, repo, monkeypatch):
        steps = [("", "a.py", "done"), ("", "a_fix.py", "done"), ("", None, "nothing_left")]
        ps, calls = _run(jsonl_state, monkeypatch, steps, review_cmd=_FLAKY_REVIEW)
        assert "Fix these review findings for the last unit of work, then commit:" in calls[1]
        assert ps.steps[js.UNNAMED_STEP].review_findings == ""
        assert ps.completion_reason == "loop_complete"

    def test_without_review_nothing_changes(self, jsonl_state, repo, monkeypatch):
        steps = [("- [ ] one", "a.py", "done"), ("- [ ] two", "b.py", "done")]
        ps, calls = _run(jsonl_state, monkeypatch, steps, plan_file="loop-plan.md")
        assert ps.completion_reason == "loop_complete"


class TestFreshReview:
    def _fake(self, monkeypatch, outputs, seen):
        def fake(prompt, **kw):
            seen.append((prompt, kw))
            out = outputs[len(seen) - 1]
            if isinstance(out, ClaudeResult):
                return out
            return ClaudeResult(success=True, structured_output=out, cost_usd=0.02)
        monkeypatch.setattr(runner, "run_with_retry", fake)

    def test_findings_then_clean(self, jsonl_state, repo, monkeypatch):
        seen = []
        self._fake(monkeypatch, [{"clean": False, "findings": "1. a.py: off by one"},
                                 {"clean": True, "findings": ""},
                                 {"clean": True, "findings": ""}], seen)
        steps = [("- [ ] one", "a.py", "done"), ("- [ ] one", "a_fix.py", "done"), ("- [ ] two", "b.py", "done")]
        ps, calls = _run(jsonl_state, monkeypatch, steps, plan_file="loop-plan.md", review="fresh")
        assert ps.completion_reason == "loop_complete"
        assert "1. a.py: off by one" in calls[1]
        prompt, kw = seen[0]
        assert "+a.py" in prompt                       # the step's diff is in the prompt
        assert kw["allowed_tools"] == jr.FRESH_REVIEW_TOOLS
        assert "Write" not in kw["allowed_tools"] and "Edit" not in kw["allowed_tools"]
        assert "json_schema" in kw and "session_id" not in kw and "resume_session_id" not in kw
        assert "dangerously_skip_permissions" not in kw
        assert "a_fix.py" in seen[1][0]                # re-review covers the fix
        # review cost is added to the iteration
        assert ps.total_cost_usd == pytest.approx(3 * 0.01 + 3 * 0.02)

    def test_failed_review_session_is_review_error(self, jsonl_state, repo, monkeypatch):
        seen = []
        self._fake(monkeypatch, [ClaudeResult(success=False, error="overloaded")], seen)
        ps, _ = _run(jsonl_state, monkeypatch, [("- [ ] one", "a.py", "done")],
                     plan_file="loop-plan.md", review="fresh")
        assert ps.completion_reason == "review_error"
        assert "- [ ] one" in (repo / "loop-plan.md").read_text()

    def test_missing_verdict_is_review_error(self, jsonl_state, repo, monkeypatch):
        seen = []
        self._fake(monkeypatch, [ClaudeResult(success=True, structured_output=None)], seen)
        ps, _ = _run(jsonl_state, monkeypatch, [("- [ ] one", "a.py", "done")],
                     plan_file="loop-plan.md", review="fresh")
        assert ps.completion_reason == "review_error"


class TestReviewerOutsideGit:
    def test_not_a_repo_is_review_error(self, jsonl_state, monkeypatch):
        (jsonl_state.project_dir / "loop-plan.md").write_text("- [ ] one\n")
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(jsonl_state.project_dir.parent))
        ps, _ = _run(jsonl_state, monkeypatch, [("- [ ] one", None, "done")],
                     plan_file="loop-plan.md", review_cmd="true")
        assert ps.completion_reason == "review_error"


class TestStepRecords:
    def test_review_findings_saved_and_cleared(self):
        ps = PhaseState(phase="justloop")
        ir = IterationResult(iteration=1, phase="justloop", mode="execute", success=True,
                             status="in_progress", step="- [ ] one", step_number=1,
                             review="findings", review_findings="fix it")
        js.record_step_result(ps, ir, 3)
        assert ps.steps["#1 one"].review_findings == "fix it"
        ctx, header = js.build_step_context(ps, 3, "- [ ] one\n")
        assert ctx.startswith("Fix these review findings for step 1, then commit:\nfix it")
        assert "continue it" not in ctx
        ir2 = IterationResult(iteration=2, phase="justloop", mode="execute", success=True,
                              status="done", step="- [ ] one", step_number=1, review="clean")
        js.record_step_result(ps, ir2, 3)
        assert ps.steps["#1 one"].review_findings == ""

    @pytest.mark.parametrize("backend", ["jsonl_state", "duckdb_state"])
    def test_persisted_in_both_backends(self, backend, request):
        state = request.getfixturevalue(backend)
        if True:
            ps = PhaseState(phase="justloop")
            ir = IterationResult(iteration=1, phase="justloop", mode="execute", success=True,
                                 status="in_progress", step="- [ ] one", step_number=1,
                                 review="findings", review_findings="fix it")
            js.record_step_result(ps, ir, 3)
            state.save_phase_state(ps)
            assert state.load_phase_state("justloop").steps["#1 one"].review_findings == "fix it"
            state.log_iteration(ir)
            row = state.load_run_log()[-1]
            assert row["review"] == "findings" and row["review_findings"] == "fix it"

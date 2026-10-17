"""query --loop-state (versioned export of the last justloop run) and --post-run-cmd."""

from __future__ import annotations

import json
import sys

import pytest
from click.testing import CliRunner

import pralph.loop as loop
from pralph.cli import main
from pralph.loop_state import LOOP_STATE_VERSION, build_loop_state
from pralph.runner import ClaudeResult


def _cr(step, status, commit="", blocker="", needs=(), denied=()):
    structured = {"step": step, "status": status, "commit": commit, "note": "n",
                  "blocker": blocker, "needs_human": list(needs)}
    return ClaudeResult(success=True, structured_output=structured, cost_usd=0.01,
                        permission_denials=list(denied))


def _run(state, monkeypatch, results, **kwargs):
    calls = []

    def fake_run(prompt, **kw):
        calls.append(prompt)
        return results[len(calls) - 1]

    monkeypatch.setattr(loop, "run_with_retry", fake_run)
    return loop.run_justloop(state, user_prompt="do it", max_iterations=len(results), cooldown=0, **kwargs)


def _query(project_dir, *extra):
    r = CliRunner().invoke(main, ["--project-dir", str(project_dir), "query", "--loop-state", *extra])
    return r


@pytest.fixture(params=["jsonl_state", "duckdb_state"])
def state(request):
    return request.getfixturevalue(request.param)


class TestExport:
    def test_no_project(self, tmp_path):
        r = _query(tmp_path, "--format", "json")
        assert r.exit_code == 0
        assert json.loads(r.output) == {"version": 1, "project_id": None, "phase_state": None, "steps": []}

    def test_project_without_a_run(self, state):
        out = build_loop_state(state)
        assert out["version"] == LOOP_STATE_VERSION
        assert out["project_id"] == state.project_id
        assert out["phase_state"] is None and out["steps"] == []

    def test_json_only(self, jsonl_state):
        r = _query(jsonl_state.project_dir)
        assert r.exit_code == 2 and "--format json" in r.output

    def test_blocked_run(self, state, monkeypatch):
        d = state.project_dir
        (d / "loop-plan.md").write_text("- [ ] one\n- [ ] two\n- [ ] two\n")
        denial = {"tool": "Write", "tool_input": {"file_path": str(d / ".claude/settings.json")}, "reason": ""}
        _run(state, monkeypatch, [
            _cr("- [ ] one", "done", commit="aaa1111"),
            _cr("- [ ] two", "in_progress", commit="bbb2222", denied=[denial]),
            _cr("- [ ] two", "blocked", blocker="timeout after 31s"),
            _cr("- [ ] two", "blocked", blocker="timeout after 29s"),
        ], plan_file="loop-plan.md")
        out = build_loop_state(state)
        ps = out["phase_state"]
        assert len(ps["run_id"]) == 36 and ps["started_at"]
        assert ps["completed"] is True and ps["completion_reason"] == "repeated_blocker"
        assert ps["last_error"].startswith("step 2 hit the same blocker twice")
        assert ps["last_step"] == "#2 two" and ps["current_iteration"] == 4
        assert ps["total_cost_usd"] == pytest.approx(0.04)
        assert [(i["tool"], i["target"], i["step_number"]) for i in ps["needs_human"]] == [
            ("Write", ".claude/settings.json", 2)]
        assert out["steps"] == [
            {"key": "#1 one", "step_number": 1, "text": "one", "attempts": 1, "last_status": "done",
             "last_blocker": "", "review_findings": "", "commit": "aaa1111"},
            {"key": "#2 two", "step_number": 2, "text": "two", "attempts": 3, "last_status": "blocked",
             "last_blocker": "timeout after 29s", "review_findings": "", "commit": "bbb2222"},
        ]
        json.dumps(out)  # serialisable as is

    def test_cli_prints_the_same(self, jsonl_state, monkeypatch):
        _run(jsonl_state, monkeypatch, [_cr("", "nothing_left")])
        r = _query(jsonl_state.project_dir, "--format", "json")
        assert r.exit_code == 0
        assert json.loads(r.output) == build_loop_state(jsonl_state)

    def test_commits_come_from_this_run_only(self, state, monkeypatch):
        (state.project_dir / "p.md").write_text("- [ ] one\n")
        _run(state, monkeypatch, [_cr("- [ ] one", "in_progress", commit="old1111")], plan_file="p.md")
        first = build_loop_state(state)["phase_state"]["run_id"]
        # A new run resets the phase (as the CLI does) and reports no commit
        loop_ps = state.load_phase_state("justloop")
        state.save_phase_state(type(loop_ps)(phase="justloop"))
        _run(state, monkeypatch, [_cr("- [ ] one", "in_progress")], plan_file="p.md")
        out = build_loop_state(state)
        assert out["phase_state"]["run_id"] != first
        assert out["steps"][0]["commit"] == ""
        rows = [r for r in state.load_run_log() if r.get("phase") == "justloop"]
        assert [r["run_id"] for r in rows] == [first, out["phase_state"]["run_id"]]

    def test_unnamed_step_with_review_findings(self, jsonl_state, monkeypatch):
        import pralph.justloop_review as jr
        # HEAD at the start is the review base; the reported commit is new
        monkeypatch.setattr(jr, "resolve_commit", lambda d, ref: "b" * 40 if ref == "HEAD" else "c" * 40)
        monkeypatch.setattr(jr, "run_review_cmd", lambda *a, **k: jr.ReviewOutcome("findings", "fix x"))
        _run(jsonl_state, monkeypatch, [_cr("", "done", commit="ccc3333")], review_cmd="true")
        [step] = build_loop_state(jsonl_state)["steps"]
        assert step["key"] == "the last unit of work"
        assert step["review_findings"] == "fix x" and step["commit"] == "ccc3333"


class TestPostRunCmd:
    def test_runs_once_with_env(self, jsonl_state, monkeypatch, tmp_path):
        out = tmp_path / "post.json"
        cmd = (f"{sys.executable} -c \"import json,os;"
               f"open({str(out)!r},'a').write(json.dumps({{k: os.environ[k] for k in "
               f"('PRALPH_RUN_ID','PRALPH_COMPLETION_REASON','PRALPH_PROJECT_DIR')}})+'\\n')\"")
        ps = _run(jsonl_state, monkeypatch, [_cr("", "in_progress"), _cr("", "nothing_left")],
                  post_run_cmd=cmd)
        [line] = out.read_text().splitlines()
        env = json.loads(line)
        assert env["PRALPH_RUN_ID"] == ps.run_id
        assert env["PRALPH_COMPLETION_REASON"] == "loop_complete"
        assert env["PRALPH_PROJECT_DIR"] == str(jsonl_state.project_dir)

    def test_failure_does_not_change_the_outcome(self, jsonl_state, monkeypatch, capsys):
        ps = _run(jsonl_state, monkeypatch, [_cr("", "nothing_left")], post_run_cmd="echo nope >&2; exit 3")
        assert ps.completion_reason == "loop_complete"
        assert "post-run command exited 3: nope" in capsys.readouterr().out

    def test_export_inside_post_run_cmd_sees_the_finished_run(self, jsonl_state, monkeypatch, tmp_path):
        from pathlib import Path
        monkeypatch.setenv("PYTHONPATH", str(Path(loop.__file__).resolve().parent.parent))
        out = tmp_path / "state.json"
        cmd = (f"{sys.executable} -c \"from pralph.cli import main;main()\" "
               f"--project-dir \"$PRALPH_PROJECT_DIR\" query --loop-state --format json > {out}")
        ps = _run(jsonl_state, monkeypatch, [_cr("", "nothing_left")], post_run_cmd=cmd)
        exported = json.loads(out.read_text())
        assert exported["phase_state"]["run_id"] == ps.run_id
        assert exported["phase_state"]["completed"] is True

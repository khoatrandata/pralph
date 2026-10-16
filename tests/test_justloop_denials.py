"""Change 4: permission denials and needs_human as typed hand-off items."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pralph.loop as loop
from pralph import denials
from pralph.models import IterationResult, PhaseState
from pralph.runner import ClaudeResult, _parse_result_event

FIXTURE = Path(__file__).parent / "fixtures" / "permission_denial.ndjson"


def _events():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


# ── stream parsing (recorded claude -p run with a refused Write) ─────


class TestCollector:
    def test_recorded_stream(self):
        c = denials.DenialCollector()
        for e in _events():
            c.feed(e)
        [d] = c.denials()
        assert d["tool"] == "Write"
        assert d["tool_input"]["file_path"] == "/repo/hello.txt"
        assert "haven't granted" in d["reason"]

    def test_result_event_only(self):
        c = denials.DenialCollector()
        c.feed(_events()[-1])
        [d] = c.denials()
        assert d["tool"] == "Write" and d["reason"] == ""

    def test_stream_only_without_result_list(self):
        c = denials.DenialCollector()
        for e in _events()[:2]:
            c.feed(e)
        [d] = c.denials()
        assert d["tool"] == "Write" and d["tool_input"]["file_path"] == "/repo/hello.txt"

    def test_ordinary_tool_error_is_not_a_denial(self):
        c = denials.DenialCollector()
        c.feed({"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "tool_use_id": "t1",
             "content": "File does not exist."}]}})
        assert c.denials() == []

    def test_refusal_text_with_list_content(self):
        c = denials.DenialCollector()
        c.feed({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "rm -rf build/out --force"}}]}})
        c.feed({"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "tool_use_id": "t2",
             "content": [{"type": "text", "text": "This command requires approval"}]}]}})
        [d] = c.denials()
        assert d["tool"] == "Bash"

    def test_run_claude_result_carries_denials(self):
        # _parse_result_event itself does not collect; run_claude attaches the collector's list
        r = _parse_result_event(_events()[-1])
        assert r.permission_denials == []


class TestItems:
    def test_short_target(self):
        assert denials.short_target("Write", {"file_path": "/p/.claude/settings.json"}, "/p") == ".claude/settings.json"
        assert denials.short_target("Write", {"file_path": "/other/x"}, "/p") == "/other/x"
        assert denials.short_target("Bash", {"command": "git push origin main --force"}) == "git push origin"
        assert denials.short_target("mcp__x__y", {"a": 1}) == ""
        assert len(denials.short_target("Bash", {"command": "x" * 300})) == 80

    def test_dedupe_by_tool_and_target(self):
        a = denials.denial_items([{"tool": "Write", "tool_input": {"file_path": "/p/a"}, "reason": "r1"}], "- [ ] one", 1, "/p")
        b = denials.denial_items([{"tool": "Write", "tool_input": {"file_path": "/p/a"}, "reason": "r2"}], "- [ ] two", 2, "/p")
        c = denials.denial_items([{"tool": "Edit", "tool_input": {"file_path": "/p/a"}, "reason": "r3"}], "- [ ] two", 2, "/p")
        merged = denials.merge_items(denials.merge_items(a, b), c)
        assert [(i["tool"], i["target"], i["step_number"]) for i in merged] == [("Write", "a", 1), ("Edit", "a", 2)]

    def test_model_items_relative_paths(self):
        [item] = denials.model_items(["Grant Write for /p/.claude/settings.json"], "", 0, "/p")
        assert item["reason"] == "Grant Write for .claude/settings.json"

    def test_model_items_dedupe_by_reason(self):
        a = denials.model_items(["Grant SSO access to the prod account"], "", 0)
        b = denials.model_items(["grant SSO access to the prod account", "  "], "", 0)
        assert len(denials.merge_items(a, b)) == 1

    def test_render(self):
        items = denials.denial_items(
            [{"tool": "Write", "tool_input": {"file_path": "/p/.claude/settings.json"}, "reason": ""}],
            "- [ ] Add hooks", 3, "/p",
        ) + denials.model_items(["Approve the prod deploy"], "- [ ] Deploy", 7)
        text = denials.render_checklist(items)
        assert text.startswith("# Needs a person\n")
        assert "- [ ] Write .claude/settings.json was refused (step 3): permission denied" in text
        assert "- [ ] Approve the prod deploy (step 7)" in text

    def test_write_and_remove(self, tmp_path):
        path = tmp_path / ".pralph" / "needs-human.md"
        assert denials.write_checklist(path, denials.model_items(["x"], "", 0))
        assert path.exists()
        assert not denials.write_checklist(path, [])
        assert not path.exists()


# ── simulated loop ───────────────────────────────────────────────────


def _cr(step, status="in_progress", denied=(), needs=()):
    structured = {"step": step, "status": status, "commit": "", "note": "n",
                  "blocker": "", "needs_human": list(needs)}
    return ClaudeResult(success=True, structured_output=structured, cost_usd=0.01,
                        permission_denials=list(denied))


def _run(state, monkeypatch, results, **kwargs):
    calls = []

    def fake_run(prompt, **kw):
        calls.append(prompt)
        return results[len(calls) - 1]

    monkeypatch.setattr(loop, "run_with_retry", fake_run)
    ps = loop.run_justloop(state, user_prompt="do it", max_iterations=len(results), cooldown=0, **kwargs)
    return ps, calls


@pytest.fixture
def plan(jsonl_state):
    path = jsonl_state.project_dir / "loop-plan.md"
    path.write_text("- [ ] one\n- [ ] two\n\n## Progress notes\n")
    return path


def _write_denial(project_dir, rel):
    return {"tool": "Write", "tool_input": {"file_path": str(project_dir / rel)},
            "reason": f"Claude requested permissions to write to {project_dir / rel}, but you haven't granted it yet."}


class TestLoop:
    def test_refused_write_ends_with_checklist(self, jsonl_state, plan, monkeypatch, capsys):
        d = jsonl_state.project_dir
        results = [
            _cr("- [ ] one", "done", denied=[_write_denial(d, ".claude/settings.json")]),
            _cr("- [ ] two", "in_progress", denied=[_write_denial(d, ".claude/settings.json")],
                needs=["Grant the deploy role"]),
        ]
        ps, _ = _run(jsonl_state, monkeypatch, results, plan_file="loop-plan.md")
        out = capsys.readouterr().out
        assert "Needs a person (2):" in out
        assert "Write .claude/settings.json was refused (step 1)" in out
        checklist = (d / ".pralph" / "needs-human.md").read_text()
        assert checklist.count("- [ ] Write .claude/settings.json") == 1  # deduplicated across iterations
        assert "write to .claude/settings.json, but" in checklist  # paths in the reason made relative
        assert "- [ ] Grant the deploy role (step 2)" in checklist
        assert "haven't granted" in checklist
        assert len(ps.needs_human) == 2
        assert jsonl_state.load_phase_state("justloop").needs_human == ps.needs_human

    def test_clean_run_removes_stale_checklist(self, jsonl_state, plan, monkeypatch):
        stale = jsonl_state.project_dir / ".pralph" / "needs-human.md"
        stale.parent.mkdir(exist_ok=True)
        stale.write_text("# Needs a person\n- [ ] old\n")
        _run(jsonl_state, monkeypatch, [_cr("- [ ] one", "done"), _cr("- [ ] two", "done")],
             plan_file="loop-plan.md")
        assert not stale.exists()

    def test_stop_on_denial(self, jsonl_state, plan, monkeypatch):
        d = jsonl_state.project_dir
        results = [_cr("- [ ] one", "in_progress", denied=[_write_denial(d, "x.txt")]),
                   _cr("- [ ] one", "done")]
        ps, calls = _run(jsonl_state, monkeypatch, results, plan_file="loop-plan.md", stop_on_denial=True)
        assert len(calls) == 1
        assert ps.completion_reason == "permission_denied"
        assert "Write x.txt was refused (step 1)" in ps.last_error
        assert (d / ".pralph" / "needs-human.md").exists()

    def test_model_needs_human_does_not_trigger_stop_on_denial(self, jsonl_state, plan, monkeypatch):
        results = [_cr("- [ ] one", "done", needs=["Approve deploy"]), _cr("- [ ] two", "done")]
        ps, calls = _run(jsonl_state, monkeypatch, results, plan_file="loop-plan.md", stop_on_denial=True)
        assert len(calls) == 2 and ps.completion_reason == "loop_complete"

    def test_denials_on_failed_iteration_are_kept(self, jsonl_state, plan, monkeypatch):
        d = jsonl_state.project_dir
        failed = ClaudeResult(success=False, error="error_during_execution",
                              permission_denials=[_write_denial(d, "x.txt")])
        ps, _ = _run(jsonl_state, monkeypatch, [failed], plan_file="loop-plan.md")
        assert [i["target"] for i in ps.needs_human] == ["x.txt"]

    def test_run_log_records_items(self, duckdb_state, monkeypatch):
        d = duckdb_state.project_dir
        (d / "loop-plan.md").write_text("- [ ] one\n")
        _run(duckdb_state, monkeypatch, [_cr("- [ ] one", "done", denied=[_write_denial(d, "x.txt")])],
             plan_file="loop-plan.md")
        row = duckdb_state.load_run_log()[-1]
        assert json.loads(row["needs_human"])[0]["target"] == "x.txt"
        assert duckdb_state.load_phase_state("justloop").needs_human[0]["tool"] == "Write"

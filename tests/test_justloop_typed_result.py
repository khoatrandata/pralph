"""Change 1: typed justloop iteration result and the pralph-managed plan file."""

from __future__ import annotations

import json

import pytest

import pralph.loop as loop
from pralph import plan_file as pf
from pralph.models import IterationResult
from pralph.parser import JUSTLOOP_RESULT_SCHEMA, parse_loop_result
from pralph.runner import ClaudeResult, _parse_result_event

# Trimmed from a real `claude -p --output-format stream-json --json-schema ...`
# result event (claude CLI, 2026-10-15). The typed object is in "structured_output";
# "result" carries the same object as a JSON string.
RECORDED_RESULT_EVENT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 2,
    "session_id": "82eed718-554d-437f-a70d-bca7146be8cf",
    "total_cost_usd": 0.0051783,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 25498,
        "cache_read_input_tokens": 0,
        "output_tokens": 157,
    },
    "permission_denials": [],
    "terminal_reason": "completed",
    "result": "{\"status\":\"nothing_left\"}",
    "structured_output": {"status": "nothing_left"},
}

TYPED = {
    "step": "- [ ] Add the parser",
    "status": "done",
    "commit": "abc1234def",
    "note": "Parser added; tests in tests/test_parser.py",
    "blocker": "",
    "needs_human": [],
}


# ── result event parsing ──────────────────────────────────────────────


class TestParseResultEvent:
    def test_recorded_event_with_structured_output(self):
        cr = _parse_result_event(RECORDED_RESULT_EVENT)
        assert cr.success
        assert cr.structured_output == {"status": "nothing_left"}
        assert cr.cost_usd == pytest.approx(0.0051783)

    def test_event_without_structured_output(self):
        event = {k: v for k, v in RECORDED_RESULT_EVENT.items() if k != "structured_output"}
        event["result"] = "Done.\n\n[LOOP_COMPLETE]"
        cr = _parse_result_event(event)
        assert cr.success
        assert cr.structured_output is None
        assert cr.result.endswith("[LOOP_COMPLETE]")

    def test_structured_output_as_json_string(self):
        event = dict(RECORDED_RESULT_EVENT, structured_output=json.dumps(TYPED))
        assert _parse_result_event(event).structured_output == TYPED

    @pytest.mark.parametrize("bad", ["{not json", "[1, 2]", ["a"], 42, None])
    def test_malformed_structured_output_is_none(self, bad):
        event = dict(RECORDED_RESULT_EVENT, structured_output=bad)
        assert _parse_result_event(event).structured_output is None


class TestParseLoopResult:
    def test_full_object(self):
        r = parse_loop_result(TYPED)
        assert r is not None
        assert (r.status, r.step, r.commit) == ("done", "- [ ] Add the parser", "abc1234def")
        assert r.needs_human == []

    @pytest.mark.parametrize("data", [None, {}, {"status": "finished"}, {"status": 3}, "done"])
    def test_invalid_returns_none(self, data):
        assert parse_loop_result(data) is None

    def test_wrong_field_types_are_coerced(self):
        r = parse_loop_result({"status": "blocked", "blocker": 5, "needs_human": ["grant SSO", 3, " "]})
        assert r is not None
        assert r.blocker == ""
        assert r.needs_human == ["grant SSO"]

    def test_schema_statuses_match_parser(self):
        assert set(JUSTLOOP_RESULT_SCHEMA["properties"]["status"]["enum"]) == {
            "done", "in_progress", "blocked", "nothing_left",
        }


# ── plan-file ticker and notes ────────────────────────────────────────

PLAN = """\
# Plan

## Rules

- Commit after each step.

## Steps

- [x] Set up the project
- [ ] Add tests
  - [ ] Add tests
  - [ ] cover the edge cases
- [ ] Add tests for the parser
- [ ] Add tests

## Progress notes

- Project set up.

## Appendix

Nothing here.
"""


class TestTicker:
    def test_ticks_only_the_exact_line(self):
        out, idx = pf.tick_line(PLAN, "- [ ] Add tests for the parser")
        lines = out.splitlines()
        assert lines[idx] == "- [x] Add tests for the parser"
        # Duplicate-looking lines untouched
        assert lines[idx - 3] == "- [ ] Add tests"
        assert "- [ ] Add tests" in lines[idx + 1]
        assert out.count("[x]") == 2

    def test_prefers_exact_indentation_over_text_match(self):
        out, idx = pf.tick_line(PLAN, "  - [ ] Add tests")
        assert out.splitlines()[idx] == "  - [x] Add tests"
        assert "\n- [ ] Add tests\n" in out

    def test_top_level_duplicate_ticks_first_unchecked(self):
        out, idx = pf.tick_line(PLAN, "- [ ] Add tests")
        lines = out.splitlines()
        assert lines[idx] == "- [x] Add tests"
        assert lines[idx + 1] == "  - [ ] Add tests"  # sub-bullet untouched
        assert lines[idx + 4] == "- [ ] Add tests"  # later duplicate untouched

    def test_accepts_step_text_without_checkbox(self):
        out, idx = pf.tick_line(PLAN, "Add tests for the parser")
        assert out.splitlines()[idx] == "- [x] Add tests for the parser"

    def test_missing_line_raises(self):
        with pytest.raises(pf.PlanFileError, match="step not found"):
            pf.tick_line(PLAN, "- [ ] Add tests for the lexer")

    def test_already_ticked_line_raises(self):
        with pytest.raises(pf.PlanFileError):
            pf.tick_line(PLAN, "- [ ] Set up the project")

    def test_empty_step_raises(self):
        with pytest.raises(pf.PlanFileError):
            pf.tick_line(PLAN, "")

    def test_steps_are_top_level_only(self):
        steps = pf.list_steps(PLAN)
        assert [s.text for s in steps] == [
            "Set up the project", "Add tests", "Add tests for the parser", "Add tests",
        ]
        assert pf.next_step(PLAN).number == 2


class TestNotes:
    def test_appends_inside_section_before_next_heading(self):
        out = pf.add_note(PLAN, "Added tests.")
        assert "- Project set up.\n- Added tests.\n\n## Appendix" in out

    def test_appends_at_end_of_last_section(self):
        plan = "## Steps\n\n- [ ] a\n\n## Progress Notes\n\n- one\n"
        assert pf.add_note(plan, "two") == "## Steps\n\n- [ ] a\n\n## Progress Notes\n\n- one\n- two\n"

    def test_empty_section(self):
        plan = "## Steps\n\n- [ ] a\n\n## progress notes\n"
        assert pf.add_note(plan, "first") == "## Steps\n\n- [ ] a\n\n## progress notes\n\n- first\n"

    def test_creates_section_when_missing(self):
        assert pf.add_note("- [ ] a", "n") == "- [ ] a\n\n## Progress notes\n\n- n\n"

    def test_note_is_one_line(self):
        assert pf.add_note("", "a\nb   c").endswith("- a b c\n")


# ── iteration result and completion ───────────────────────────────────


def _cr(structured=None, result="", success=True):
    return ClaudeResult(success=success, result=result, structured_output=structured, cost_usd=0.01)


class TestIterationResult:
    def test_typed_fields_copied(self, tmp_path):
        ir = loop._justloop_iteration_result(1, "execute", _cr(TYPED))
        assert (ir.status, ir.step, ir.commit, ir.blocker) == ("done", TYPED["step"], "abc1234def", "")

    def test_plan_file_ticked_and_noted(self, tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text(PLAN)
        typed = dict(TYPED, step="- [ ] Add tests for the parser")
        ir = loop._justloop_iteration_result(1, "execute", _cr(typed), str(plan))
        content = plan.read_text()
        assert "- [x] Add tests for the parser" in content
        assert "- Step 3: Parser added; tests in tests/test_parser.py\n" in content
        assert ir.success and ir.error == ""

    def test_not_done_appends_note_without_tick(self, tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text(PLAN)
        typed = dict(TYPED, step="- [ ] Add tests for the parser", status="blocked",
                     blocker="permission denied", note="Blocked on write")
        loop._justloop_iteration_result(1, "execute", _cr(typed), str(plan))
        content = plan.read_text()
        assert content.count("[x]") == 1
        assert "- Step 3 (blocked): Blocked on write" in content

    def test_missing_step_is_recorded_error(self, tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text(PLAN)
        typed = dict(TYPED, step="- [ ] Something else")
        ir = loop._justloop_iteration_result(1, "execute", _cr(typed), str(plan))
        assert ir.success
        assert "step not found" in ir.error
        assert plan.read_text().count("[x]") == 1

    def test_no_structured_output_leaves_status_empty(self):
        ir = loop._justloop_iteration_result(1, "execute", _cr(None, result="[LOOP_COMPLETE]"))
        assert ir.success and ir.status == "" and ir.raw_output == "[LOOP_COMPLETE]"

    def test_failure_passthrough(self):
        ir = loop._justloop_iteration_result(1, "execute", _cr(success=False))
        assert not ir.success and ir.status == ""


def _run_justloop(state, monkeypatch, results, **kwargs):
    """Run run_justloop with run_with_retry replaced by scripted ClaudeResults."""
    calls = []

    def fake_run(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return results[len(calls) - 1]

    monkeypatch.setattr(loop, "run_with_retry", fake_run)
    ps = loop.run_justloop(state, user_prompt="do it", max_iterations=len(results) + 2, cooldown=0, **kwargs)
    return ps, calls


class TestRunJustloop:
    def test_nothing_left_ends_loop(self, jsonl_state, monkeypatch):
        results = [_cr(dict(TYPED, step="", status="done")), _cr(dict(TYPED, step="", status="nothing_left"))]
        ps, calls = _run_justloop(jsonl_state, monkeypatch, results)
        assert ps.completion_reason == "loop_complete"
        assert len(calls) == 2
        assert json.loads(calls[0]["json_schema"]) == JUSTLOOP_RESULT_SCHEMA
        assert "[LOOP_COMPLETE]" not in calls[0]["prompt"]

    def test_missing_structured_result_falls_back_to_marker(self, jsonl_state, monkeypatch):
        results = [_cr(None, result="more to do"), _cr(None, result="all done\n[LOOP_COMPLETE]")]
        ps, calls = _run_justloop(jsonl_state, monkeypatch, results)
        assert ps.completion_reason == "loop_complete"
        assert len(calls) == 2

    def test_marker_ignored_when_typed_status_present(self, jsonl_state, monkeypatch):
        results = [
            _cr(dict(TYPED, step="", status="in_progress"), result="[LOOP_COMPLETE]"),
            _cr(dict(TYPED, step="", status="nothing_left")),
        ]
        _, calls = _run_justloop(jsonl_state, monkeypatch, results)
        assert len(calls) == 2

    def test_plan_file_run_ticks_and_ends_when_all_ticked(self, jsonl_state, monkeypatch):
        plan = jsonl_state.project_dir / ".pralph" / "loop-plan.md"
        plan.write_text("## Steps\n\n- [ ] one\n- [ ] two\n\n## Progress notes\n")
        results = [
            _cr(dict(TYPED, step="- [ ] one", note="did one")),
            _cr(dict(TYPED, step="- [ ] two", note="did two")),
        ]
        ps, calls = _run_justloop(jsonl_state, monkeypatch, results, plan_file=".pralph/loop-plan.md")
        assert ps.completion_reason == "loop_complete"
        assert len(calls) == 2
        assert plan.read_text() == "## Steps\n\n- [x] one\n- [x] two\n\n## Progress notes\n\n- Step 1: did one\n- Step 2: did two\n"
        assert ".pralph/loop-plan.md" in calls[0]["prompt"]
        assert "Do NOT tick checklist boxes" in calls[0]["prompt"]

    def test_run_log_rows_carry_typed_fields(self, jsonl_state, monkeypatch):
        results = [_cr(dict(TYPED, status="blocked", blocker="boom")), _cr(dict(TYPED, status="nothing_left"))]
        _run_justloop(jsonl_state, monkeypatch, results)
        rows = jsonl_state.load_run_log()
        assert rows[0]["status"] == "blocked"
        assert rows[0]["blocker"] == "boom"
        assert rows[0]["step"] == TYPED["step"]
        assert rows[0]["commit"] == "abc1234def"
        assert rows[1]["status"] == "nothing_left"


def test_duckdb_run_log_carries_typed_fields(duckdb_state):
    duckdb_state.log_iteration(IterationResult(
        iteration=1, phase="justloop", mode="execute", success=True,
        status="blocked", step="- [ ] a", commit="abc", blocker="boom",
    ))
    row = duckdb_state.load_run_log()[-1]
    assert (row["status"], row["step"], row["commit"], row["blocker"]) == ("blocked", "- [ ] a", "abc", "boom")


def test_duckdb_migration_adds_typed_columns():
    import duckdb

    from pralph.db import _ensure_schema

    conn = duckdb.connect()
    conn.execute("CREATE TABLE run_log (project_id TEXT NOT NULL, iteration INTEGER, logged_at TIMESTAMP)")
    _ensure_schema(conn)
    cols = {r[0] for r in conn.execute("DESCRIBE run_log").fetchall()}
    assert {"session_id", "status", "step", "commit", "blocker"} <= cols

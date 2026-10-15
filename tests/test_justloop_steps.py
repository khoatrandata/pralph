"""Change 2: step attempts, blocker signatures and stop rules enforced in code."""

from __future__ import annotations

import pytest

import pralph.loop as loop
from pralph import justloop_steps as js
from pralph.models import IterationResult, PhaseState, StepRecord
from pralph.runner import ClaudeResult


# ── blocker signatures ────────────────────────────────────────────────


class TestBlockerSignature:
    def test_plan_example_matches(self):
        assert js.blocker_signature("timeout after 31s at /tmp/x/123") == js.blocker_signature(
            "timeout after 29s at /tmp/y/456"
        )

    def test_case_and_whitespace(self):
        assert js.blocker_signature("Permission  DENIED\n") == js.blocker_signature("permission denied")

    def test_paths_trimmed_to_last_two_components(self):
        assert js.blocker_signature("cannot write /Users/a/repo/.claude/settings.json") == \
            js.blocker_signature("cannot write /home/b/other/.claude/settings.json")
        assert js.blocker_signature("missing src/pkg/mod.py") == "missing pkg/mod.py"

    def test_different_files_differ(self):
        assert js.blocker_signature("cannot write .claude/settings.json") != \
            js.blocker_signature("cannot write .claude/hooks.json")

    def test_hex_and_digits(self):
        assert js.blocker_signature("commit 3f9a2c1d failed (exit 1)") == js.blocker_signature(
            "commit 0b7e44a9e1 failed (exit 2)"
        )

    def test_words_that_look_hex_are_kept(self):
        assert "deadbeef" in js.blocker_signature("deadbeef error")

    def test_different_errors_differ(self):
        assert js.blocker_signature("timeout after 31s") != js.blocker_signature("connection refused")


# ── record_step_result ────────────────────────────────────────────────


def _ir(status, step="- [ ] build it", blocker=""):
    return IterationResult(iteration=1, phase="justloop", mode="execute", success=True,
                           status=status, step=step, blocker=blocker)


class TestRecordStepResult:
    def test_same_blocker_twice_stops(self):
        ps = PhaseState(phase="justloop")
        assert js.record_step_result(ps, _ir("blocked", blocker="timeout after 31s"), 5) == ""
        assert js.record_step_result(ps, _ir("blocked", blocker="timeout after 29s"), 5) == "repeated_blocker"
        assert "build it" in js.stop_message(ps, "repeated_blocker")
        assert "timeout after 29s" in js.stop_message(ps, "repeated_blocker")

    def test_same_blocker_not_consecutive_still_stops(self):
        ps = PhaseState(phase="justloop")
        js.record_step_result(ps, _ir("blocked", blocker="error A"), 5)
        js.record_step_result(ps, _ir("in_progress"), 5)
        assert js.record_step_result(ps, _ir("blocked", blocker="error A"), 5) == "repeated_blocker"

    def test_changing_blocker_runs_to_cap(self):
        ps = PhaseState(phase="justloop")
        assert js.record_step_result(ps, _ir("blocked", blocker="error A"), 3) == ""
        assert js.record_step_result(ps, _ir("blocked", blocker="error B"), 3) == ""
        assert js.record_step_result(ps, _ir("blocked", blocker="error C"), 3) == "step_attempts_exhausted"

    def test_in_progress_counts_toward_cap(self):
        ps = PhaseState(phase="justloop")
        assert js.record_step_result(ps, _ir("in_progress"), 2) == ""
        assert js.record_step_result(ps, _ir("in_progress"), 2) == "step_attempts_exhausted"

    def test_same_blocker_on_different_steps_does_not_stop(self):
        ps = PhaseState(phase="justloop")
        js.record_step_result(ps, _ir("blocked", step="- [ ] one", blocker="boom"), 5)
        assert js.record_step_result(ps, _ir("blocked", step="- [ ] two", blocker="boom"), 5) == ""

    def test_done_never_stops(self):
        ps = PhaseState(phase="justloop")
        js.record_step_result(ps, _ir("in_progress"), 2)
        assert js.record_step_result(ps, _ir("done"), 2) == ""

    def test_untyped_or_stepless_results_not_tracked(self):
        ps = PhaseState(phase="justloop")
        assert js.record_step_result(ps, _ir(""), 1) == ""
        assert js.record_step_result(ps, _ir("in_progress", step=""), 1) == ""
        assert ps.steps == {}

    def test_step_key_ignores_checkbox(self):
        ps = PhaseState(phase="justloop")
        js.record_step_result(ps, _ir("blocked", step="- [ ] build it", blocker="x"), 5)
        assert js.record_step_result(ps, _ir("blocked", step="build it", blocker="x"), 5) == "repeated_blocker"


def test_stop_message_without_plan_truncates_step():
    ps = PhaseState(phase="justloop")
    long_step = "- [ ] " + "x" * 100
    js.record_step_result(ps, _ir("blocked", step=long_step, blocker="b"), 5)
    js.record_step_result(ps, _ir("blocked", step=long_step, blocker="b"), 5)
    msg = js.stop_message(ps, "repeated_blocker")
    assert msg == "step '" + "x" * 57 + "...' hit the same blocker twice: b"


class TestStepContext:
    PLAN = "## Steps\n\n- [x] one\n- [ ] two\n  - [ ] sub\n- [ ] three\n"

    def test_first_attempt(self):
        text, header = js.build_step_context(PhaseState(phase="justloop"), 3, self.PLAN)
        assert "step 2, attempt 1 of 3" in text
        assert "- [ ] two" in text
        assert header == "step 2, attempt 1/3"

    def test_after_blocked_attempt(self):
        ps = PhaseState(phase="justloop", steps={"two": StepRecord(attempts=1, last_status="blocked",
                                                                   last_blocker="no access")})
        text, _ = js.build_step_context(ps, 3, self.PLAN)
        assert "attempt 2 of 3" in text
        assert "Last attempt was blocked: no access" in text

    def test_prior_run_blocker(self):
        ps = PhaseState(phase="justloop")
        js.seed_carried_blocker(ps, "- [ ] two", "no access")
        text, _ = js.build_step_context(ps, 3, self.PLAN)
        assert "The previous run stopped on step 2 with: no access" in text

    def test_no_plan_file_reports_last_step(self):
        ps = PhaseState(phase="justloop", last_step="lint",
                        steps={"lint": StepRecord(attempts=1, last_status="blocked", last_blocker="ruff missing")})
        text, _ = js.build_step_context(ps, 3)
        assert "reported blocked: ruff missing" in text

    def test_all_ticked(self):
        assert js.build_step_context(PhaseState(phase="justloop"), 3, "- [x] a\n") == ("", "")


class TestCarriedBlocker:
    def test_blocked_last_step_is_carried(self):
        prev = PhaseState(phase="justloop", last_step="two",
                          steps={"two": StepRecord(attempts=2, last_status="blocked", last_blocker="no access")})
        assert js.carried_blocker(prev) == ("two", "no access")

    @pytest.mark.parametrize("prev", [
        None,
        PhaseState(phase="justloop"),
        PhaseState(phase="justloop", last_step="two", steps={"two": StepRecord(last_status="done")}),
    ])
    def test_nothing_carried(self, prev):
        assert js.carried_blocker(prev) is None


# ── persistence ───────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", ["jsonl_state", "duckdb_state"])
def test_steps_round_trip(backend, request):
    state = request.getfixturevalue(backend)
    ps = PhaseState(phase="justloop", last_step="two", steps={
        "two": StepRecord(attempts=2, last_status="blocked", last_blocker="b", signatures=["b"]),
    })
    state.save_phase_state(ps)
    loaded = state.load_phase_state("justloop")
    assert loaded.last_step == "two"
    assert loaded.steps["two"] == ps.steps["two"]


# ── simulated loops (scripted claude results, no subprocess) ─────────


def _cr(status, step="- [ ] two", blocker="", success=True):
    structured = {"step": step, "status": status, "commit": "", "note": f"{status} {blocker}".strip(),
                  "blocker": blocker, "needs_human": []}
    return ClaudeResult(success=success, structured_output=structured if success else None,
                        error="" if success else "boom", cost_usd=0.01)


def _run(state, monkeypatch, results, **kwargs):
    calls = []

    def fake_run(prompt, **kw):
        calls.append(prompt)
        return results[len(calls) - 1]

    monkeypatch.setattr(loop, "run_with_retry", fake_run)
    ps = loop.run_justloop(state, user_prompt="do it", max_iterations=len(results) + 3, cooldown=0, **kwargs)
    return ps, calls


@pytest.fixture
def plan(jsonl_state):
    path = jsonl_state.project_dir / "loop-plan.md"
    path.write_text("## Steps\n\n- [x] one\n- [ ] two\n- [ ] three\n\n## Progress notes\n")
    return path


class TestSimulatedLoop:
    def test_repeated_blocker_stops_on_second(self, jsonl_state, plan, monkeypatch, capsys):
        results = [_cr("blocked", blocker="timeout after 31s"), _cr("blocked", blocker="timeout after 29s")]
        ps, calls = _run(jsonl_state, monkeypatch, results, plan_file="loop-plan.md")
        assert len(calls) == 2
        assert ps.completion_reason == "repeated_blocker"
        out = capsys.readouterr().out
        assert "step 2 hit the same blocker twice: timeout after 29s" in out
        assert "attempt 2 of 3" in calls[1]
        assert "Last attempt was blocked: timeout after 31s" in calls[1]

    def test_changing_blocker_runs_to_cap(self, jsonl_state, plan, monkeypatch):
        results = [_cr("blocked", blocker=b) for b in ("error A", "error B", "error C")]
        ps, calls = _run(jsonl_state, monkeypatch, results, plan_file="loop-plan.md", max_step_attempts=3)
        assert len(calls) == 3
        assert ps.completion_reason == "step_attempts_exhausted"

    def test_in_progress_cap(self, jsonl_state, plan, monkeypatch):
        ps, calls = _run(jsonl_state, monkeypatch, [_cr("in_progress")] * 2, plan_file="loop-plan.md",
                         max_step_attempts=2)
        assert len(calls) == 2
        assert ps.completion_reason == "step_attempts_exhausted"

    def test_progress_across_steps(self, jsonl_state, plan, monkeypatch):
        results = [_cr("in_progress"), _cr("done"), _cr("blocked", step="- [ ] three", blocker="x"),
                   _cr("done", step="- [ ] three")]
        ps, calls = _run(jsonl_state, monkeypatch, results, plan_file="loop-plan.md")
        assert ps.completion_reason == "loop_complete"
        assert "step 3, attempt 1 of 3" in calls[2]
        assert "step 3, attempt 2 of 3" in calls[3]

    def test_consecutive_invocation_errors(self, jsonl_state, monkeypatch):
        ps, calls = _run(jsonl_state, monkeypatch, [_cr("", success=False)] * 5)
        assert len(calls) == 5
        assert ps.completion_reason == "consecutive_errors"

    def test_nothing_left(self, jsonl_state, monkeypatch):
        ps, calls = _run(jsonl_state, monkeypatch, [_cr("nothing_left", step="")])
        assert len(calls) == 1
        assert ps.completion_reason == "loop_complete"


class TestAcrossRuns:
    def _second_run(self, state, monkeypatch, results):
        """Mirror the CLI: keep the previous state, reset, run."""
        previous = state.load_phase_state("justloop")
        state.save_phase_state(PhaseState(phase="justloop"))
        return _run(state, monkeypatch, results, plan_file="loop-plan.md", previous_state=previous)

    def test_blocked_step_retried_once_then_stops_if_same(self, jsonl_state, plan, monkeypatch):
        _run(jsonl_state, monkeypatch, [_cr("blocked", blocker="no access to /tmp/a/1")] * 2,
             plan_file="loop-plan.md")
        ps, calls = self._second_run(jsonl_state, monkeypatch, [_cr("blocked", blocker="no access to /tmp/b/2")])
        assert "The previous run stopped on step 2 with: no access to /tmp/a/1" in calls[0]
        assert len(calls) == 1
        assert ps.completion_reason == "repeated_blocker"

    def test_cleared_blocker_continues(self, jsonl_state, plan, monkeypatch):
        _run(jsonl_state, monkeypatch, [_cr("blocked", blocker="no access")] * 2, plan_file="loop-plan.md")
        ps, calls = self._second_run(jsonl_state, monkeypatch, [_cr("done"), _cr("done", step="- [ ] three")])
        assert len(calls) == 2
        assert ps.completion_reason == "loop_complete"
        assert "previous run stopped" not in calls[1]

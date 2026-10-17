"""Changes 5 and 6: focused plan context and a model per step."""

from __future__ import annotations

import json

import pytest

import pralph.loop as loop
from pralph import plan_context as pc
from pralph import plan_file as planfile
from pralph.runner import ClaudeResult

PLAN = """\
# Plan: archive lookup

## Rules

- One step per iteration.
- Never edit `.claude/settings.json` directly.

## Steps

- [x] Add the parser in scripts/archive_lookup.py
- [x] Wire EFF-3173 search keys
- [x] Add the README section
- [x] Write tests for parse_rows
- [ ] Fix the date bug in scripts/archive_lookup.py
  - keep the old format working
  - see EFF-3173 for examples
- [ ] Release it (model: fable)

## Progress notes

- Step 1: created scripts/archive_lookup.py with a parse_rows function.
- Step 2: EFF-3173 keys added; the API returns ISO dates.
- Step 3: README updated with usage.
- Step 4: tests in tests/test_lookup.py
  cover the happy path only.
"""


# ── parsing ──────────────────────────────────────────────────────────


class TestParse:
    def test_standard_layout(self):
        parts = pc.parse_plan(PLAN)
        assert parts.recognised
        assert parts.rules.startswith("- One step per iteration.")
        assert "settings.json" in parts.rules
        assert [n.number for n in parts.notes] == [1, 2, 3, 4]
        assert parts.notes[3].text == "Step 4: tests in tests/test_lookup.py cover the happy path only."

    def test_missing_sections(self):
        parts = pc.parse_plan("- [ ] one\n- [ ] two\n")
        assert parts.recognised and parts.rules == "" and parts.notes == []

    def test_extra_headings_and_case(self):
        text = ("## RULES\n\nbe careful\n\n### Rule details\n\nmore\n\n## Background\n\nnoise\n\n"
                "## Steps\n\n- [ ] one\n\n## progress NOTES\n\n- n1\n\n## Appendix\n\n- not a note\n")
        parts = pc.parse_plan(text)
        assert parts.rules == "be careful\n\n### Rule details\n\nmore"
        assert [n.text for n in parts.notes] == ["n1"]

    def test_unknown_layout(self):
        assert not pc.parse_plan("# Todo\n\n1. do this\n2. do that\n").recognised

    def test_step_block_includes_sub_bullets(self):
        nxt = planfile.next_step(PLAN)
        assert pc.step_block(PLAN, nxt) == (
            "- [ ] Fix the date bug in scripts/archive_lookup.py\n"
            "  - keep the old format working\n"
            "  - see EFF-3173 for examples"
        )


# ── relevance ────────────────────────────────────────────────────────


class TestRelevance:
    def test_tokens(self):
        toks = pc.step_tokens("Fix `parse_rows` in scripts/archive_lookup.py for EFF-3173 and main.py; do it")
        assert {"parse_rows", "scripts/archive_lookup.py", "EFF-3173", "main.py"} <= toks
        assert "Fix" not in toks and "do" not in toks

    def test_early_note_sharing_a_path_is_included_and_unrelated_is_not(self):
        notes = pc.parse_plan(PLAN).notes
        step = pc.step_block(PLAN, planfile.next_step(PLAN))
        chosen = [n.number for n in pc.select_notes(notes, step)]
        # newest first: the last two, then the earlier matches (EFF-3173, archive_lookup.py)
        assert chosen == [4, 3, 2, 1]
        unrelated = pc.select_notes(notes, "- [ ] Bump the version")
        assert [n.number for n in unrelated] == [4, 3]

    def test_prefix_of_a_path_does_not_match(self):
        notes = [pc.Note(1, "touched docs/guide.md.bak"), pc.Note(2, "a"), pc.Note(3, "b")]
        assert [n.number for n in pc.select_notes(notes, "edit docs/guide.md")] == [3, 2]
        notes[0] = pc.Note(1, "touched docs/guide.md.")
        assert [n.number for n in pc.select_notes(notes, "edit docs/guide.md")] == [3, 2, 1]

    def test_cap(self):
        notes = [pc.Note(i, f"main.py change {i} " + "x" * 1000) for i in range(1, 8)]
        chosen = pc.select_notes(notes, "fix main.py", cap=3000)
        assert sum(len(n.text) for n in chosen) <= 3000
        assert [n.number for n in chosen][:2] == [7, 6]

    def test_long_recent_note_is_cut_not_dropped(self):
        notes = [pc.Note(1, "y" * 5000)]
        [only] = pc.select_notes(notes, "anything", cap=3000)
        assert len(only.text) == 3000 and only.text.endswith("...")


# ── the prompt block ─────────────────────────────────────────────────


class TestBuildContext:
    def test_focused_order_and_size(self):
        nxt = planfile.next_step(PLAN)
        ctx = pc.build_plan_context(PLAN, nxt, ".pralph/loop-plan.md", record="Next step: step 5, attempt 1 of 3.")
        t = ctx.text
        assert ctx.focused and ctx.note_ids == [4, 3, 2, 1]
        order = [t.index(h) for h in ("### Rules", "### Current step (step 5)", "### This step so far",
                                       "### Relevant progress notes")]
        assert order == sorted(order)
        assert "Add the parser" not in t  # finished steps are not sent
        assert "`.pralph/loop-plan.md`" in t
        assert ctx.chars == len(t)

    def test_smaller_than_the_file_with_many_finished_steps(self):
        big = PLAN.replace("## Progress notes\n", "## Progress notes\n" + "".join(
            f"- Step {i}: did unrelated thing number {i} " + "z" * 200 + "\n" for i in range(20)))
        nxt = planfile.next_step(big)
        assert pc.build_plan_context(big, nxt, "p.md").chars < len(big)

    def test_unknown_layout_sends_whole_file(self):
        text = "# Todo\n\n1. do this\n"
        ctx = pc.build_plan_context(text, None, "todo.md")
        assert not ctx.focused and "1. do this" in ctx.text and "(full)" in ctx.text


# ── model tags ───────────────────────────────────────────────────────


class TestModelTag:
    @pytest.mark.parametrize("text, model", [
        ("Release it (model: fable)", "fable"),
        ("Release it (Model: OPUS)", "opus"),
        ("Release it (model:claude-sonnet-5-5)  ", "claude-sonnet-5-5"),
        ("Release it", ""),
        ("Mention (model: fable) mid-line then more", ""),
    ])
    def test_parse(self, text, model):
        assert pc.model_tag(text) == model

    def test_valid(self):
        assert pc.valid_model("fable") and pc.valid_model("claude-opus-5-5")
        assert not pc.valid_model("gpt-9") and not pc.valid_model("")

    def test_tag_survives_ticking(self):
        content, _ = planfile.tick_line("- [ ] Release it (model: fable)\n", "- [ ] Release it (model: fable)")
        assert content == "- [x] Release it (model: fable)\n"


# ── simulated loop ───────────────────────────────────────────────────


def _cr(step, status="done"):
    structured = {"step": step, "status": status, "commit": "", "note": f"did {step[6:30]}",
                  "blocker": "", "needs_human": []}
    return ClaudeResult(success=True, structured_output=structured, cost_usd=0.01)


def _run(state, monkeypatch, steps_, **kwargs):
    calls = []

    def fake_run(prompt, **kw):
        calls.append((prompt, kw))
        return _cr(steps_[len(calls) - 1])

    monkeypatch.setattr(loop, "run_with_retry", fake_run)
    ps = loop.run_justloop(state, user_prompt="do it", max_iterations=len(steps_) + 1, cooldown=0,
                           model="opus", **kwargs)
    return ps, calls


class TestLoop:
    def test_model_per_step_and_focused_prompt(self, jsonl_state, monkeypatch, capsys):
        plan = (jsonl_state.project_dir / "loop-plan.md")
        plan.write_text(PLAN.replace("- [ ] Release it (model: fable)",
                                     "- [ ] Review it (model: fable)\n- [ ] Ship it"))
        steps_ = ["- [ ] Fix the date bug in scripts/archive_lookup.py",
                  "- [ ] Review it (model: fable)", "- [ ] Ship it"]
        ps, calls = _run(jsonl_state, monkeypatch, steps_, plan_file="loop-plan.md")
        assert [kw["model"] for _, kw in calls] == ["opus", "fable", "opus"]
        assert ps.completion_reason == "loop_complete"
        first = calls[0][0]
        assert "### Current step (step 5)" in first and "keep the old format working" in first
        assert "Add the parser in scripts" not in first
        assert "Step line:" not in first
        out = capsys.readouterr().out
        assert "model fable" in out and "plan context: notes [4, 3, 2, 1]" in out
        log = [json.loads(line) for line in jsonl_state._run_log_path.read_text().splitlines()]
        assert [r["model"] for r in log] == ["opus", "fable", "opus"]
        assert log[0]["context"]["focused"] is True and log[0]["context"]["notes"] == [4, 3, 2, 1]
        # notes appended by pralph after step 5 are the newest for step 6
        assert log[1]["context"]["notes"][0] == 5

    def test_unknown_model_tag_falls_back(self, jsonl_state, monkeypatch, capsys):
        (jsonl_state.project_dir / "loop-plan.md").write_text("- [ ] one (model: gpt-9)\n")
        ps, calls = _run(jsonl_state, monkeypatch, ["- [ ] one (model: gpt-9)"], plan_file="loop-plan.md")
        assert calls[0][1]["model"] == "opus"
        assert "unknown model 'gpt-9'" in capsys.readouterr().out
        assert ps.completion_reason == "loop_complete"

    def test_unknown_layout_runs_with_whole_file(self, jsonl_state, monkeypatch):
        (jsonl_state.project_dir / "todo.md").write_text("# Todo\n\n1. do this\n")
        calls = []

        def fake_run(prompt, **kw):
            calls.append(prompt)
            return ClaudeResult(success=True, cost_usd=0.01, structured_output={
                "step": "", "status": "nothing_left", "commit": "", "note": "", "blocker": "", "needs_human": []})

        monkeypatch.setattr(loop, "run_with_retry", fake_run)
        ps = loop.run_justloop(jsonl_state, user_prompt="do it", max_iterations=2, cooldown=0,
                               plan_file="todo.md")
        assert "1. do this" in calls[0] and "(full)" in calls[0]
        assert ps.completion_reason == "loop_complete"

    def test_no_plan_file_records_run_model(self, duckdb_state, monkeypatch):
        calls = []

        def fake_run(prompt, **kw):
            calls.append(kw)
            return ClaudeResult(success=True, cost_usd=0.01, structured_output={
                "step": "", "status": "nothing_left", "commit": "", "note": "", "blocker": "", "needs_human": []})

        monkeypatch.setattr(loop, "run_with_retry", fake_run)
        loop.run_justloop(duckdb_state, user_prompt="do it", max_iterations=1, cooldown=0, model="sonnet")
        row = duckdb_state.load_run_log()[-1]
        assert row["model"] == "sonnet" and row["context"] == ""

    def test_review_model(self, jsonl_state, monkeypatch):
        seen = {}

        class FakeReviewer:
            def __init__(self, project_dir, **kw):
                seen.update(kw)

        monkeypatch.setattr(loop, "StepReviewer", FakeReviewer)
        monkeypatch.setattr(loop, "_run_loop", lambda *a, **k: loop.PhaseState(phase="justloop"))
        loop.run_justloop(jsonl_state, user_prompt="x", model="opus", review="fresh", review_model="fable")
        assert seen["model"] == "fable"


def test_cost_per_model_query(jsonl_state):
    from pralph.models import IterationResult
    from pralph.report import run_builtin_query
    for model, cost in (("opus", 0.5), ("fable", 0.2), ("opus", 0.25), ("", 1.0)):
        jsonl_state.log_iteration(IterationResult(iteration=1, phase="justloop", mode="execute",
                                                  success=True, cost_usd=cost, model=model))
    cols, rows = run_builtin_query("cost-per-model", jsonl_state)
    assert cols[0] == "model"
    assert rows == [("opus", 2, 0.75, 0, 0), ("fable", 1, 0.2, 0, 0)]

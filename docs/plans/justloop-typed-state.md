# Plan: justloop with typed state, a fresh reviewer and focused context

Status: changes 1 and 2 merged (PR #1, 2026-10-16); changes 3 and 4 implemented on branch `feat/justloop-review-denials` (2026-10-16); changes 5 and 6 not started. Written 2026-10-15 for hand-over to another session. The design sections describe the plan as written; where the implementation differs, the change notes under each heading say how.

## Why

`justloop` today trusts text. The agent decides when a step is done, writes free-text notes, rates its own work, and signals the end with `[LOOP_COMPLETE]` in prose. pralph's code checks none of it. One run on 2026-10-15 (platform-data-lake, branch `feat/agent-state-store`, seven steps from a plan file) shows what that costs:

- The loop rated every step 10/10 in its own self-review. A fresh-session review then gave 8/10, and the CI review on the PR gave 5.5/10. The missed defects were real: new tests not wired into CI, missing permission allow rules, and hooks that could block a prompt when a file was missing.
- Two writes were refused by permissions, under `.claude/` and to the settings file. The loop worked around one refusal and left the other as a sentence in a progress note. A person found both only by reading the notes.
- An earlier run, recorded in platform-data-lake's `.claude/references/archive-lookup-loop-plan.md`, would not retry a step because its own notes still said "blocked" after the cause was fixed. The stop rule lived in prompt text, and the notes outlived the condition.

The design idea behind all six changes: keep the state the loop depends on in pralph's own typed records, and let code make the decisions that code can make (stop, tick, review, route). The model does the work and reports in a fixed shape. This follows "Jev Engineering for Coding Agents" (September 2026): state explicit and typed, small decisions answered in typed form, and context assembled per question instead of accumulated.

## The six changes, in build order

1. Typed iteration result (structured output), the foundation. **Done.**
2. Step attempts and a stop rule enforced in code. **Done.**
3. A fresh-process reviewer after each step. **Done.**
4. Permission denials recorded as typed hand-off items. **Done.**
5. Focused context: send the current step, not the whole plan.
6. Model chosen per step.

Changes 2–6 all read the typed result from change 1, so build change 1 first. Changes 3–6 are independent of each other after that.

## Background a new session needs

- `run_justloop` is in `pralph/loop.py` (near line 1542). It builds the prompt with `assemble_justloop_prompt` (`pralph/assembler.py`, near line 539), calls `run_with_retry` (`pralph/runner.py`), and stops through `completion_fn`. The only stops in code are `[LOOP_COMPLETE]` (found by `detect_loop_complete` in `pralph/parser.py`) and `consecutive_errors >= 5`, which counts failed `claude` invocations, not task failures.
- `run_claude` runs `claude -p --output-format stream-json`, and `_parse_result_event` reads the final `result` event into `ClaudeResult` (success, text, cost, token counts). The CLI supports `--json-schema <schema>` for structured output (confirmed with `claude --help` on 2026-10-15).
- `PhaseState` (`pralph/models.py`) holds iteration counters, the last error and the last summary. `IterationResult` holds one iteration's outcome. Both are saved through the `StateManager`, which has JSONL and DuckDB backends (`pralph/state.py`, `jsonl_state.py`, `db_state.py`).
- The `justloop` CLI command (`pralph/cli.py`, near line 626) resets its phase on every start ("justloop is a standalone tool, not a resumable phase").
- `implement` already has a fresh-session reviewer (`pralph/review.py`, `run_review`). On a review error it auto-approves. Do not copy that behaviour into justloop.
- The plan-file convention that platform-data-lake uses (`.pralph/loop-plan.md`): Rules, an ordered checklist of `- [ ]` steps, and Progress notes. The prompt is just "Read .pralph/loop-plan.md and do the next unchecked step." pralph does not parse this file today.
- Tests are pytest under `tests/`. Release goes through CodeArtifact (platform-data-lake's `/publish` skill). Never hand-bump the version.

## Change 1 — Typed iteration result

**Implemented** in `db9cb4f`, with follow-ups in `7f9e199` and the review-fix commit on the same branch. Differences from the design below:
- The prompt drops the `[LOOP_COMPLETE]` section in both modes, not only with `--plan-file`, because the structured object replaces the final text.
- Progress notes are prefixed with the step number and, when not done, the status ("Step 3 (blocked): ...").
- A `done` result whose step cannot be ticked is logged as an error and counted as an `in_progress` attempt on the plan's next step.
- With `--plan-file`, the loop also stops once every top-level step is ticked, and the prompt defaults to "Read PATH and do the next unchecked step."
- pralph does not commit its plan-file edits.

**Problem.** The loop's outcome is prose. pralph cannot tell done from blocked from half-done, and cannot read the commit, the note or the blocker without parsing text.

**Design.**
- Each justloop iteration runs `claude -p` with `--json-schema`, passing a fixed schema. The model's final answer must be one object with these fields:
  - `step`: the checklist line it worked on, verbatim, or empty when there is no plan file;
  - `status`: one of `done`, `in_progress`, `blocked`, `nothing_left`;
  - `commit`: the commit sha it made, or empty;
  - `note`: one line for the next iteration, which pralph writes to the progress notes;
  - `blocker`: when blocked, the error text that stopped it;
  - `needs_human`: a list of actions only a person can take (see change 4).
- `_parse_result_event` reads the structured output from the result event into a new field on `ClaudeResult`. Check the exact key name the CLI emits by running one call with `--json-schema` and printing the raw result event. Don't assume it.
- `completion_fn` stops on `status == nothing_left`, replacing `[LOOP_COMPLETE]`. Keep `detect_loop_complete` as a fallback for runs where structured output is missing, so old prompt templates and custom `justloop` templates still end.
- When the run has a plan file, pralph ticks the step itself (changes `- [ ]` to `- [x]` for the exact line) and appends the note under "Progress notes". The model no longer edits the checklist or the notes. Record the plan-file path with a new `--plan-file PATH` option, so pralph knows which file to manage. With no `--plan-file`, behaviour stays as today apart from the typed result.
- Add `status`, `step`, `commit` and `blocker` to `IterationResult`, and save them with the run log, so `pralph query` and the viewer can show per-step history.

**Prompt changes.** `pralph/prompts/justloop.py` drops the `[LOOP_COMPLETE]` section and the "tick the step / write a note" instructions when `--plan-file` is set. Instead it says to finish with the structured object. Keep the "one unit of work per iteration" rule.

**Acceptance.**
- A run with `--plan-file` ticks steps and writes notes without the model editing either.
- `nothing_left` ends the loop. A missing structured result falls back to the old marker and never crashes.
- `IterationResult` rows carry status, step, commit and blocker.

**Tests.** Unit-test the parser against recorded result events: with a structured object, without one, and with malformed JSON. Unit-test the plan-file ticker on a checklist with duplicate-looking lines and indented sub-bullets. Only the exact line is ticked, and a line that isn't found is a recorded error, not a silent skip.

**Open question.** If the model ends in `in_progress` repeatedly on the same step, is that progress or a stall? Change 2 answers that with an attempt cap.

## Change 2 — Step attempts and a stop rule in code

**Implemented** in `0dcac36`. Difference from the design below: the "last two path components" rule alone would not make the example blockers match, so paths under temp directories are reduced to their last component.

**Problem.** The stop rule ("stop if the same error appears twice") is prompt text, and the agent applies it by reading its own notes, which can be stale.

**Design.**
- Add a per-run `steps` record to `PhaseState`: for each step line, store attempts, last status, last blocker text and a normalised blocker signature. Normalise by lowercasing, replacing digits and hex runs with placeholders, and trimming paths to their last two components, so "timeout after 31s at /tmp/x/123" and "timeout after 29s at /tmp/y/456" match.
- Before each iteration, pralph tells the model which step is next and what happened before: "Step 4, attempt 2. Last attempt was blocked: <blocker>."
- Stop rules, all in code:
  - **Same blocker twice in this run** on the same step: stop with `completion_reason = repeated_blocker`, naming the step and the blocker.
  - **Attempt cap** per step (default 3, flag `--max-step-attempts`): stop with `step_attempts_exhausted`.
  - Keep `consecutive_errors >= 5` for invocation failures.
- **Across runs:** justloop resets its phase on start, which is right, because attempts count per run. On start, if the previous run ended on a blocker, print it and tell the first iteration: "The previous run stopped on step 4 with <blocker>. Try it once; if it fails the same way, report blocked." That gives the cleared-blocker retry the reference doc asks for, without trusting stale notes.

**Acceptance.**
- A step that returns the same blocker twice stops the run on the second time, with the step and the blocker printed.
- A step whose blocker changes keeps going until the attempt cap.
- A fresh run after a blocked run retries the blocked step once.

**Tests.** Signature normalisation cases. A simulated loop with fake iteration results covering each stop path. Inject `iteration_fn` so no `claude` call is needed. The `_run_loop` signature already takes functions.

## Change 3 — A fresh-process reviewer after each step

**Implemented** on `feat/justloop-review-denials`. Differences from the design below:
- The review base is the last cleanly reviewed commit, so a re-review after findings covers the step and its fixes together. The base starts at HEAD when the run starts.
- A done step with no new commit since the base is not reviewed and is ticked.
- Only exit 1 means findings. Any other non-zero exit, a signal, or a timeout (30 minutes, which kills the command's process group) is `review_error`, and the error is added to the "Needs a person" list. This matches platform-data-lake's `scripts/headless_review.py`: 0 is a 10/10, 1 is below 10, 2 is a review that did not complete and 3 is a limit that needs a person.
- The findings lead the iteration's "Step Context" section, which comes after the task, rather than the very top of the prompt.
- Without a plan file, findings are tracked under the step name "the last unit of work".
- `--review-every N` always reviews the plan's last step.
- The command also gets `PRALPH_STEP_NUMBER`.

**Problem.** Self-review in the same session rated 10/10 work that two independent reviews marked down to 8 and 5.5. A model rarely finds fault with the context it just produced.

**Design.**
- New option `--review-cmd "<command>"`. After an iteration ends `done` with a commit, pralph runs the command in the project directory as a subprocess, with `PRALPH_STEP`, `PRALPH_COMMIT` and `PRALPH_BASE` (the previous step's commit) in the environment.
- Contract: exit 0 means clean. Any other exit means findings; pralph reads the findings from stdout and caps them at about 4,000 characters for the next prompt.
- On findings, pralph does not tick the step. It marks the step `in_progress`, saves the findings in the step record, and the next iteration's prompt opens with "Fix these review findings for step N, then commit: <findings>". The attempt cap from change 2 applies, so a review that never passes ends the run as `review_not_passing`. It does not loop forever.
- If the review command itself crashes (not found, timeout), record `review_error` and stop. Never auto-approve on a review error; that is a deliberate difference from `implement`.
- Add a built-in option `--review fresh`, for users without a review script. It runs `claude -p` with a reviewer prompt in a new session, on that step's diff only, with read-only tools. The reviewer returns a typed verdict (`--json-schema`: `clean` true or false, plus `findings`). Reuse `assemble_review_prompt` ideas, but scope it to `git diff PRALPH_BASE..PRALPH_COMMIT`, not a story.
- For platform-data-lake, the intended value is `--review-cmd 'python3 scripts/headless_review.py --step --base "$PRALPH_BASE"'` (the `--step` mode was added for this loop: limits per step, spend capped per run, no push stamp). The original note follows.
- Earlier intended value: `--review-cmd "python3 scripts/headless_review.py"`. That script reviews the branch in a fresh read-only session, records a stamp, and has spend caps. Confirm how it sets its exit code before relying on it; its output prints a 0–10 rating and exits 1 below 10.
- Cost note: one review per step roughly doubles cost on review-heavy runs. Make `--review-every N` available for long, low-risk runs.

**Acceptance.**
- A step with review findings is not ticked. The next iteration receives the findings, and the step is ticked only after a clean review.
- A review command that errors stops the run with `review_error`.

**Tests.** A fake review command (a small script that exits 1 with findings the first time and 0 the second) driving a simulated loop. A crashing review command.

## Change 4 — Permission denials as typed hand-off items

**Implemented** on `feat/justloop-review-denials`. A recorded run (2026-10-16) confirmed the field: the result event's `permission_denials` lists `tool_name`, `tool_use_id` and `tool_input`. The refused tool result reads "Claude requested permissions to write to PATH, but you haven't granted it yet." with `is_error` set. Both sources are matched by `tool_use_id`, and the refusal text becomes the reason, with project paths made relative. Items are kept on the run's phase state and in the run log; `--stop-on-denial` stops with `permission_denied`. Model-reported items are deduplicated by their text.

**Problem.** When Claude Code refuses a tool call in an unattended run, the agent improvises a workaround or a sentence in the notes. The person running the loop learns about it only by reading everything.

**Design.**
- Collect denials from two sources. Use the result event's permission-denial list if the CLI emits one; check the raw event to confirm the field name. Also collect tool results in the stream that carry a permission-refusal message. `_print_event` already walks the stream, so add collection there.
- Add the structured `needs_human` list from change 1, for actions the model knows it can't take: an SSO grant, a deploy to a gated environment, an edit it was refused.
- Store every item in a run-level `needs_human` list with the step it came from: tool, a short form of the target (file path or command prefix), and the reason.
- At the end of the run, print a "Needs a person" checklist after the summary and write it to `.pralph/needs-human.md`. Deduplicate by tool and target.
- Optionally, `--stop-on-denial` stops at the first denial, for runs where a workaround would be worse than a stop.

**Acceptance.** A run with a refused write ends with a checklist naming the step, the tool and the path, without anyone reading the notes.

**Tests.** Parse recorded stream events containing a denial. Check deduplication and the end-of-run rendering.

## Change 5 — Focused context: send the current step

**Problem.** Every iteration reads the whole plan file: all rules, every step and every note. Most of it is irrelevant to the step at hand, and it grows with each note. In the measured sessions, re-reading context accounts for most tokens (98.7% of API tokens were cache reads over 14 days of sessions in platform-data-lake).

**Design.**
- With `--plan-file`, pralph parses the file into three parts: the Rules section, the checklist, and the notes. The parser is tolerant: headings are matched case-insensitively, and an unrecognised layout falls back to sending the whole file.
- The iteration prompt contains, in this order:
  - the Rules section in full;
  - the current step line plus any indented sub-bullets under it;
  - the step's own record from change 2 (attempt, last blocker, review findings);
  - notes chosen by relevance.
- Relevance, simplest first:
  - always include the last two notes;
  - also include any earlier note that names a file path, ticket key or identifier that appears in the current step text (exact-token match, extracted with a small regex);
  - cap the notes block at about 3,000 characters, newest first.
- The model keeps read access to the whole plan file. The prompt says where it is and that it may read it when a step refers to earlier work.
- Log what was included (note ids, characters) in the iteration record, so a miss can be traced later.

**Acceptance.**
- The prompt for step 5 holds the rules, step 5 and the relevant notes, and is smaller than the full file on a plan with more than three finished steps.
- A plan file in an unknown layout still works, sent whole.

**Tests.** Parser cases: the standard layout, missing sections, extra headings, and indented sub-bullets. A relevance test where an early note sharing a file path with the current step is included and an unrelated one is not.

**Open question.** Use a small model to choose notes instead of token matching? Start with matching, log the inclusions, and decide from evidence.

## Change 6 — Model per step

**Problem.** `--model` applies to the whole run. Users route by task type: in platform-data-lake, review and planning steps go to Fable and build and debug steps to Opus. A run that mixes them pays the wrong price or gets the wrong strength on some steps.

**Design.**
- A step may carry a model tag in the plan file: a trailing `(model: fable)` on the checklist line. A tag is easier to parse than free text and survives ticking.
- pralph uses the step's model for that iteration, `--model` otherwise. The built-in reviewer from change 3 takes `--review-model`, defaulting to the run model.
- Print the model on each iteration header, and record it in `IterationResult`, so cost per model shows in `pralph query`.
- Validate tags against the model aliases the CLI accepts. Log and fall back to `--model` on an unknown tag; don't fail the run.

**Acceptance.** A plan whose step 3 is tagged `(model: fable)` runs step 3 on Fable and the others on the default, and the run log shows it.

**Tests.** Tag parsing, the fallback on an unknown alias, and the model passed to `run_claude` per iteration with an injected runner.

## Compatibility and rollout

- Everything new is opt-in through `--plan-file`, `--review-cmd`/`--review`, `--max-step-attempts` and step tags. A plain `pralph justloop "fix all lint errors"` behaves as today, apart from the typed result, which falls back to the old marker.
- Custom justloop prompt templates (`resolve_prompt_template("justloop", ...)`) keep working. Document the new placeholders and the structured-output instruction for anyone who overrides the template.
- Ship changes 1 and 2 together as one minor version, then 3 and 4, then 5 and 6. Each release is published to CodeArtifact through platform-data-lake's `/publish`.
- After the first release, update platform-data-lake's `.claude/skills/pralph/SKILL.md` and `.claude/references/archive-lookup-loop-plan.md`. They describe the stop rule, self-review and progress notes as prompt conventions; after this work, pralph enforces them.

## Verification for the whole effort

Re-run a plan like the 2026-10-15 one: seven steps with a step that hits a permission refusal. Run it with `--plan-file`, `--review-cmd` and a model-tagged review step. Expected:
- steps are ticked by pralph;
- a review finding blocks a tick until fixed;
- the refusal appears in the "Needs a person" list;
- a repeated blocker stops the run on its second occurrence;
- the run log shows status, model and cost per step.

## Out of scope

- Changing `implement`'s reviewer, beyond noting its auto-approve on error.
- A shared state store across machines. The step records live with the run, in pralph's existing state backends.
- Choosing notes with a model (change 5's open question) until there is evidence.

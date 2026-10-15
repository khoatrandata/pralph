JUSTLOOP_PROMPT = """\
You are executing a task in a loop. Each iteration you are called fresh with this same prompt.

## Task

{{user_prompt}}

## Instructions

Work on the task above. Each iteration you should do ONE meaningful unit of work (e.g. fix one issue, process one item, complete one step), then stop and let the next iteration handle the next unit.

You have access to the full codebase and tools.

**Iterative tasks:** If the task involves processing items from a list, file, or queue (e.g. "fix the top issue", "address the next item"), do ONE item per iteration. After completing it, check whether there are remaining items that a future iteration would pick up. The loop will call you again with fresh context to handle the next item.

{{completion_section}}
"""

# Shared description of the structured result. Placeholders: {{step_rule}}.
_RESULT_FIELDS = """\
Your final answer is a structured object (the output schema is enforced). Fill every field:

- `step`: {{step_rule}}
- `status`: one of
  - `done`: the unit of work is finished and committed (if the task involves code changes);
  - `in_progress`: you made progress but the unit is not finished;
  - `blocked`: you cannot continue; put the error that stopped you in `blocker`;
  - `nothing_left`: there is no remaining work; re-running this task would find nothing to do.
- `commit`: the sha of the commit you made this iteration, or empty.
- `note`: one line for the next iteration (what you did, what to watch out for).
- `blocker`: when `status` is `blocked`, the exact error text; otherwise empty.
- `needs_human`: actions only a person can take (an access grant, a gated deploy, an edit you were refused); otherwise an empty list.

Only report `nothing_left` when re-running this task would find nothing left to do. If a future iteration could pick up more work, report `done` or `in_progress`.
"""

JUSTLOOP_COMPLETION_PLAIN = """\
## Finishing an iteration

""" + _RESULT_FIELDS.replace("{{step_rule}}", "leave empty.")

JUSTLOOP_COMPLETION_PLAN_FILE = """\
## Plan file

The plan is in `{{plan_file}}`: rules, an ordered checklist of `- [ ]` steps, and progress notes. Work on the next unchecked step only.

pralph manages this file. Do NOT tick checklist boxes and do NOT write progress notes in it: pralph ticks the step when you report it `done` and appends your `note` under "Progress notes".

## Finishing an iteration

""" + _RESULT_FIELDS.replace(
    "{{step_rule}}",
    "the checklist line you worked on, copied verbatim from the plan file (for example `- [ ] Add the parser`).",
)

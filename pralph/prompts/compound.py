COMPOUND_CAPTURE_PROMPT = """\
# Compound Learning: Capture Solutions

You just finished implementing a story. Your job is to analyze what was done and
capture any **non-trivial solutions** as structured documentation so future work
benefits from them.

## Story Context

**ID:** {{story_id}}
**Title:** {{story_title}}

{{implementation_summary}}

## Your Task

Analyze the implementation by:

1. **Context analysis** — Run `git diff HEAD~1` and `git log --oneline -5` to see what changed.
   Read any modified files you need to understand the solution.

2. **Solution extraction** — Identify problems that were solved during this implementation.
   Focus on things that required research, debugging, or non-obvious approaches.
   Skip trivial changes (adding a field, simple CRUD, renaming).

3. **Related documentation** — Check if there are existing docs, READMEs, or comments
   that relate to the solution. Note any patterns or conventions discovered.

4. **Prevention strategies** — For each problem, note how to avoid it in the future
   or how to detect it early.

5. **Classification** — Categorize each solution into one of these categories:
   `build-errors`, `test-failures`, `runtime-errors`, `performance-issues`,
   `database-issues`, `security-issues`, `ui-bugs`, `integration-issues`,
   `logic-errors`, `configuration-issues`

## Output Format

Return JSON:

```json
{
  "captured": true,
  "reason": "Brief explanation of what was notable",
  "solutions": [
    {
      "title": "Fix missing module import for X",
      "category": "build-errors",
      "tags": ["import", "build", "module-name"],
      "error_signature": "ModuleNotFoundError: No module named 'x'",
      "problem": "Description of the problem encountered",
      "solution": "Step-by-step description of the fix",
      "prevention": "How to avoid this in the future",
      "related_files": ["path/to/file.py"],
      "content": "Full markdown document for the solution file (see template below)"
    }
  ]
}
```

If nothing notable was solved (trivial changes, boilerplate, simple additions),
return:

```json
{
  "captured": false,
  "reason": "Brief explanation of why nothing was captured",
  "solutions": []
}
```

## Solution Document Template

Each solution's `content` field should be a markdown document following this structure:

```markdown
# Title

## Problem
What went wrong or what was the challenge.

## Error Signature
The exact error message or symptom (if applicable).

## Root Cause
Why the problem occurred.

## Solution
Step-by-step fix with code snippets where helpful.

## Prevention
How to avoid this in the future.

## Related Files
- path/to/relevant/file.py
```

## Guidelines

- Only capture solutions that would save someone time in the future
- Include specific error messages, file paths, and code snippets
- Tags should be lowercase, hyphenated keywords useful for search
- One solution per distinct problem (don't merge unrelated issues)
- Be concise but complete — another developer should be able to apply the solution
"""

COMPOUND_RECALL_SECTION = """\
## Past Solutions (Compound Learning)

Your team has documented solutions from previous implementations. These may be
relevant to the current task:

{{solutions_context}}

Use these solutions to:
- Avoid repeating known mistakes
- Apply proven patterns
- Skip research for previously solved problems
"""

INFER_DOMAIN_PROMPT = """\
# Domain Inference

Given a solution document and a list of available domains, determine which
domain(s) the solution belongs to.

## Available Domains

{{available_domains}}

## Solution

**Title:** {{title}}
**Category:** {{category}}
**Tags:** {{tags}}
**Error Signature:** {{error_signature}}

### Content

{{content}}

## Your Task

Pick the domain(s) from the available list that this solution is most relevant to.
Consider the programming language, frameworks, tools, and error types mentioned.

Return JSON:

```json
{
  "domains": ["domain1"],
  "reason": "Brief explanation"
}
```

Rules:
- Only return domains from the available list
- Usually a solution belongs to 1-2 domains — be specific, not broad
- If the solution is truly language/tool agnostic and could apply anywhere, return
  all available domains
"""


COMPACT_INDEX_PROMPT = """\
# Solution Index Compaction

You are compacting a solution index for a compound learning system. Your job is
to merge duplicate or near-duplicate solution entries and flag orphans.

## Current Index Entries

{{index_entries}}

## Solution Contents

{{solution_contents}}

## Your Task

1. **Identify duplicates and near-duplicates** — Solutions that describe the same
   problem/fix but were captured at different times or from different projects.
   Look at titles, error signatures, tags, and actual content to judge similarity.

2. **Merge duplicates** — When two or more solutions cover the same issue, produce
   a single merged entry that combines the best information from each:
   - Keep the most descriptive title
   - Union the tags (deduplicated, lowercase)
   - Keep the most specific error_signature
   - Merge related_files lists
   - Preserve the most recent created timestamp
   - Keep source_project from the most recent entry if present
   - Merge the content: combine problem descriptions, solutions, and prevention
     strategies into one comprehensive document. Don't just concatenate — synthesize.

3. **Preserve unique solutions** — Solutions that are genuinely different should be
   kept as-is, even if they share a category or some tags.

4. **Flag orphans** — Entries that reference missing files (already handled upstream)
   or have empty/meaningless content.

## Output Format

Return JSON:

```json
{
  "entries": [
    {
      "filename": "category/slug.md",
      "category": "build-errors",
      "title": "Merged or original title",
      "tags": ["tag1", "tag2"],
      "error_signature": "ErrorType: message",
      "related_files": ["path/to/file.py"],
      "story_id": "STORY-001",
      "created": "2024-04-18T12:00:00",
      "source_project": "/path/to/project",
      "content": "Full merged markdown content for the solution file"
    }
  ],
  "merges": [
    {
      "merged_into": "category/slug.md",
      "sources": ["category/old-slug-1.md", "category/old-slug-2.md"],
      "reason": "Brief explanation of why these were merged"
    }
  ],
  "removed": [
    {
      "filename": "category/slug.md",
      "reason": "Why this entry was removed (e.g. empty content, meaningless)"
    }
  ]
}
```

## Guidelines

- Be conservative: only merge when solutions clearly describe the same underlying
  problem. Two different build errors are not duplicates just because they're both
  build errors.
- When merging content, produce a clean markdown document following the standard
  solution template (# Title, ## Problem, ## Error Signature, ## Root Cause,
  ## Solution, ## Prevention, ## Related Files).
- Preserve all actionable information — don't lose steps, file paths, or code
  snippets during merging.
- The filename for merged entries should use the slug of the best title.
- Keep story_id from the most recent entry. If merging across projects,
  prefer the entry with source_project set.
- If there's nothing to merge or remove, return all entries unchanged with
  empty merges and removed arrays.
"""

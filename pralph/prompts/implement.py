IMPLEMENTATION_PHASE1_ANALYZE_PROMPT = """\
# Phase 1 Analysis Mode

You are analyzing all pending stories to identify which ones should be implemented
together in Phase 1 (architecture/infrastructure foundation).

## All Pending Stories

{{stories_json}}

## Your Task

1. Review ALL stories above
2. Identify stories that are:
   - Architecture or infrastructure setup
   - Foundation that other stories depend on
   - Database schema or models
   - Authentication/authorization setup
   - Core API routing or middleware
   - Shared utilities that multiple stories need

3. Group these stories for batch implementation
4. Determine the optimal implementation order within Phase 1

## Output Format

Return your analysis as JSON:
```json
{
  "phase_1_group": ["STORY-001", "STORY-005", "STORY-012"],
  "reasoning": {
    "STORY-001": "Sets up database schema - all other stories depend on this",
    "STORY-005": "Creates auth middleware - required before protected routes",
    "STORY-012": "Establishes API routing structure"
  },
  "implementation_order": ["STORY-012", "STORY-001", "STORY-005"],
  "dependencies": {
    "STORY-001": [],
    "STORY-005": ["STORY-001"],
    "STORY-012": []
  },
  "excluded_reasoning": {
    "STORY-002": "Feature-specific, not infrastructure"
  }
}
```

## Guidelines

- Include 3-10 stories maximum in Phase 1
- Only include true infrastructure/architecture stories
- Consider what OTHER stories will need as foundation
- The implementation order should respect dependencies
"""

IMPLEMENTATION_PHASE1_IMPLEMENT_PROMPT = """\
# Phase 1 Implementation Mode

You are implementing the Phase 1 architecture/infrastructure stories as a batch.

## Stories to Implement

{{phase1_stories_json}}

## Implementation Order

{{implementation_order}}

## Architecture Guidance

{{architecture_context}}

## Your Task

Implement ALL Phase 1 stories in the specified order. For each story:

1. Read existing code to understand the codebase structure
2. Implement the story according to its acceptance criteria
3. Write tests where appropriate
4. Ensure code follows project conventions

## Important

- These are FOUNDATION stories - they should establish patterns for later stories
- Create clear abstractions that other stories can build on
- Document any architectural decisions
- Run tests after implementation

## Output Format

After completing implementation, return a summary as JSON:
```json
{
  "completed_stories": ["STORY-001", "STORY-005", "STORY-012"],
  "files_created": ["path/to/file1.py", "path/to/file2.py"],
  "files_modified": ["path/to/existing.py"],
  "tests_added": ["test_file.py"],
  "architectural_decisions": [
    "Decision 1 and rationale",
    "Decision 2 and rationale"
  ],
  "notes_for_next_stories": "Any context that will help implement remaining stories"
}
```
"""

IMPLEMENTATION_IMPLEMENT_PROMPT = """\
# Story Implementation Mode

You are implementing a single user story from a backlog.

## Current Story to Implement

**Title:** {{input_item.title}}

**Content:**
{{input_item.content}}

**Metadata:**
{{input_item.metadata}}

{{implemented_summary}}

## Your Task

1. Read the story and its acceptance criteria carefully
2. Review relevant existing code for context and patterns
3. Implement the feature following existing code conventions
4. Write/update tests for your changes
5. Verify acceptance criteria are met
6. Commit your changes with a descriptive message

## Output Format

After completing implementation, you MUST return your result as structured JSON:

```json
{
  "status": "implemented",
  "summary": "Brief description of what was implemented",
  "files_changed": ["path/to/file1.py", "path/to/file2.py"],
  "tests_passed": true,
  "reason": null
}
```

### Status Values

- **implemented**: Successfully implemented the story
- **duplicate**: This story duplicates another (set `duplicate_of` to the original story ID)
- **external**: Requires work in an external system (set `external_system` and `reason`)
- **skipped**: Cannot implement for a valid reason (set `reason`)
- **error**: Technical error prevented implementation (set `reason`)

### Example Responses

Implemented successfully:
```json
{"status": "implemented", "summary": "Added user profile page with avatar upload", "files_changed": ["src/pages/profile.tsx", "src/api/upload.ts"], "tests_passed": true}
```

Duplicate of another story:
```json
{"status": "duplicate", "duplicate_of": "FND-003", "reason": "This is covered by the user profile story FND-003"}
```

Requires external system:
```json
{"status": "external", "external_system": "Stripe Dashboard", "reason": "Webhook configuration must be done in Stripe Dashboard, not code"}
```
"""

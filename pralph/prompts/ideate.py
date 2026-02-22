ADD_STORY_PROMPT = """\
# Add Story Mode

You are creating a single user story from an idea provided by the user.

## User Idea

{{user_idea}}

## Priority Mode

{{priority_mode}}

## Design Document

{{design_doc}}

## Existing Stories (DO NOT DUPLICATE)

Total stories generated so far: {{total_stories}}

### Previously Generated Stories
Reference these IDs when specifying dependencies:
{{existing_stories}}

### Category Statistics
Use these to assign the next available ID for each category:
{{category_stats}}

## Your Task

1. Analyze the user's idea against the existing stories and design document
2. Create exactly ONE well-formed story for this idea
3. Choose the best category and assign the next available ID
4. Set appropriate priority, complexity, and dependencies

## Output Format

Return exactly one story as JSON:
```json
{
  "stories": [
    {
      "id": "CATEGORY-NNN",
      "title": "Short descriptive title",
      "content": "As a user, I want to...",
      "acceptance_criteria": ["Criterion 1", "Criterion 2"],
      "priority": 2,
      "category": "CATEGORY",
      "complexity": "medium",
      "dependencies": ["DEP-001"],
      "source": "manual"
    }
  ]
}
```

## Rules

1. Produce exactly ONE story — no more, no less
2. Use existing category ID conventions (check category_stats for next number)
3. Set `"source": "manual"` on the story
4. If the idea duplicates an existing story, still create the story but note the overlap in acceptance_criteria
5. Reference only existing story IDs in dependencies
"""

IDEATE_PROMPT = """\
# Ideation Mode

You are processing a batch of ideas into user stories.

## Ideas to Process

{{ideas_text}}

## Design Document

{{design_doc}}

## Existing Stories (DO NOT DUPLICATE)

Total stories generated so far: {{total_stories}}

### Previously Generated Stories
Reference these IDs when specifying dependencies:
{{existing_stories}}

### Category Statistics
Use these to assign the next available ID for each category:
{{category_stats}}

## Your Task

1. Process each idea against the existing stories and design document
2. For each idea, determine:
   - Best category and ID
   - Appropriate priority (1-5)
   - Complexity (small, medium, large)
   - Dependencies on existing stories
   - Acceptance criteria
3. **Skip ideas that duplicate existing stories** — report them in `skipped`
4. Generate well-formed stories for non-duplicate ideas

## Output Format

```json
{
  "stories": [
    {
      "id": "CATEGORY-NNN",
      "title": "Short descriptive title",
      "content": "As a user, I want to...",
      "acceptance_criteria": ["Criterion 1", "Criterion 2"],
      "priority": 2,
      "category": "CATEGORY",
      "complexity": "medium",
      "dependencies": ["DEP-001"],
      "source": "ideate"
    }
  ],
  "skipped": [
    {"idea": "the duplicate idea text", "reason": "Duplicates AUTH-003"}
  ]
}
```

## Rules

1. Set `"source": "ideate"` on ALL generated stories
2. Use existing category ID conventions (check category_stats for next number)
3. Reference only existing story IDs (or IDs from this batch) in dependencies
4. Skip genuine duplicates — report them in `skipped`
5. Break large ideas into multiple stories if appropriate

## Completion Signal — CRITICAL RULES

When you have processed ALL ideas and have NOTHING left, output the signal on its own line:

[IDEATION_COMPLETE]

**IMPORTANT:**
- NEVER output `[IDEATION_COMPLETE]` if you are returning new stories in this response
- NEVER mention, reference, or discuss `[IDEATION_COMPLETE]` in your text
- The signal must appear ALONE on its own line, not inside a sentence
- Either output the signal (when truly done) or don't — say nothing about it
"""

REFINE_PROMPT = """\
# Refine Stories Mode

You are refining existing user stories based on a user instruction.

## Refinement Instruction

{{refinement_instruction}}

## Original Stories to Refine

IDs: {{original_story_ids}}

```json
{{original_stories_json}}
```

## Design Document

{{design_doc}}

## Existing Stories (DO NOT DUPLICATE)

Total stories generated so far: {{total_stories}}

### Previously Generated Stories
Reference these IDs when specifying dependencies:
{{existing_stories}}

### Category Statistics
Use these to assign the next available ID for each category:
{{category_stats}}

## Your Task

1. Apply the refinement instruction to the original stories above
2. Produce **replacement** stories that fulfil the instruction (split, merge, rewrite, etc.)
3. Preserve the intent and acceptance criteria from the originals, distributing them among the new stories as appropriate
4. Choose the best category and assign the next available ID for each new story
5. Set appropriate priority, complexity, and dependencies
6. **Skip** any result that would duplicate an existing story (check the existing stories list)

## Output Format

Return replacement stories as JSON:
```json
{
  "stories": [
    {
      "id": "CATEGORY-NNN",
      "title": "Short descriptive title",
      "content": "As a user, I want to...",
      "acceptance_criteria": ["Criterion 1", "Criterion 2"],
      "priority": 2,
      "category": "CATEGORY",
      "complexity": "medium",
      "dependencies": ["DEP-001"],
      "source": "refine"
    }
  ]
}
```

## Rules

1. Set `"source": "refine"` on ALL generated stories
2. Use existing category ID conventions (check category_stats for next number)
3. Reference only existing story IDs (or IDs from this batch) in dependencies
4. Preserve the original stories' intent — do not drop requirements
5. Distribute acceptance criteria from originals among new stories so nothing is lost
6. If the instruction says "split", produce multiple smaller stories from each original
7. If the instruction says "merge", combine originals into fewer stories
"""

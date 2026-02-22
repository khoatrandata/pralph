PLANNING_EXTRACT_PROMPT = """\
# Story Extraction Mode

You are analyzing design documents to extract user stories for implementation.

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

## Input Documents

The following input files are available. Use the Read tool to read any files you need:
{{inputs_list}}

## Your Task

1. Read any input documents listed above using the Read tool
2. Analyze the design document and input files thoroughly
3. Generate NEW stories (do not duplicate existing ones above)
4. For each story, provide:
   - **ID**: Use format CATEGORY-NNN (see category stats for next number)
   - A clear title
   - User story format: "As a [user], I want [feature] so that [benefit]"
   - Acceptance criteria (testable conditions)
   - Priority (1-5, where 1 is highest)
   - Category (uppercase: AUTH, API, DB, UI, INFRA, etc.)
   - Estimated complexity (small, medium, large)
   - **Dependencies**: Array of story IDs this depends on

## Output Format

Return your findings as JSON:
```json
{
  "stories": [
    {
      "id": "AUTH-043",
      "title": "Short descriptive title",
      "content": "As a user, I want to...",
      "acceptance_criteria": ["Criterion 1", "Criterion 2"],
      "priority": 1,
      "category": "AUTH",
      "complexity": "medium",
      "dependencies": ["AUTH-001", "AUTH-002"]
    }
  ],
  "notes": "Any observations about the documents"
}
```

## ID Assignment Rules

1. Look at category_stats to find the next available ID for each category
2. If category "AUTH" has next_id: 43, use "AUTH-043" for your first AUTH story
3. If you generate multiple stories in the same category, increment: AUTH-043, AUTH-044, etc.
4. For new categories not in category_stats, start at 001

## Dependency Guidelines

- **Infrastructure/foundation stories** typically have NO dependencies (they are the foundation)
- **Feature stories** depend on their infrastructure (e.g., "AUTH-005" depends on "AUTH-001" if AUTH-001 creates the user model)
- **UI stories** depend on their backend (e.g., "UI-003" depends on "API-002" for the endpoint it calls)
- **ONLY reference story IDs** that:
  1. Exist in "Previously Generated Stories" above, OR
  2. Are other stories in THIS batch you're generating
- If a dependency hasn't been created yet, note it in acceptance_criteria instead

## Guidelines

- Each story should be independently implementable (given its dependencies)
- Break large features into smaller stories
- Avoid duplicating stories that already exist
- Flag any ambiguous requirements for clarification
- Mark infrastructure/architecture stories with category "INFRA" or "ARCH"

## Completion Signal — CRITICAL RULES

When you have exhausted ALL stories and have NOTHING new to add, return an empty stories
array and output the signal on its own line:

```json
{"stories": [], "notes": "All requirements have been extracted."}
```

[GENERATION_COMPLETE]

**IMPORTANT:**
- NEVER output `[GENERATION_COMPLETE]` if you are returning new stories in this response
- NEVER mention, reference, or discuss `[GENERATION_COMPLETE]` in your text — do not explain
  whether you are or are not emitting it, do not say "I am not emitting the signal because..."
- The signal must appear ALONE on its own line, not inside a sentence
- Either output the signal (when truly done) or don't — say nothing about it
"""

PLANNING_RESEARCH_PROMPT = """\
# Research Mode

You are researching best practices and implementation approaches for the project.

## Context

Based on the design documents in the inputs directory, research relevant:
- Industry best practices
- Common implementation patterns
- Security considerations
- Performance optimizations

## Your Task

1. Identify areas where research would be valuable
2. Search for relevant information
3. Synthesize findings into actionable recommendations

## Output Format

Return your findings as JSON:
```json
{
  "topic": "What you researched",
  "findings": [
    {
      "insight": "Key finding",
      "source": "Where you found it",
      "applicability": "How it applies to our project"
    }
  ],
  "recommendations": [
    "Specific recommendation for implementation"
  ],
  "additional_stories": [
    {
      "id": "STORY-RES-001",
      "title": "Story suggested by research",
      "content": "As a user, I want...",
      "rationale": "Why this story was identified through research"
    }
  ]
}
```
"""

WEBGEN_REQUIREMENTS_PROMPT = """\
# Web-Generated Requirements Discovery

You are researching industry best practices to find requirements MISSING from the design document.

## Design Document Summary
{{design_doc_summary}}

## Existing Stories (DO NOT DUPLICATE)
Total: {{total_stories}}
{{existing_stories}}

## Category Statistics (for ID assignment)
{{category_stats}}

## Your Task

1. **Identify the domain** from the design document (e.g., "e-commerce", "healthcare", "fintech")
2. **Research** using WebSearch:
   - "{domain} application best practices {{current_year}}"
   - "{domain} security requirements"
   - "{domain} compliance regulations"
   - "common {domain} features users expect"
3. **Find gaps** - requirements NOT in existing stories
4. **Generate stories** for those gaps

## Output Format

```json
{
  "domain_identified": "e-commerce",
  "searches_performed": ["e-commerce best practices 2026", "..."],
  "gaps_found": [
    {"gap": "No rate limiting", "source": "OWASP guidelines"},
    {"gap": "Missing GDPR compliance", "source": "EU regulations"}
  ],
  "stories": [
    {
      "id": "SEC-045",
      "title": "Implement rate limiting",
      "content": "As a system administrator, I want rate limiting on API endpoints so that the system is protected from abuse.",
      "acceptance_criteria": ["Rate limit of 100 req/min per user", "429 response when exceeded", "Configurable limits"],
      "priority": 2,
      "category": "SEC",
      "complexity": "medium",
      "source": "webgen_requirements",
      "rationale": "OWASP recommends rate limiting for all public APIs"
    }
  ]
}
```

## Rules

1. Use standard CATEGORY-NNN IDs (check category_stats for next number)
2. Add `"source": "webgen_requirements"` to each story's metadata
3. Include `rationale` explaining WHY this requirement matters
4. If web search returns nothing useful, return empty stories array with explanation
5. DO NOT duplicate existing stories - check IDs and titles carefully
6. Focus on GAPS - things genuinely missing, not rephrasing existing stories

## Completion Signal — CRITICAL RULES

When you have exhausted ALL gaps and have NOTHING new to add, return an empty stories
array and output the signal on its own line:

```json
{"stories": [], "gaps_found": [], "notes": "All gaps have been addressed."}
```

[GENERATION_COMPLETE]

**IMPORTANT:**
- NEVER output `[GENERATION_COMPLETE]` if you are returning new stories in this response
- NEVER mention, reference, or discuss `[GENERATION_COMPLETE]` in your text — do not explain
  whether you are or are not emitting it
- The signal must appear ALONE on its own line, not inside a sentence
- Either output the signal (when truly done) or don't — say nothing about it
"""

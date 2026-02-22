CODE_GUARDRAILS = """\
# Code Quality Guardrails

## Security Requirements
- Never hardcode secrets or credentials
- Validate all user inputs
- Use parameterized queries for database access
- Implement proper authentication checks
- Sanitize outputs to prevent XSS
- Follow principle of least privilege

## Code Style
- Follow existing project conventions
- Use meaningful variable and function names
- Keep functions focused and small
- Add type hints where the project uses them
- Match existing indentation and formatting

## Testing Requirements
- Write tests for new functionality
- Cover happy path and error cases
- Test edge cases and boundary conditions
- Ensure tests are deterministic

## Error Handling
- Handle errors at appropriate levels
- Provide meaningful error messages
- Don't swallow exceptions silently
- Log errors appropriately

## Performance
- Avoid N+1 query patterns
- Use appropriate data structures
- Consider caching for expensive operations
- Don't premature optimize

## Anti-Patterns to Avoid
- God objects/functions that do everything
- Deep nesting (prefer early returns)
- Magic numbers without explanation
- Copy-paste code (extract to functions)
- Unused imports or dead code
"""

STORY_GUARDRAILS = """\
# Story Quality Guardrails

## Required Fields
Every story MUST include:
- Unique ID in CATEGORY-NNN format
- User story in proper format ("As a..., I want..., so that...")
- At least 2 acceptance criteria
- Valid priority (1-5)
- Category assignment

## Quality Rules

### Size
- Stories should be completable in 1-3 days
- If a story seems larger, break it down

### Dependencies
- No story should have more than 3 dependencies
- Dependencies must reference valid story IDs
- Avoid circular dependencies

### Categories
Infrastructure stories belong in:
- INFRA, ARCH, DB, AUTH, FND, SYS

Feature stories belong in:
- UI, API, FEAT

### Acceptance Criteria
- Must be testable (yes/no determination)
- Should be specific, not vague
- Include edge cases where relevant

## Anti-Patterns to Avoid
- Stories that are just "implement X" without user value
- Acceptance criteria that are implementation details
- Dependencies on stories that don't exist
- Multiple unrelated changes in one story
"""

DEFAULT_GUARDRAILS = """\
# Default Guardrails

## Code Quality
- Write clean, readable code with meaningful names
- Follow existing code style and patterns in the codebase
- Add comments only where logic isn't self-evident
- Keep functions focused and reasonably sized

## Safety
- Never introduce security vulnerabilities (injection, XSS, etc.)
- Validate inputs at system boundaries
- Handle errors gracefully
- Don't expose sensitive information in logs or errors

## Testing
- Test your changes before reporting success
- Consider edge cases and error conditions
- Verify existing functionality isn't broken

## Git
- Make atomic, focused commits
- Write clear commit messages
- Don't commit secrets, credentials, or sensitive data

## Communication
- Be clear about what you changed and why
- Report blockers or uncertainties promptly
- Ask for clarification when requirements are ambiguous
"""

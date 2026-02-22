# pralph

**Planned Ralph** — a multi-phase AI development workflow that orchestrates [Claude Code](https://docs.anthropic.com/en/docs/claude-code) externally to automate the full software development lifecycle — from design to implementation.

## Background

pralph is inspired by the official [Ralph](https://github.com/anthropics/ralph) plugin and [RalphX](https://github.com/anthropics/ralphx), which provide AI-driven product development workflows inside Claude Code. Unlike those tools, **pralph runs outside of Claude Code**, driving it as a subprocess. This means:

- **External orchestration** — pralph launches Claude Code invocations as child processes, managing sessions, streaming output, and coordinating multi-phase workflows from the outside.
- **Phase state persistence** — All state (design docs, stories, run logs) is tracked in a `.pralph/` directory, surviving across sessions and crashes.
- **Interactive takeover** — Press ESC during any Claude execution to drop into an interactive Claude Code session and resume manually.

> **Token usage warning:** pralph orchestrates multiple Claude Code sessions in a loop, each consuming tokens. A single `implement` run across a full backlog can use a significant amount of tokens. Use `--max-budget-usd` and `--max-iterations` to set limits, and monitor your usage.

## How it works

pralph breaks development into four phases:

### Phase 1: Plan

Creates a comprehensive design document through interactive conversation with Claude. Researches best practices via web search and generates coding guardrails.

### Phase 2: Stories

Extracts user stories from the design document (`stories`) or discovers missing requirements via web research (`webgen`). Stories are stored as structured JSONL with acceptance criteria, priority, complexity, and dependencies.

### Phase 2b: Add / Ideate / Refine

Manage stories on the fly:

- **`add`** — Turn a single idea into one well-formed story. Good for when something specific comes to mind mid-workflow. Use `--next` to flag it as priority 1 so it gets implemented next.
- **`ideate`** — Describe a high-level idea or feature area that was missed in the original design and let Claude break it down into multiple structured stories. Use this when you realize a broad concept was overlooked (e.g. "internationalization support") and want pralph to figure out the individual pieces.
- **`refine`** — Modify existing stories — split, merge, or rewrite them by ID or glob pattern.

### Phase 3: Implement

Autonomously implements stories one at a time from the backlog. Optionally runs a review loop after each implementation. Includes crash recovery — orphaned in-progress stories reset to pending on restart.

## Installation

**Prerequisites:** Python 3.10+, [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated.

```bash
git clone <repo-url> && cd my-ralph
./install.sh
```

This creates a virtualenv, installs dependencies, and adds `pralph` to your PATH.

## Usage

Run commands from inside any project directory. pralph stores its state in `.pralph/` within that directory.

```bash
# 1. Create a design document
pralph plan --user-prompt "Build a task management app with auth and real-time updates"

# 2. Extract stories from the design
pralph stories

# 3. Discover missing requirements via web research
pralph webgen

# 4a. Add a single idea as a story (--next = implement it first)
pralph add --idea "add dark mode support" --next

# 4b. Describe a broad idea — Claude splits it into stories
pralph ideate "add internationalization support with locale detection, translated UI strings, RTL layout, and date/currency formatting"

# Or point at a file with a longer description
pralph ideate --ideas-file ideas.txt

# 5. Refine existing stories
pralph refine -s AUTH-001 "split into login and registration"

# 6. Implement the backlog
pralph implement --review

# 7. Browse and edit stories in the web viewer
pralph viewer
```

### Global options

| Option | Default | Description |
|---|---|---|
| `--model` | `opus` | Model alias or full Claude model name |
| `--max-iterations` | `50` | Max loop iterations per phase |
| `--max-budget-usd` | — | Cost limit per Claude invocation |
| `--cooldown` | `5` | Seconds between iterations |
| `--verbose` | off | Show full Claude output |
| `--project-dir` | `.` | Target project directory |
| `--dangerously-skip-permissions` | off | Bypass Claude Code permission checks |
| `--extra-tools` | — | Additional MCP tools (comma-separated) |

### Project state

All state lives in `.pralph/` within your project:

```
.pralph/
  design-doc.md         # Design document (Phase 1)
  guardrails.md         # Coding standards and constraints
  stories.jsonl         # Story backlog
  status.jsonl          # Story status history
  phase-state.json      # Current phase progress
  run-log.jsonl         # Iteration log
  review-feedback/      # Per-story review notes
  prompts/              # Project-level prompt overrides
```

## Story viewer

```bash
pralph viewer            # opens http://localhost:8411
pralph viewer --port 9000 --no-open
```

A dark-themed web UI for browsing and managing your story backlog. Features:

- **Sidebar + detail panel** — click any story to see its full content, acceptance criteria, dependencies, and metadata.
- **Filtering** — filter by status, category, priority, or search by text.
- **In-place editing** — edit story fields (title, content, priority, status, etc.) directly in the browser and save back to disk.
- **Timeline view** — a Gantt-style visualization that lays out stories by dependency depth, with arrows showing dependency relationships.

## Customization

### Additional prompt files

These files live directly in `.pralph/` and provide extra context that gets appended to the built-in prompts. Create them when you want to steer Claude's behavior without replacing the entire prompt.

| File | Description |
|---|---|
| `guardrails.md` | Project-specific coding standards and constraints injected into every phase |
| `extra-tools.txt` | Additional MCP tools to enable (one per line), merged with `--extra-tools` CLI flag (see example below) |
| `plan-prompt.md` | Extra context appended to plan phase prompts |
| `implement-prompt.md` | Extra context appended to implement phase prompts |
| `review-prompt.md` | Project-specific review guidelines injected into the review template |

For example, adding a `guardrails.md` with "Always use TypeScript strict mode" will influence every phase without needing to touch the underlying prompts.

**extra-tools.txt example** — grant Claude access to MCP tools by listing specific tool names or using wildcards:

```
# Specific Jira tools
mcp__jira__get_issue
mcp__jira__search_issues
mcp__jira__create_issue

# Or allow all tools from the Jira MCP server
mcp__jira__*
```

You can also pass these on the command line: `pralph implement --extra-tools "mcp__jira__*"`

### Prompt template overrides

For full control, you can replace any built-in prompt entirely. Templates are resolved in order:

1. **Project-level** — `.pralph/prompts/<name>.md`
2. **Home-level** — `~/.pralph/prompts/<name>.md`
3. **Built-in default** — falls back to the bundled prompt

Use project-level overrides to tailor behavior for a specific project, or home-level overrides for personal defaults across all projects.

| Template | Phase | Description |
|---|---|---|
| `plan-initial.md` | Plan | First design document creation |
| `plan-iteration.md` | Plan | Iterative design refinement |
| `stories-extract.md` | Stories | Extracting stories from design doc |
| `stories-research.md` | Stories | Research mode for best practices |
| `stories-webgen.md` | Stories | Web-based requirements discovery |
| `add.md` | Add | Creating a single story from an idea |
| `ideate.md` | Ideate | Breaking a high-level idea into stories |
| `refine.md` | Refine | Splitting, merging, or rewriting stories |
| `implement.md` | Implement | Single story implementation |
| `implement-phase1-analyze.md` | Implement | Architecture-first grouping analysis |
| `implement-phase1.md` | Implement | Phase 1 batch implementation |
| `review.md` | Implement | Code review after implementation |

Templates use `{{variable}}` placeholders that are substituted at runtime (e.g. `{{design_doc}}`, `{{user_prompt}}`, `{{existing_stories}}`). Check the built-in defaults in `pralph/prompts/` to see which variables each template expects.

## Acknowledgements

pralph is heavily inspired by:

- **[Ralph](https://github.com/anthropics/ralph)** — Anthropic's official Claude Code plugin for AI-driven product development workflows.
- **[RalphX](https://github.com/anthropics/ralphx)** — The extended version of Ralph with multi-phase planning, story extraction, and implementation loops.

pralph reimplements and extends these ideas with an external orchestration approach, driving Claude Code as a subprocess rather than running as a plugin within it.

## License

MIT

# pralph

**Planned Ralph** — a multi-phase AI development workflow that orchestrates [Claude Code](https://docs.anthropic.com/en/docs/claude-code) externally to automate the full software development lifecycle — from design to implementation.

## Table of contents

- [Background](#background)
- [How it works](#how-it-works)
- [Installation](#installation)
- [Usage](#usage)
  - [Standard workflow](#standard-workflow)
  - [Adding stories later](#adding-stories-later)
  - [Justloop](#justloop)
  - [Compound learning](#compound-learning)
  - [Piping from stdin](#piping-from-stdin)
  - [Global options](#global-options)
  - [Command options](#command-options)
  - [Project state](#project-state)
- [Story viewer](#story-viewer)
- [Customization](#customization)
  - [Configuration](#configuration)
  - [Additional prompt files](#additional-prompt-files)
  - [Prompt template overrides](#prompt-template-overrides)
- [Acknowledgements](#acknowledgements)
- [License](#license)

## Background

pralph is inspired by the official [Ralph](https://github.com/anthropics/claude-code/tree/main/plugins/ralph-wiggum) plugin and [RalphX](https://github.com/jackneil/ralphx), which provide AI-driven product development workflows inside Claude Code. Unlike those tools, **pralph runs outside of Claude Code**, driving it as a subprocess. This means:

- **External orchestration** — pralph launches Claude Code invocations as child processes, managing sessions, streaming output, and coordinating multi-phase workflows from the outside.
- **Phase state persistence** — All state (design docs, stories, run logs) is tracked in a `.pralph/` directory, surviving across sessions and crashes.
- **Interactive takeover** — Press ESC during any Claude execution to drop into an interactive Claude Code session and resume manually.

> **Token usage warning:** pralph orchestrates multiple Claude Code sessions in a loop, each consuming tokens. A single `implement` run across a full backlog can use a significant amount of tokens. Use `--max-budget-usd` and `--max-iterations` to set limits, and monitor your usage.

## How it works

pralph breaks development into phases:

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

### Compound Learning

Inspired by the [compound-engineering-plugin](https://github.com/EveryInc/compound-engineering-plugin), compound learning captures non-trivial solutions as structured documentation after each implementation. Each documented solution compounds your team's knowledge — the first time you solve a problem takes research; document it, and the next occurrence takes minutes.

- **`--compound`** flag on `implement` — auto-captures learnings after each successful story
- **`compound`** standalone command — ad-hoc capture from recent work

Solutions are stored in `.pralph/solutions/` and automatically recalled during future plan and implement phases via keyword search.

#### Global compound learning

By default, solutions are scoped to the current project. Enable **global compound learning** to also save solutions to `~/.pralph/solutions/`, subdivided by auto-detected domain (e.g. `swift-ios`, `rust`, `docker`). This means learnings from one project automatically carry over to new projects in the same domain — no manual copying.

To opt in, set `global_compound` in your config (see [Configuration](#configuration)):

```json
// ~/.pralph/config.json
{
  "global_compound": true
}
```

**Recall is always global.** Even without `global_compound` enabled, plan and implement phases will search `~/.pralph/solutions/` for relevant learnings matching the project's detected domains. The setting only controls whether new solutions are *saved* globally.

**Domain detection** is automatic — pralph scans project files to determine domains:

| Files | Domain |
|---|---|
| `*.swift`, `Package.swift`, `*.xcodeproj` | `swift-ios` |
| `Cargo.toml`, `*.rs` | `rust` |
| `Dockerfile`, `docker-compose.yml` | `docker` |
| `*.py`, `pyproject.toml` | `python` |
| `*.ts`, `*.tsx`, `package.json` | `typescript` |
| `*.go`, `go.mod` | `go` |
| `*.tf` | `terraform` |
| ... | (40+ rules for common languages/platforms) |

A project can have multiple domains (e.g. a Rust service with Docker gets both `rust` and `docker` learnings). Override detection with `.pralph/domains.txt` (one domain per line) or the `--domain` CLI flag.

```
~/.pralph/solutions/
  swift-ios/
    index.jsonl
    build-errors/
    ui-bugs/
  rust/
    index.jsonl
    build-errors/
    runtime-errors/
```

## Installation

**Prerequisites:** Python 3.10+, [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated.

```bash
git clone <repo-url> && cd my-ralph
./install.sh
```

This creates a virtualenv, installs dependencies, and adds `pralph` to your PATH.

## Usage

Run commands from inside any project directory. pralph stores its state in `.pralph/` within that directory.

### Standard workflow

```bash
# 1. Create a design document
pralph plan --prompt "Build a task management app with auth and real-time updates"

# 2. Extract stories from the design
pralph stories

# 3. Discover missing requirements via web research
pralph webgen

# Browse and edit stories in the web viewer
pralph viewer

# 4. Implement the backlog (review is on by default)
pralph implement --compound --max-iterations 30
```

### Adding stories later

After the initial backlog is built, you can add more stories at any time — then run `implement` again to pick them up.

```bash
# Add a single idea as a story (--next = implement it first)
pralph add --prompt "add dark mode support" --next

# Describe a broad idea — Claude splits it into stories
pralph ideate "add internationalization support with locale detection, translated UI strings, RTL layout, and date/currency formatting"

# Or point at a file with a longer description
pralph ideate --ideas-file ideas.txt

# Refine existing stories — split, merge, or rewrite
pralph refine -s AUTH-001 "split into login and registration"

# Ad-hoc capture of learnings
pralph compound --prompt "Fixed CORS issue by adding middleware"
pralph compound --story-id AUTH-001
```

### Justloop

A simple loop for running any prompt to completion without the full plan/stories/implement workflow. Give it a task and pralph handles the rest — it wraps the prompt with proper end conditions, runs it in a loop, and stops when Claude signals the task is done.

```bash
# Positional arguments
pralph justloop "refactor the auth module to use JWT tokens"

# Multi-word prompts work naturally
pralph justloop fix all linting errors and update deprecated API calls

# Via --prompt flag
pralph justloop --prompt "add comprehensive test coverage for the utils module"

# Piped from stdin
gh issue view 42 --json body -q .body | pralph justloop

# Reset and re-run
pralph justloop --reset "migrate database schema to v2"
```

Each iteration has full tool access (Read, Write, Edit, Bash, Glob, Grep) and resume context from prior iterations. The loop terminates when Claude determines the task is fully complete, after 5 consecutive errors, or when max iterations is reached.

**Tip: review-driven fix loop** — A powerful pattern is to first run Claude Code's `/review` command on your codebase and have it write the issues to a file, then let justloop work through them one by one:

```bash
# Step 1: In Claude Code, run /review and ask it to save issues to a markdown file
#   e.g. "/review the codebase and write all issues to issues.md, ordered by severity"

# Step 2: Let justloop fix them
pralph justloop "Read issues.md. Pick the highest severity unresolved issue, fix it, then update issues.md to mark it as resolved."
```

Each iteration fixes one issue and updates the tracking file, giving you a clean audit trail. The loop stops when all issues are resolved.

### Compound learning

Inspired by the [compound-engineering-plugin](https://github.com/EveryInc/compound-engineering-plugin), compound learning captures non-trivial solutions as structured documentation after each implementation.

The value compounds over time. The first time Claude hits a tricky CORS config, it burns tokens researching. Document that solution, and the next project that needs CORS gets it right on the first iteration. Build errors, deployment quirks, library gotchas, auth patterns — every solution captured makes subsequent implementations faster, cheaper, and more reliable. Early runs are slow; later runs benefit from everything that came before.

Solutions are stored in `.pralph/solutions/` and automatically recalled during future plan and implement phases via keyword search — no manual lookup needed. With [global compound learning](#global-compound-learning) enabled, solutions are also saved to `~/.pralph/solutions/{domain}/` and automatically recalled in new projects that share the same domain.

```bash
# Auto-capture learnings after each successful story
pralph implement --compound

# Ad-hoc capture from recent work
pralph compound --prompt "Fixed CORS issue by adding middleware"
pralph compound --story-id AUTH-001
```

### Piping from stdin

All commands that accept `--prompt` also read from stdin when piped, making it easy to compose with other tools:

```bash
echo "Build a calculator app" | pralph plan
echo "add dark mode" | pralph add
echo "internationalization support" | pralph ideate
echo "split into login and registration" | pralph refine -s AUTH-001
echo "use vanilla JS" | pralph implement
echo "Fixed CORS issue by adding middleware" | pralph compound
echo "fix all linting errors" | pralph justloop

# Compose with other tools
cat requirements.txt | pralph plan
gh issue view 42 --json body -q .body | pralph add --next
```

Input is resolved in order: `--prompt` flag > stdin pipe > interactive prompt.

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
| `--domain` | auto | Override detected domains for global learning (repeatable) |

### Command options

#### `plan`

| Option | Default | Description |
|---|---|---|
| `--prompt` | — | Guidance for design doc creation |
| `--reset` | off | Reset phase state and start fresh |

#### `stories`

| Option | Default | Description |
|---|---|---|
| `--extract-weight` | `80` | Extract vs research weight (0–100) |
| `--reset` | off | Reset phase state and start fresh |

#### `webgen`

| Option | Default | Description |
|---|---|---|
| `--reset` | off | Reset phase state and start fresh |

#### `add`

| Option | Default | Description |
|---|---|---|
| `--prompt` | — | Brief idea to turn into a story (prompted if omitted) |
| `--next` | off | Priority 1 — implement next |
| `--anytime` | off | Let Claude pick priority |

#### `ideate`

Accepts ideas as positional arguments (e.g. `pralph ideate "idea one" "idea two"`).

| Option | Default | Description |
|---|---|---|
| `--ideas-file` | — | Path to ideas file (default: `.pralph/ideas.md`) |
| `--prompt` | — | Ideas as inline text |
| `--reset` | off | Reset phase state and start fresh |

#### `refine`

Accepts an optional positional instruction (e.g. `pralph refine -s AUTH-001 "split into login and registration"`).

| Option | Default | Description |
|---|---|---|
| `--prompt` | — | Refinement instruction (alternative to positional arg) |
| `-s`, `--story` | — | Story ID(s) to refine (repeatable) |
| `-p`, `--pattern` | — | Glob pattern to match story IDs (e.g. `I18N-*`) |

#### `implement`

| Option | Default | Description |
|---|---|---|
| `--story-id` | — | Implement a specific story |
| `--phase1` / `--no-phase1` | on | Architecture-first grouping |
| `--review` / `--no-review` | on | Run reviewer after each implementation |
| `--compound` / `--no-compound` | off | Capture learnings after each story |
| `--prompt` | — | Guidance for implementation (e.g. "use FastAPI") |
| `--reset` | off | Reset phase state and start fresh |

#### `justloop`

Accepts the task as positional arguments (e.g. `pralph justloop fix all linting errors`).

| Option | Default | Description |
|---|---|---|
| `--prompt` | — | Task prompt (alternative to positional args) |
| `--reset` | off | Reset phase state and start fresh |

#### `compound`

| Option | Default | Description |
|---|---|---|
| `--story-id` | — | Story ID to capture learnings from |
| `--prompt` | — | Description of what was done |

#### `reset-errors`

No options. Resets all stories with `error` status back to `pending` and clears the current phase's error state (`consecutive_errors`, `last_error`, and `completion_reason` if the phase was stopped due to errors). This unblocks a phase that halted after hitting too many consecutive errors.

```bash
pralph reset-errors
```

#### `viewer`

| Option | Default | Description |
|---|---|---|
| `--port` | `8411` | Port to serve on |
| `--no-open` | off | Don't auto-open browser |

### Project state

All state lives in `.pralph/` within your project:

```
.pralph/
  config.json           # Project-level config overrides (optional)
  domains.txt           # Override auto-detected domains (optional)
  design-doc.md         # Design document (Phase 1)
  guardrails.md         # Coding standards and constraints
  stories.jsonl         # Story backlog
  status.jsonl          # Story status history
  phase-state.json      # Current phase progress
  run-log.jsonl         # Iteration log
  review-feedback/      # Per-story review notes
  prompts/              # Project-level prompt overrides
  solutions/            # Compound learning knowledge base
    index.jsonl         # Lightweight index for fast keyword search
    build-errors/       # Categorized solution documents
    runtime-errors/
    ...
```

Global state lives in `~/.pralph/`:

```
~/.pralph/
  config.json           # User-wide config defaults
  prompts/              # User-wide prompt template overrides
  solutions/            # Global compound learning (domain-scoped)
    swift-ios/          # One directory per detected domain
      index.jsonl
      build-errors/
      ui-bugs/
    rust/
      index.jsonl
      runtime-errors/
    ...
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

<img src="docs/viewer-stories.png" alt="Story viewer — card view with detail panel" width="800">
<img src="docs/viewer-timeline.png" alt="Story viewer — timeline view with dependency arrows" width="800">

## Customization

### Configuration

pralph uses JSON config files for persistent settings. Config is resolved in order: project `.pralph/config.json` > user `~/.pralph/config.json` > defaults.

```json
// ~/.pralph/config.json
{
  "global_compound": true
}
```

| Key | Type | Default | Description |
|---|---|---|---|
| `global_compound` | `bool` | `false` | Save compound learnings to `~/.pralph/solutions/{domain}/` for cross-project reuse |

Set user-wide defaults in `~/.pralph/config.json`. Override per-project in `.pralph/config.json` (e.g. disable global saving for a throwaway project).

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
| `justloop.md` | Justloop | Task execution prompt with completion signal |
| `compound.md` | Compound | Solution capture after implementation |

Templates use `{{variable}}` placeholders that are substituted at runtime (e.g. `{{design_doc}}`, `{{user_prompt}}`, `{{existing_stories}}`). Check the built-in defaults in `pralph/prompts/` to see which variables each template expects.

## Acknowledgements

pralph is heavily inspired by:

- **[Ralph](https://github.com/anthropics/claude-code/tree/main/plugins/ralph-wiggum)** — Anthropic's official Claude Code plugin for AI-driven product development workflows.
- **[RalphX](https://github.com/jackneil/ralphx)** — The extended version of Ralph with multi-phase planning, story extraction, and implementation loops.

- **[compound-engineering-plugin](https://github.com/EveryInc/compound-engineering-plugin)** — Every Inc's compound learning plugin whose philosophy of documenting solutions to build institutional knowledge inspired pralph's compound learning feature.

pralph reimplements and extends these ideas with an external orchestration approach, driving Claude Code as a subprocess rather than running as a plugin within it.

## License

MIT

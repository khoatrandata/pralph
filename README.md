# pralph

**Planned Ralph** — a multi-phase AI development workflow that orchestrates [Claude Code](https://docs.anthropic.com/en/docs/claude-code) externally to automate the full software development lifecycle — from design to implementation.

## Table of contents

- [Background](#background)
- [How it works](#how-it-works)
- [Installation](#installation)
- [Usage](#usage)
  - [Standard workflow](#standard-workflow)
  - [Adding stories later](#adding-stories-later)
  - [Compound learning](#compound-learning)
  - [Querying project data](#querying-project-data)
  - [Piping from stdin](#piping-from-stdin)
  - [Global options](#global-options)
  - [Command options](#command-options)
  - [Project state](#project-state)
- [Story viewer](#story-viewer)
- [Customization](#customization)
  - [Additional prompt files](#additional-prompt-files)
  - [Prompt template overrides](#prompt-template-overrides)
- [Acknowledgements](#acknowledgements)
- [License](#license)

## Background

pralph is inspired by the official [Ralph](https://github.com/anthropics/claude-code/tree/main/plugins/ralph-wiggum) plugin and [RalphX](https://github.com/jackneil/ralphx), which provide AI-driven product development workflows inside Claude Code. Unlike those tools, **pralph runs outside of Claude Code**, driving it as a subprocess. This means:

- **External orchestration** — pralph launches Claude Code invocations as child processes, managing sessions, streaming output, and coordinating multi-phase workflows from the outside.
- **Multi-project DuckDB storage** — All structured data (stories, run logs, costs, phase state) is stored in a shared DuckDB database at `~/.pralph/pralph.duckdb`, supporting multiple projects simultaneously and enabling SQL queries across them.
- **Interactive takeover** — Press ESC during any Claude execution to drop into an interactive Claude Code session and resume manually.

> **Token usage warning:** pralph orchestrates multiple Claude Code sessions in a loop, each consuming tokens. A single `implement` run across a full backlog can use a significant amount of tokens. Use `--max-budget-usd` and `--max-iterations` to set limits, and monitor your usage with `pralph query --cost`.

## How it works

pralph breaks development into phases. Each phase enforces its prerequisites — running a phase out of order will error with a message pointing you to the required step.

`plan` → `stories` → `webgen` (optional) → `implement`

### Phase 1: Plan

Creates a comprehensive design document through interactive conversation with Claude. Researches best practices via web search and generates coding guardrails. The `--name` flag on `plan` sets the project identity used across all commands.

### Phase 2: Stories

Extracts user stories from the design document (`stories`) or discovers missing requirements via web research (`webgen`). Stories are stored in DuckDB with acceptance criteria, priority, complexity, and dependencies.

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

## Installation

**Prerequisites:** Python 3.10+, [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated.

```bash
git clone <repo-url> && cd my-ralph
./install.sh
```

This creates a virtualenv, installs dependencies (including DuckDB), and adds `pralph` to your PATH.

## Usage

Run commands from inside any project directory. pralph stores markdown artifacts in `.pralph/` within that directory and structured data in a shared DuckDB database at `~/.pralph/pralph.duckdb`.

### Standard workflow

```bash
# 1. Create a design document (--name sets the project identity)
pralph plan --name myapp --prompt "Build a task management app with auth and real-time updates"

# 2. Extract stories from the design
pralph stories

# 3. Discover missing requirements via web research
pralph webgen

# Browse and edit stories in the web viewer
pralph viewer

# 4. Implement the backlog (review is on by default)
pralph implement --compound --max-iterations 30

# 5. Check progress and costs
pralph query --report
pralph query --report --watch 10  # live dashboard while implement runs
```

The `--name` flag is only required the first time you run `plan` in a directory. It's stored in `.pralph/project.json` and all subsequent commands read it automatically. If you omit `--name`, pralph will prompt for it.

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

### Compound learning

Inspired by the [compound-engineering-plugin](https://github.com/EveryInc/compound-engineering-plugin), compound learning captures non-trivial solutions as structured documentation after each implementation.

The value compounds over time. The first time Claude hits a tricky CORS config, it burns tokens researching. Document that solution, and the next project that needs CORS gets it right on the first iteration. Build errors, deployment quirks, library gotchas, auth patterns — every solution captured makes subsequent implementations faster, cheaper, and more reliable. Early runs are slow; later runs benefit from everything that came before.

Solutions are stored in `.pralph/solutions/` and automatically recalled during future plan and implement phases via keyword search — no manual lookup needed.

```bash
# Auto-capture learnings after each successful story
pralph implement --compound

# Ad-hoc capture from recent work
pralph compound --prompt "Fixed CORS issue by adding middleware"
pralph compound --story-id AUTH-001
```

### Querying project data

All structured data is stored in DuckDB, queryable via `pralph query`. Use built-in shortcuts or write arbitrary SQL.

**Built-in shortcuts:**

```bash
pralph query --progress        # story counts by status
pralph query --cost            # cost breakdown by phase
pralph query --cost-per-story  # cost per story
pralph query --stories         # list all stories with status
pralph query --errors          # recent errors
pralph query --timeline        # implementation timeline
pralph query --projects        # all registered projects
pralph query --report          # full progress report (phases, stories, costs, active work)
pralph query --report --watch 10  # auto-refresh every 10 seconds
```

All query commands use a read-only database snapshot, so they work safely while another pralph process is running.

**Custom SQL:**

```bash
pralph query "SELECT id, title, status FROM stories WHERE priority = 1"
pralph query "SELECT phase, SUM(cost_usd) FROM run_log GROUP BY phase"
```

**Output formats:**

```bash
pralph query --cost --format table  # default, aligned columns
pralph query --cost --format csv    # CSV output
pralph query --cost --format json   # JSON output
```

**Available tables:** `projects`, `stories`, `status_log`, `run_log`, `phase_state`, `phase1_analysis`, `solutions_index`

### Piping from stdin

All commands that accept `--prompt` also read from stdin when piped, making it easy to compose with other tools:

```bash
echo "Build a calculator app" | pralph plan --name calc
echo "add dark mode" | pralph add
echo "internationalization support" | pralph ideate
echo "split into login and registration" | pralph refine -s AUTH-001
echo "use vanilla JS" | pralph implement
echo "Fixed CORS issue by adding middleware" | pralph compound

# Compose with other tools
cat requirements.txt | pralph plan --name myproject
gh issue view 42 --json body -q .body | pralph add --next
```

Input is resolved in order: `--prompt` flag > stdin pipe > interactive prompt.

### Global options

- `--model` (default: `opus`) — Model alias or full Claude model name
- `--max-iterations` (default: `50`) — Max loop iterations per phase
- `--max-budget-usd` — Cost limit per Claude invocation
- `--cooldown` (default: `5`) — Seconds between iterations
- `--verbose` — Show full Claude output
- `--project-dir` (default: `.`) — Target project directory
- `--dangerously-skip-permissions` — Bypass Claude Code permission checks
- `--extra-tools` — Additional MCP tools (comma-separated)

### Command options

#### `plan`

- `--name` — Project name, used as the project identifier across sessions (required on first run, prompted if omitted)
- `--prompt` — Guidance for design doc creation
- `--reset` — Reset phase state and start fresh

#### `stories`

- `--extract-weight` (default: `80`) — Extract vs research weight (0-100)
- `--reset` — Reset phase state and start fresh

#### `webgen`

- `--reset` — Reset phase state and start fresh

#### `add`

- `--prompt` — Brief idea to turn into a story (prompted if omitted)
- `--next` — Priority 1 — implement next
- `--anytime` — Let Claude pick priority

#### `ideate`

Accepts ideas as positional arguments (e.g. `pralph ideate "idea one" "idea two"`).

- `--ideas-file` — Path to ideas file (default: `.pralph/ideas.md`)
- `--prompt` — Ideas as inline text
- `--reset` — Reset phase state and start fresh

#### `refine`

Accepts an optional positional instruction (e.g. `pralph refine -s AUTH-001 "split into login and registration"`).

- `--prompt` — Refinement instruction (alternative to positional arg)
- `-s`, `--story` — Story ID(s) to refine (repeatable)
- `-p`, `--pattern` — Glob pattern to match story IDs (e.g. `I18N-*`)

#### `implement`

- `--story-id` — Implement a specific story
- `--phase1` / `--no-phase1` (default: on) — Architecture-first grouping
- `--review` / `--no-review` (default: on) — Run reviewer after each implementation
- `--compound` / `--no-compound` (default: off) — Capture learnings after each story
- `--prompt` — Guidance for implementation (e.g. "use FastAPI")
- `--reset` — Reset phase state and start fresh

#### `compound`

- `--story-id` — Story ID to capture learnings from
- `--prompt` — Description of what was done

#### `reset-errors`

No options. Resets all stories with `error` status back to `pending` and clears the current phase's error state (`consecutive_errors`, `last_error`, and `completion_reason` if the phase was stopped due to errors). This unblocks a phase that halted after hitting too many consecutive errors.

```bash
pralph reset-errors
```

#### `viewer`

- `--port` (default: `8411`) — Port to serve on
- `--no-open` — Don't auto-open browser

#### `query`

- `--progress` — Story progress by status
- `--cost` — Cost breakdown by phase
- `--stories` — List all stories with status
- `--cost-per-story` — Cost per story
- `--errors` — Recent errors
- `--timeline` — Implementation timeline
- `--projects` — All registered projects (no project context needed)
- `--report` — Full progress report (phases, stories, costs, active work)
- `--watch SECONDS` — Auto-refresh every N seconds (use with `--report`)
- `--all-projects` — Hint for custom SQL across all projects
- `--format` (`table` | `csv` | `json`, default: `table`) — Output format
- Positional `SQL` argument — Arbitrary SQL query

### Project state

Each project stores markdown artifacts in `.pralph/` within the project directory, and structured data in a shared DuckDB database at `~/.pralph/pralph.duckdb`.

**Local files** (in `.pralph/`):

- `project.json` — Project identity (project_id)
- `design-doc.md` — Design document (Phase 1)
- `guardrails.md` — Coding standards and constraints
- `review-feedback/` — Per-story review notes
- `prompts/` — Project-level prompt overrides
- `solutions/` — Compound learning knowledge base (markdown files)

**DuckDB tables** (in `~/.pralph/pralph.duckdb`):

- `projects` — Registered projects with name and creation time
- `stories` — Story backlog with status, priority, dependencies, acceptance criteria
- `status_log` — Append-only history of story status changes
- `run_log` — Per-iteration log with phase, cost, tokens, duration
- `phase_state` — Current progress of each phase
- `phase1_analysis` — Architecture-first grouping data
- `solutions_index` — Searchable index of compound learning solutions

All DuckDB tables are keyed by `project_id`, so multiple projects coexist in the same database. Existing projects that used JSONL files are automatically migrated to DuckDB on first access (originals backed up as `.bak` files).

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

### Additional prompt files

These files live directly in `.pralph/` and provide extra context that gets appended to the built-in prompts. Create them when you want to steer Claude's behavior without replacing the entire prompt.

- `guardrails.md` — Project-specific coding standards and constraints injected into every phase
- `extra-tools.txt` — Additional MCP tools to enable (one per line), merged with `--extra-tools` CLI flag (see example below)
- `plan-prompt.md` — Extra context appended to plan phase prompts
- `implement-prompt.md` — Extra context appended to implement phase prompts
- `review-prompt.md` — Project-specific review guidelines injected into the review template

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

Available templates:

- `plan-initial.md` — First design document creation
- `plan-iteration.md` — Iterative design refinement
- `stories-extract.md` — Extracting stories from design doc
- `stories-research.md` — Research mode for best practices
- `stories-webgen.md` — Web-based requirements discovery
- `add.md` — Creating a single story from an idea
- `ideate.md` — Breaking a high-level idea into stories
- `refine.md` — Splitting, merging, or rewriting stories
- `implement.md` — Single story implementation
- `implement-phase1-analyze.md` — Architecture-first grouping analysis
- `implement-phase1.md` — Phase 1 batch implementation
- `review.md` — Code review after implementation
- `compound.md` — Solution capture after implementation

Templates use `{{variable}}` placeholders that are substituted at runtime (e.g. `{{design_doc}}`, `{{user_prompt}}`, `{{existing_stories}}`). Check the built-in defaults in `pralph/prompts/` to see which variables each template expects.

## Acknowledgements

pralph is heavily inspired by:

- **[Ralph](https://github.com/anthropics/claude-code/tree/main/plugins/ralph-wiggum)** — Anthropic's official Claude Code plugin for AI-driven product development workflows.
- **[RalphX](https://github.com/jackneil/ralphx)** — The extended version of Ralph with multi-phase planning, story extraction, and implementation loops.

- **[compound-engineering-plugin](https://github.com/EveryInc/compound-engineering-plugin)** — Every Inc's compound learning plugin whose philosophy of documenting solutions to build institutional knowledge inspired pralph's compound learning feature.

pralph reimplements and extends these ideas with an external orchestration approach, driving Claude Code as a subprocess rather than running as a plugin within it.

## License

MIT

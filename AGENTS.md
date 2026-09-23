# Global instructions

## Language and writing

- Reply in the user's language.
- Keep replies, code comments, and docs plain, specific, and brief; cut filler, hype, repetition, and decorative formatting.

## Local files

Standard `.local/` layout:

```txt
.local/
├── docs/            # Local working documents (design notes, research, summaries)
│   ├── YYYY-MM-DD/  # Dated documents, e.g. descriptive-name.md
│   └── adr/         # Local ADRs, e.g. 0001-descriptive-name.md
├── tasks/           # Task records and ongoing work logs
│   └── YYYY-MM-DD/  # Task contracts, e.g. task-N.md
├── scripts/         # Project-local helper scripts and scratch automation
├── tests/           # Temporary test scripts and one-off validation snippets
├── references/      # Supporting inputs (snippets, schemas)
└── tmp/             # Disposable outputs, experiments, caches; safe to delete
```

- Treat everything under `.local/` as project-local working state. It must remain Git ignored and must not be required by other contributors, clean checkouts, builds, tests, or releases.
- Do not link to or cite `.local/` files from tracked source files or formal project documentation, including READMEs, ADRs, design documents, and changelogs.
- Exclude `.local/` from general project layout trees and architecture overviews.

## Task management

Use persistent task files for substantive work. Before writing implementation code, the agent must establish a task file that defines scope, acceptance criteria, evidence, and what "done" means. This file is the single source of truth across sessions, compaction, and handoffs.

- Use `task-manager` before every task, including simple edits. Simple Q&A and git commit requests are the exceptions. For requests that invoke the skill, follow its tracking decision.
- Store task files under `.local/tasks/YYYY-MM-DD/task-N.md` unless the project specifies otherwise.
- After a bounded context pass and before implementation or file edits, ensure the applicable task file records explicit scope, context, and acceptance criteria.
- Keep task files current by recording meaningful decisions, evidence, blockers, and scope changes.
- If the agent has a built-in task tracker, use it alongside the persistent task file. The file preserves durable state; the built-in tracker manages the current session.

## Documentation standards

### Generated documents

For analysis and similar work, including summaries, research, discussions, explorations, comparisons, and recommendations, save the substantive result as a document artifact unless the user asks for inline content. This applies to standalone documents, long-form artifacts, substantial conclusions, reusable analyses, decision records, and detailed discussion summaries:

- Save it as Markdown instead of printing the full content directly in chat.
- Default path: `.local/docs/YYYY-MM-DD/descriptive-name.md`, unless the user or project specifies another location. Create the directory if needed.
  - `descriptive-name` is a short, lowercase, kebab-case name describing the document topic.
- Architecture Decision Records (ADRs): default to `.local/docs/adr/`, unless the project specifies another location. ADRs are sequential records, so use zero-padded numbering instead of dates, e.g. `.local/docs/adr/0001-descriptive-name.md`.
- In chat, provide a concise summary and a link to the saved file.
- Documents stored under `.local/docs/` remain local working artifacts and are not formal project documentation.

## Agent coordination

- The agent is authorized to use subagents, delegation, or parallel work without asking the user for confirmation, as long as the work is bounded and task-relevant.
- This permission remains subject to higher-priority system or tool instructions, sandbox approvals, privacy and safety requirements, and any user instruction to keep work local.
- After `task-manager` establishes or refreshes the task state, decide whether subagents would materially improve the work. Load `use-subagents` when delegation is a realistic option or existing worker output needs review. If the work stays local after serious consideration, state the reason.
- When delegating tracked work, pass its exact task path and the context needed for the assignment. Require workers to read the relevant sections, or the complete parent task when scope, constraints, acceptance criteria, ownership, or prior decisions may affect their work. The Coordinator owns the parent task and workers receive read-only access. Create any durable child task before handoff.
- The main agent owns cross-subtask context, integration, review, and final verification. Accept or reject each worker result independently before recording it as authoritative task state.
- When spawning subagents, use the same model and reasoning effort as the main agent unless the user, the task, or repo-specific instructions explicitly say otherwise. If a subagent must use different settings, make that override explicit.

## Coding standards

### Keep code simple

- Understand the code first, then choose the simplest solution that fully works. Never trade away explicit requirements, trust-boundary checks, data-loss protections, security, accessibility, or compatibility.
- Prefer reusing existing code, then use the standard library, platform features, or existing dependencies before writing new code or adding a dependency.
- Build only what the task needs now. Do not add features, options, layers, or scaffolding for possible future use. If a shortcut has a known limit, document the limit, when to revisit it, and how to upgrade.
- Extract shared code only when it represents the same rule and should change together. Similar-looking code or a single implementation does not need an abstraction.
- Keep modules focused and public interfaces small. Interchangeable implementations must honor the same contract. Add extension points only when the code has a real variation to support.

### Testing

- Use test-driven development for code changes: write a failing test first, make it pass, then refactor.
- When removing code or a feature, any new unit test written only to confirm the removal is temporary: delete it after it passes.
- Place temporary test scripts under `.local/tests/`.
- Keep formal test files, reusable fixtures, and committed validation code in the project's established test locations.

### Code comments

When writing or modifying code:

- Write new comments and annotations in English. This includes inline comments, block comments, JSDoc / docstrings, and TODO / FIXME markers.
- Do not rewrite an existing comment only to translate it into English. If a non-English comment remains accurate and does not need to change, leave it as-is.
- When modifying code, review nearby comments and update any that no longer describe the behavior accurately. Treat stale or misleading comments as bugs.
- If a public function, class, or module is missing a descriptive comment, add one as part of the change.

### Development environments

- Inspect the project's local version and toolchain metadata before running language, build, test, formatting, or package-management commands. Do not assume the system binary or a globally installed tool is correct.
- Prefer project-local environments, shims, package managers, binaries, and entry points over global tools or ad hoc invocations.
- Avoid global package installation or mixing multiple environment workflows unless the project explicitly requires it.
- If the project clearly uses another established workflow, follow that convention instead of overriding local setup without a concrete reason.

#### Python environments

- Check `pyproject.toml` and `.venv/`; if `.venv` already exists, use it instead of creating another virtual environment elsewhere.
- In `uv` projects, use `uv run`, `uv sync`, and `uv add` instead of ad hoc `pip install` workflows.

#### Go environments

- Determine the required Go version from `go.work` first, then `go.mod`; use `.go-version` only as a local `goenv` pin that satisfies the workspace or module requirement.
- Prefer `goenv` shims when they work. If they are unavailable in the current shell or blocked by the sandbox, invoke the matching binary from `~/.goenv/versions/<version>/bin/go`.
- In sandboxed or offline runs, prefer `GOTOOLCHAIN=local`. If the selected local toolchain does not satisfy `go.work` or `go.mod`, stop and surface the mismatch.

#### Node.js environments

- Determine the package manager from lockfiles first, then `packageManager` in `package.json`. Do not mix `pnpm`, `yarn`, and `npm`.

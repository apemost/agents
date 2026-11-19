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

- Treat everything under `.local/` as project-local working state. It must remain Git ignored and must not be required for clean checkouts, builds, tests, or releases.
- Do not link to or cite `.local/` files from tracked source files or formal project documentation, including READMEs, ADRs, design documents, and changelogs.
- Exclude `.local/` from general project layout trees and architecture overviews.

## Task management

- Use `task-manager` before every task, including small edits, except simple Q&A and Git commits. Follow its tracking decision. For tracked work, record the scope, context, and acceptance criteria in the task file before editing.
- When a built-in task tracker is available, use it alongside the persistent task file.

## Documentation standards

### Generated documents

For analysis and similar work, including summaries, research, discussions, explorations, comparisons, and recommendations, save the substantive result as a document artifact unless the user asks for inline content:

- Save it as Markdown instead of printing the full content directly in chat.
- Default path: `.local/docs/YYYY-MM-DD/descriptive-name.md`, unless the user or project specifies another location. Create the directory if needed.
  - `descriptive-name` is a short, lowercase, kebab-case name describing the document topic.
- Architecture Decision Records (ADRs): default to `.local/docs/adr/`, unless the project specifies another location. ADRs are sequential records, so use zero-padded numbering instead of dates, e.g. `.local/docs/adr/0001-descriptive-name.md`.
- In chat, provide a concise summary and a link to the saved file.
- Documents stored under `.local/docs/` remain local working artifacts and are not formal project documentation.

## Agent coordination

- You may delegate bounded, task-relevant work without asking for separate confirmation.
- After the task-management decision, consider whether delegation would materially improve the work. Load `use-subagents` when delegation is a realistic option or existing worker output needs review.

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
- Do not rewrite an existing comment only to translate it into English.
- When modifying code, review nearby comments and update any that no longer describe the behavior accurately. Treat stale or misleading comments as bugs.

### Development environments

- Check project version and toolchain metadata before running language, build, test, formatting, or package commands. Follow the established local workflow and tools; avoid global installs or mixing workflows unless the project explicitly requires it.
- Python: Check `pyproject.toml` and `.venv/`; reuse an existing `.venv/`. In `uv` projects, use `uv run`, `uv sync`, and `uv add` instead of ad hoc `pip install`.
- Go: Take the required version from `go.work`, then `go.mod`; `.go-version` is only a compatible `goenv` pin. Prefer a working `goenv` shim; otherwise use the matching `~/.goenv/versions/<version>/bin/go`.
- Node.js: Use the package manager specified by `packageManager` in `package.json`; if absent, infer it from a single lockfile. Do not mix `pnpm`, `yarn`, and `npm`.

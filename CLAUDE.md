# burnie: project context

## What this is

A local cost analytics tool for Claude Code. Reads `~/.claude/projects/**/*.jsonl` session transcripts and surfaces token costs.

Python package (src-layout), installed/run via `uv`/`uvx`/`pip`, published as `burnie` on PyPI. Ported from an earlier Node.js implementation because `burnie` was already taken on npm.

## Main component

**CLI / report (`src/burnie/cli.py`)**
- Scans all local session files, generates an HTML report (`burnie-report.html`)
- Visual breakdown: total spend, daily avg, cache savings, cost by model
- Per-session detail: context growth, tool usage, agent calls, repeated operations
- Tooltips on non-obvious metrics, and a link to `claude.ai/new#settings/usage`
- `src/burnie/report.py` builds the HTML. The CSS and client-side JS (Chart.js-based) inside it are plain static strings, not templated, so edit them as HTML/JS directly

## Data source

Claude Code session files: `~/.claude/projects/<encoded-path>/<session-id>.jsonl`

Each line is a JSON event. Relevant types:
- `assistant`: has `message.usage` with `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`
- `user`: user messages and tool results
- `ai-title`: session title

Encoded path: project's absolute path with `/` replaced by `-`.

## Pricing

Stored in `src/burnie/pricing.py`, the single source of truth. Includes `PRICING_UPDATED` date shown in the report. Use the `/burnie-update` skill to refresh from `anthropic.com/pricing`.

## Skill install

`burnie --install-skill` (`cli.py::_install_skill`): with a repo checkout on disk (`_repo_skills_dir`, detected via `__file__`, which holds true for editable installs and source runs), symlinks `skills/burnie` and `skills/burnie-update` into `~/.claude/skills/` so edits show up live. `burnie-update` edits `src/burnie/pricing.py` in place, so it's only included here, alongside the source tree. Without a checkout (plain PyPI/`uv` install), copies just `skills/burnie/SKILL.md` from the wheel, bundled there via `[tool.hatch.build.targets.wheel.force-include]` in `pyproject.toml`.

`metadata.version` in both `SKILL.md` files is a plain string, bumped by hand alongside `pyproject.toml`'s `version`, the same convention as `PRICING_UPDATED`.

## CI / Releasing

`.github/workflows/ci.yml` builds and smoke-tests the wheel on every push/PR. `.github/workflows/release.yml` drafts a GitHub Release from `pyproject.toml`'s version on every push to `main`, and publishes to PyPI via Trusted Publishing when that draft is published. `src/burnie/__init__.py` reads `__version__` from installed metadata at runtime, so it's not a manual bump point.

## Key decisions

- No backend, no API calls: everything runs locally on the JSONL files
- Python, packaged with `hatchling`, distributed via PyPI/`uv`
- Pricing in one file (`src/burnie/pricing.py`) so updates don't touch report logic

## Non-goals

- Not a billing tool (no API key, no Anthropic account access)
- Not a real-time token counter during generation
- Not a cloud service

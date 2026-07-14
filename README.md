<p align="center">
  <img src="https://raw.githubusercontent.com/czoido/burnie/main/burnie.png" alt="Burnie" width="200" />
</p>

<p align="center"><em>Some agents just want to watch your tokens burn.</em></p>

---

**burnie** is a local cost analytics tool for Claude Code. It reads the session transcripts Claude Code stores on disk and gives you a clear picture of where your money is going: no API key, no cloud, just your files.

## Features

- **Report:** visual HTML breakdown of token costs across all your Claude Code sessions: total spend, daily average, cache savings, cost by model
- **Inspect:** drill into any session to see what drove the cost: context growth over time, tool call patterns, agent spawns, repeated operations
- **Analyze:** the `/burnie` skill runs a condensed report through Claude for a written, reasoned take on where your cost is coming from and what (if anything) to do about it

## Install

```bash
# run without installing
uvx burnie

# or install as a persistent command
uv tool install burnie
```

No `uv`? `pip install burnie` works too.

Then get the `/burnie` skill into Claude Code:

```bash
burnie --install-skill
```

## Usage

```bash
# Generate HTML report
burnie

# Highlight a specific session in the report
burnie --session <session-id>

# Markdown report instead of HTML
burnie --markdown

# Condensed, LLM-readable report printed to stdout (what the `/burnie` skill uses)
burnie --raw --session <session-id>
```

The report is written to `burnie-report.html` in the current directory and opened in your browser. `--markdown` writes `burnie-report.md` instead. `--raw` prints straight to stdout with no file.

## How it works

Claude Code writes every session to a JSONL file at:

```
~/.claude/projects/<encoded-project-path>/<session-id>.jsonl
```

Each assistant turn includes token usage. burnie reads those files, applies current model pricing (bundled with the package and updated periodically as Anthropic changes its rates), and surfaces the data.

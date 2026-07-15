<p align="center">
  <img src="https://raw.githubusercontent.com/czoido/burnie/main/burnie.png" alt="Burnie" width="200" />
</p>

<p align="center"><em>Some agents just want to watch your tokens burn.</em></p>

---

**burnie** is a local cost analytics tool for Claude Code. It reads the session transcripts Claude Code stores on disk and tells you not just *how much* your sessions cost, but whether a given session was **abnormally** expensive, and what you can honestly say about *why*.

<p align="center">
  <img src="https://raw.githubusercontent.com/czoido/burnie/main/assets/report.png" alt="burnie report" width="900" />
</p>

The hard part of a cost report isn't adding up tokens. It's not misleading you once you have the total. burnie judges a session against the ones that are actually comparable (same project, same model, similar length), so "expensive" means expensive *for work like this*, not just expensive against an average that the priciest sessions have already skewed. When it points at something, it stays careful about what it's actually claiming. It tells you *when* cost landed rather than inventing *why*. It won't call a repeated read "waste" when all the data proves is that it repeated. It won't quote a saving it can't compute. The result is a report you can act on without second-guessing whether the numbers are telling you a story that isn't there.

## Features

- **Report:** a visual HTML breakdown of token costs across all your Claude Code sessions, covering total spend, daily average, cache savings, and cost by model and by project.
- **Inspect:** drill into any session to see what drove the cost, including context growth over time, tool call patterns, agent spawns, repeated operations, and how it compares to similar sessions.
- **Analyze:** the `/burnie` skill runs a condensed report through Claude for a written, reasoned take on where your cost is coming from and what (if anything) is actually worth changing.

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

## Cost & privacy

- **The dollar figures are API-equivalent costs**, computed from Anthropic's published API rates for each model. They are *not* necessarily what you're billed on a Pro or Max plan. Treat them as "what this usage would cost at API prices," useful for comparing sessions against each other, not as an invoice.
- **The report is generated entirely on your machine.** No API key, no account access, no data leaves your computer. burnie only reads the JSONL files Claude Code already wrote to disk.
- **The optional `/burnie` skill passes the condensed report to your own Claude Code session**, the one you're already running, so Claude can analyze it. That stays local to your session, and it isn't a call to any burnie backend (there isn't one).

## How it works

Claude Code writes every session to a JSONL file at:

```
~/.claude/projects/<encoded-project-path>/<session-id>.jsonl
```

Each assistant turn includes token usage. burnie reads those files, applies current model pricing (bundled with the package and updated periodically as Anthropic changes its rates), and surfaces the data.

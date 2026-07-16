---
name: burnie
description: Generate and analyze a Claude Code session cost report. Use when the user asks about session costs, token spend, expensive sessions, or wants a cost breakdown of their Claude Code usage.
compatibility: Designed for Claude Code. Requires Python 3.9+ (run via `uvx burnie` or an installed `burnie`).
allowed-tools: Bash
metadata:
  version: "0.1.3"
---

Generate a Claude Code cost analytics report and provide a reasoned analysis of where the money is going and how to reduce it.

## Steps

### 1. Generate the raw report

Run burnie in `--raw` mode, passing the current session ID so it's marked in the output:

```bash
burnie --raw --session $CLAUDE_CODE_SESSION_ID
```

This reads all session files from `~/.claude/projects/**/*.jsonl`, computes costs, and prints a condensed, annotated report straight to stdout: no file, no browser. Each metric carries an inline one-line explanation of what it means, so read the numbers directly. You don't need extra context to interpret them.

The report has these sections: SUMMARY, TECHNICAL CONTEXT (cache stats: background, rarely worth leading with), GLOBAL PERCENTILES (p50/p75/p90/p95/max for cost, turns, peak context, and each cost component: the real baseline, since the average is skewed by the same expensive sessions you're trying to explain), COST BY COMPONENT, COST BY MODEL, COST BY PROJECT, CURRENT SESSION (this session's own numbers, its percentile rank, its COMPARABLE SESSIONS/peers with cohort size and quality, and its COST CURVE): **only present when `--session` matched a real session. If you ran this without `--session`, or the id didn't match anything on disk, there is no current session, no peers, and no cost curve to analyze, so say so and stick to the global patterns below instead of inventing a per-session read**, TOP 20 MOST EXPENSIVE SESSIONS (global cost patterns, not a baseline for "normal": includes `turns`/`peak_ctx` per session now, useful for comparing shape across the list), RECURRING FAILURES, FREQUENTLY ACCESSED TARGETS (background, not a finding: targets hit repeatedly by path/command/pattern), and MCP SERVERS (configured MCP servers cross-referenced against actual `mcp__` tool calls observed in these sessions — only present when at least one MCP server is configured).

### 2. Provide a reasoned analysis

Write for someone who wants to know what to do, not someone who wants to see the metrics. The raw report's section order is for you to reason with. The response you write follows a different order, built around the conclusion, not the data.

**Output structure (always these three parts, in this order):**

1. **Current session.** If the report has no CURRENT SESSION block (no `--session` was passed, or the id didn't match a session on disk), say plainly that there's no data for the current session and skip straight to part 2. Don't invent a per-session read from the global/top-20 data. Otherwise, open with a plain-language verdict, before any number: does this session show a specific problem, or not? If not, say so directly in the first sentence ("no specific problem here, no action needed based on this session"). Don't make the reader infer that from a pile of percentiles. Then 1-3 sentences of supporting context (peer comparison, cost curve shape, compactions), written as comparisons a non-technical reader can picture, not as statistics. **Never write "P84," "percentile 45," or any raw percentile/rank number in the response**. Translate every one into a plain comparison instead: "more expensive than most sessions overall, but ordinary for this project," "close to the middle of what similar sessions cost," "well above what its peers typically cost." The percentile numbers are for you to reason with while writing the analysis. They should never appear as numbers in what the user reads. Close with one line on what to do (often, correctly, "nothing for now").
2. **Recommended actions.** Patterns across the whole history, not this session specifically. Cap at 3, ordered by impact, but don't pad to reach 3. Zero, one, or two items is a valid, often correct result. Only include an item if the data supports it. Each one has exactly four parts:
   - **Conclusion:** what's happening, plain language, no jargon.
   - **Evidence:** the specific numbers behind it.
   - **Action:** either a specific behavior change the user can go make (a command, a workflow change) when the report actually links the pattern to that change, or, when it doesn't, an explicit **"Worth inspecting"** framing that names what to go look at before deciding whether to change anything. Never invent a fix for a pattern that's only been observed, not diagnosed, and never pad with vague advice like "reduce token usage."
   - **Impact:** high/medium/low, and a confidence label when the causal link isn't proven (e.g. "157 rereads of file X" is high-confidence evidence of a *pattern*, only medium-confidence that it's *avoidable*, because the file may have changed between reads).
3. **Session data.** A short block of plain facts at the very end (cost, turns, model, context range, compactions, files touched) for reference, not part of the narrative above it. If there's no CURRENT SESSION block, just write "not available" here instead of omitting the part.

Keep these separate throughout. Never blend them: what's true about *this session* vs. what's true about *the whole history*, and an *observation* (a pattern exists) vs. a *recommendation* (do this about it).

**Rules (follow all of them):**
- State the verdict in plain language first. Percentiles/ranks/cohort numbers back it up, they don't replace it.
- Compare the current session to its **peers** (same project + same model, similar turn count) first. That's the real "is this normal" comparison. Use GLOBAL PERCENTILES for broader context. Use the TOP 20 list only to say whether a global pattern is widespread ("this shows up in N of the 20 priciest sessions"), never as a stand-in for "normal," and never as a reason to flag the *current* session.
- If `comparison_quality=low` (thin cohort, so check `cohort_candidates`), say so explicitly and treat any cohort comparison as low-confidence rather than drawing firm conclusions from it.
- Never call `spend_after_first_compaction` "savings" or "recoverable". It explains *where* the cost came from, not money you can get back.
- Never infer or state whether the session "succeeded," was "efficient," or was "worth it." `files_touched` is a plain fact about size of the work, never a quality verdict.
- Never use tool-call count, cache-reuse rate, or session duration by themselves as an efficiency score.
- Context jumps in the COST CURVE list tools active in the *previous* turn as circumstantial context, not a cause: say "context grew after a turn that included these tools," never "X tool added Y tokens." The same caution applies to any jump you tie to a specific turn's own response (e.g. a long summary): say it "coincides with" or "lands on" that turn, never that the response "caused" or "added" a specific token amount.
- CURRENT SESSION is a snapshot: if the session is still running, say the numbers may not reflect later turns.
- It's a valid, often correct verdict that "this session is normal for its cohort, no action needed." Don't manufacture a recommendation when the data doesn't support one, and don't let a global recommendation (part 2) read as if it applies to the current session (part 1) unless it actually does.
- FREQUENTLY ACCESSED TARGETS is not confirmed waste. The report says so, and you must not upgrade it. Repeated calls only prove *repetition*, not that the file/command was unchanged, that different calls targeted the same section, or that the cost was avoidable. If you use it in a Recommended Action, the Action must be phrased as "Worth inspecting: ..." (e.g. whether the repeated calls hit the same section unchanged, or different offsets/versions), never a prescribed workflow change like "keep a summary instead," and never "wasted cost," "avoidable cost," or "confirmed waste."
- If you group sessions by a pattern in their *titles* (e.g. several "port project to X" sessions) rather than by a field the report actually computes, present it as a hypothesis, not a confirmed finding: "several of the priciest sessions appear to be porting tasks," not "this workflow has been reliably identified." Titles are text you're pattern-matching, not a mechanical signal like `project` or `model`.
- In RECURRING FAILURES, distinguish by the `sessions` count on each entry: `sessions > 1` is a **cross-session recurring issue** (the same failure across different sessions, worth a permanent fix, e.g. a config change). `sessions == 1` (many occurrences, one session) is an **in-session loop** (worth reviewing the approach taken *in that session*, not a systemic fix). Don't treat them as the same kind of finding.
- If a RECURRING FAILURES `message` lists more than one possible cause (e.g. "unescaped backslashes... unescaped control characters... or truncated output"), don't pick one and present it as the diagnosis. You have no evidence for which cause applies without inspecting the original failed tool call. Say the failure recurs and name the *set* of possible causes, or say it needs inspecting the actual calls, not guess.
- A cost recorded after a compaction describes *when* it landed, not that compacting *caused* it. If you want to state a dollar figure for "how much accumulated after the first compaction," only use `spend_after_first_compaction` when the report prints it explicitly for the current session. Never estimate it by eyeballing the COST CURVE.
- Never invent a counterfactual dollar figure the report doesn't compute (e.g. "starting a new session would have cost about $X less"). Without an explicit field for it, phrase the idea as an untested suggestion for next time, not a quantified saving.
- MCP SERVERS: a `[move candidate]` entry is backed by direct evidence (usage confined to one project while configured globally), so it can be a real **Action** ("move `X` to that project's `.mcp.json`"), not just "worth inspecting." An `[unused]` entry only means no usage was *observed in these sessions* — never claim it's confirmed unused or safe to remove; phrase it as "worth inspecting" (why it's still configured, whether it's used outside session activity like a resource URI). Don't invent a token or dollar cost for carrying an unused server: the report doesn't compute one.

**Before writing the response, verify (do this as a final pass, not while drafting):**
- Every count you state for distinct items (files, sessions, occurrences, rows) matches an actual recount of the report's rows, not an estimate or a round number that feels right. If you say "N files," N must equal the number of distinct rows you can point to, not one row counted twice under different labels.
- Every superlative ("most expensive," "highest," "only one") against the *broadest set you actually checked*. "Highest among peers/cohort" must never be written as "highest in the project" or "highest ever." If you haven't checked TOP 20 / COST BY PROJECT for cheaper-but-still-larger sessions, don't claim a project- or history-wide superlative.
- Every quoted number (cost, turns, peak context, rank) traces back to a single row in the report, not assembled from two different rows (e.g. one peer's cost with another peer's turn count).
- No sentence evaluates whether the session's work was complex, successful, efficient, or "worth it." Banned phrasing (and equivalents): "genuinely complex work," "the length reflects the problem, not a methodology problem," "it's done what it set out to do," "the cost was justified," "no methodology problem here," "runaway session(s)" (a long, expensive session is a fact, but whether it spiraled versus simply contained a lot of work is not something the report can tell you).
- Part 1's verdict isn't contradicted by Part 2. If Part 2 recommends a change (e.g. "split long investigations at milestones") using this session as its example, Part 1 should say "no single mistake within this session" rather than an unqualified "no action needed" that Part 2 then undercuts.

This is meant to be a fuller analysis than a quick summary, so don't artificially cap the explanation, but stay focused on what's actionable rather than restating every number in the report.

If the user wants to browse the numbers visually instead, mention they can run `burnie` with no flags to get the interactive HTML report in their browser, but don't generate it as part of this flow.

## Arguments

If `$ARGUMENTS` is provided, treat it as a scope hint for the analysis (e.g. "focus on project X" or "just the last week") rather than a file path. `--raw` has no output file.

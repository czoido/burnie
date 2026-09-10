---
name: burnie-update
description: Fetch current Claude model prices from anthropic.com/pricing and update src/burnie/pricing.py. Use when the user says prices have changed, wants to refresh pricing, or update model costs.
compatibility: Designed for Claude Code. Requires internet access to anthropic.com/pricing.
allowed-tools: WebFetch
metadata:
  version: "0.1.9"
---

Fetch current Claude model prices from anthropic.com/pricing and update `src/burnie/pricing.py`.

## Steps

1. Fetch https://www.anthropic.com/pricing with WebFetch
2. Extract the $/MTok prices for each model family (input, output, cache write, cache read)
3. Update `src/burnie/pricing.py`:
   - Replace the `PRICING` list entries with the new values
   - Update `PRICING_UPDATED` to today's date (YYYY-MM-DD format)
4. If any price actually changed (not just `PRICING_UPDATED`), bump the version to prepare a release:
   - Bump the patch version in `pyproject.toml`'s `version`
   - Bump `metadata.version` in `skills/burnie/SKILL.md` and `skills/burnie-update/SKILL.md` to match
   - All three must stay in sync, same as a manual release bump
5. Report what changed (which models, old vs new values, and the new version if bumped)

## Notes

- Cache write is typically 1.25× input price
- Cache read is typically 0.1× input price
- If a model from the current list is no longer on the pricing page, keep it but add a comment `# discontinued?`
- If a new model appears, add it following the same format
- Only edit `src/burnie/pricing.py` for pricing data. The other files import from it
- If no prices changed (only `PRICING_UPDATED` moved, or nothing moved at all), don't bump the version

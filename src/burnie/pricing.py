# Prices per MTok sourced from https://claude.com/pricing (API pricing table).
# Cache write has two tiers depending on TTL: 5-minute cache = 1.25x input, 1-hour cache = 2x input.
# Cache read (hit) = 0.1x input, same for either TTL.
# Last updated: 2026-07-13
from datetime import datetime

PRICING_UPDATED = '2026-07-13'

PRICING = [
    {'prefix': 'claude-fable-5',    'p': {'input': 10.00, 'output': 50.00, 'cacheWrite5m': 12.50, 'cacheWrite1h': 20.00, 'cacheRead': 1.00}},
    {'prefix': 'claude-mythos-5',   'p': {'input': 10.00, 'output': 50.00, 'cacheWrite5m': 12.50, 'cacheWrite1h': 20.00, 'cacheRead': 1.00}},
    {'prefix': 'claude-opus-4',     'p': {'input': 5.00,  'output': 25.00, 'cacheWrite5m': 6.25,  'cacheWrite1h': 10.00, 'cacheRead': 0.50}},
    {'prefix': 'claude-opus-3',     'p': {'input': 15.00, 'output': 75.00, 'cacheWrite5m': 18.75, 'cacheWrite1h': 30.00, 'cacheRead': 1.50}},  # discontinued?
    {'prefix': 'claude-sonnet-5', 'from': '2026-09-01', 'p': {'input': 3.00, 'output': 15.00, 'cacheWrite5m': 3.75, 'cacheWrite1h': 6.00, 'cacheRead': 0.30}},
    {'prefix': 'claude-sonnet-5',                       'p': {'input': 2.00, 'output': 10.00, 'cacheWrite5m': 2.50, 'cacheWrite1h': 4.00, 'cacheRead': 0.20}},  # introductory until 2026-08-31
    {'prefix': 'claude-sonnet-4',                       'p': {'input': 3.00, 'output': 15.00, 'cacheWrite5m': 3.75, 'cacheWrite1h': 6.00, 'cacheRead': 0.30}},
    {'prefix': 'claude-sonnet-3-7', 'p': {'input': 3.00,  'output': 15.00, 'cacheWrite5m': 3.75, 'cacheWrite1h': 6.00, 'cacheRead': 0.30}},  # discontinued?
    {'prefix': 'claude-sonnet-3-5', 'p': {'input': 3.00,  'output': 15.00, 'cacheWrite5m': 3.75, 'cacheWrite1h': 6.00, 'cacheRead': 0.30}},  # discontinued?
    {'prefix': 'claude-haiku-4',    'p': {'input': 1.00,  'output': 5.00,  'cacheWrite5m': 1.25,  'cacheWrite1h': 2.00,  'cacheRead': 0.10}},
    {'prefix': 'claude-haiku-3-5',  'p': {'input': 0.80,  'output': 4.00,  'cacheWrite5m': 1.00,  'cacheWrite1h': 1.60,  'cacheRead': 0.08}},
    {'prefix': 'claude-haiku-3',    'p': {'input': 0.25,  'output': 1.25,  'cacheWrite5m': 0.30,  'cacheWrite1h': 0.50,  'cacheRead': 0.03}},  # discontinued?
]
DEFAULT_PRICING = {'input': 3.00, 'output': 15.00, 'cacheWrite5m': 3.75, 'cacheWrite1h': 6.00, 'cacheRead': 0.30}


def get_model_pricing(model, date=None):
    if not model:
        return DEFAULT_PRICING
    d = date[:10] if date else datetime.now().strftime('%Y-%m-%d')
    candidates = sorted(
        PRICING,
        key=lambda e: (len(e['prefix']), e.get('from') or ''),
        reverse=True,
    )
    for entry in candidates:
        if model.startswith(entry['prefix']) and (not entry.get('from') or d >= entry['from']):
            return entry['p']
    return DEFAULT_PRICING


# Max context window per model family, in tokens. Anthropic doesn't publish Claude Code's
# auto-compact trigger point, so this is the raw model limit, not "tokens until compaction."
CONTEXT_WINDOWS = [
    {'prefix': 'claude-fable-5',    'tokens': 1_000_000},
    {'prefix': 'claude-mythos-5',   'tokens': 1_000_000},
    {'prefix': 'claude-opus-4',     'tokens': 1_000_000},
    {'prefix': 'claude-opus-3',     'tokens': 200_000},
    {'prefix': 'claude-sonnet-5',   'tokens': 1_000_000},
    {'prefix': 'claude-sonnet-4',   'tokens': 1_000_000},
    {'prefix': 'claude-sonnet-3-7', 'tokens': 200_000},
    {'prefix': 'claude-sonnet-3-5', 'tokens': 200_000},
    {'prefix': 'claude-haiku-4',    'tokens': 200_000},
    {'prefix': 'claude-haiku-3-5',  'tokens': 200_000},
    {'prefix': 'claude-haiku-3',    'tokens': 200_000},
]
DEFAULT_CONTEXT_WINDOW = 200_000


def get_context_window(model):
    if not model:
        return DEFAULT_CONTEXT_WINDOW
    candidates = sorted(CONTEXT_WINDOWS, key=lambda e: len(e['prefix']), reverse=True)
    for entry in candidates:
        if model.startswith(entry['prefix']):
            return entry['tokens']
    return DEFAULT_CONTEXT_WINDOW


# `usage` accepts either the raw per-call shape the API returns (with a `cache_creation` object
# breaking cache writes into 5-minute vs 1-hour TTL buckets, because they're priced very
# differently, 1.25x vs 2x input) or a synthetic aggregate built the same way. Older/malformed
# usage without that breakdown falls back to treating the flat cache_creation_input_tokens as
# 5-minute cache.
#
# Returns the per-component dollar breakdown (not just the total) so a caller accumulating cost
# across many calls (each potentially priced on a different date) can sum components without
# re-deriving them later from a single date applied to the whole batch.
def calc_cost_components(usage, model, date=None):
    p = get_model_pricing(model, date)
    M = 1_000_000
    cache_creation = usage.get('cache_creation') or {}
    write5m = cache_creation.get('ephemeral_5m_input_tokens', usage.get('cache_creation_input_tokens') or 0)
    write1h = cache_creation.get('ephemeral_1h_input_tokens', 0)
    cache_read = usage.get('cache_read_input_tokens') or 0
    return {
        'input': (usage.get('input_tokens') or 0) * p['input'] / M,
        'output': (usage.get('output_tokens') or 0) * p['output'] / M,
        'cacheWrite': (write5m * p['cacheWrite5m'] + write1h * p['cacheWrite1h']) / M,
        'cacheRead': cache_read * p['cacheRead'] / M,
        # What these cache_read tokens would have cost as fresh input, minus what they actually
        # cost: the counterfactual "money saved by caching" figure.
        'cacheSavings': cache_read * (p['input'] - p['cacheRead']) / M,
    }


def calc_cost(usage, model, date=None):
    c = calc_cost_components(usage, model, date)
    return c['input'] + c['output'] + c['cacheWrite'] + c['cacheRead']

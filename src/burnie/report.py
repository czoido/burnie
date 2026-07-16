import json
import math
from datetime import datetime, timedelta
from importlib import resources

from .pricing import get_context_window, get_model_pricing, PRICING_UPDATED

COLORS = ['#6366f1', '#8b5cf6', '#a78bfa', '#c4b5fd', '#06b6d4', '#0891b2', '#0e7490', '#155e75', '#10b981', '#059669', '#047857', '#065f46']
MIN_SESSIONS_FOR_PERCENTILES = 10
# Cohorts (same project + model) are inherently smaller than the whole session set, so this uses a lower bar
# than MIN_SESSIONS_FOR_PERCENTILES, below which p90/p95 would just be restating the max.
COHORT_MIN_FOR_PERCENTILES = 5

_chart_js_source = None


# Vendored (src/burnie/assets/chart.umd.min.js) and inlined into the report instead of loaded
# from a CDN, because the report's whole pitch is "no cloud, just your local files," and it also
# contains local paths, titles, and error messages from every session, so it shouldn't be
# handed to a page that fetches third-party JS at open time.
def _load_chart_js():
    global _chart_js_source
    if _chart_js_source is None:
        _chart_js_source = resources.files('burnie').joinpath('assets', 'chart.umd.min.js').read_text()
    return _chart_js_source


# Standard linear-interpolation percentile (same convention as numpy's default), used instead of
# a plain average, which a skewed cost distribution (a handful of sessions dominating total spend)
# makes a weak baseline: half the sessions can sit well below "the average."
def _percentile(sorted_values, pct):
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100)
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)


def _percentile_block(values, minimum=MIN_SESSIONS_FOR_PERCENTILES):
    if len(values) < minimum:
        return None
    sv = sorted(values)
    block = {p: _percentile(sv, p) for p in (50, 75, 90, 95)}
    block['max'] = sv[-1]
    return block


# Where a single value falls among a set of others: how a specific session compares to the
# distribution it's part of, not just whether it's in some top-N list.
def _percentile_rank(value, values):
    if not values:
        return None
    return sum(1 for v in values if v <= value) / len(values) * 100


# Smallest prefix of a project's own sessions (priciest first) whose combined cost passes half of
# that project's total, shared by the project table's secondary concentration line and (formerly)
# the "What deserves attention" bullet, so both read the same number.
def _cost_concentration(costs_desc, target_frac=0.5):
    total = sum(costs_desc)
    if total <= 0:
        return 0, 0.0
    cum, n = 0.0, 0
    for c in costs_desc:
        cum += c
        n += 1
        if cum >= total * target_frac:
            break
    return n, cum


def _fmt_short(n):
    return f"${n:.2f}" if n >= 0.01 else f"${n:.4f}"


def _fmt_k(n):
    return f'{n / 1000:.1f}K' if n >= 1000 else str(int(n))


def _fmt_big(n):
    return f'{n / 1_000_000:.1f}M' if n >= 1_000_000 else _fmt_k(n)


def _fmt_date(iso):
    return iso[:10] if iso else 'unknown'


def _safe_json_dumps(obj, **kwargs):
    # A string value can legitimately contain a literal "</script>", e.g. a Bash command
    # descriptor for a command that itself emits or greps HTML/JS. The browser's HTML parser
    # doesn't know it's sitting inside a JSON string, it just sees "</script" and closes our
    # <script> tag right there, dumping the rest of the JSON as visible text on the page.
    # "<\/script>" is functionally identical JS/JSON (an escaped "/" is still "/") but doesn't
    # match the closing-tag byte sequence, so it's a safe no-op unless the content is hostile.
    return json.dumps(obj, **kwargs).replace('</', '<\\/')


def _escape_html(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;').replace("'", '&#39;'))




def _js_round(x):
    return math.floor(x + 0.5)


def _fmt_pct(x):
    # A component with real cost can still round to 0% against a much larger total, so showing
    # "0%" then reads as "no cost", when the tooltip/adjacent $ figure says otherwise.
    rounded = _js_round(x)
    if rounded == 0 and x > 0:
        return '<1%'
    return f'{rounded}%'


def _format_generated(now):
    hour12 = now.strftime('%I').lstrip('0') or '12'
    return f"{now.month}/{now.day}/{now.year}, {hour12}:{now.strftime('%M:%S %p')}"


# A tool_result marked is_error isn't always a real failure worth acting on: a hook that
# short-circuits a redundant re-read (e.g. "unchanged since turn:0, no need to re-read") reports
# itself as an error even though it's the opposite of one, and a non-zero exit with no stderr is
# usually just grep/test/diff signaling "false", not a broken command. Both would otherwise
# inflate "Recurring failures" with noise that doesn't point to any fix.
_PREVENTED_PATTERNS = ('unchanged since turn', 'no need to re-read', 'already in context')
_EXPECTED_PATTERNS = ('non-zero exit, no output',)


def _classify_error(message):
    m = (message or '').lower()
    if any(p in m for p in _PREVENTED_PATTERNS):
        return 'prevented'
    if any(p in m for p in _EXPECTED_PATTERNS):
        return 'expected'
    return 'failure'


# The same exact failure (tool + descriptor + message) recurring across separate sessions is a
# much more reliable "worth fixing" signal than a raw per-tool error count. Gated on 2+ sessions,
# not just a raw count: the same command failing 3x inside one session is that session's own
# thrashing loop (still visible there, in its Errors list), not a global pattern, and only recurrence
# across separate sessions points at something worth fixing everywhere. Only counts messages
# classified as a real 'failure'. Prevented-redundant and expected-benign outcomes are surfaced
# elsewhere, not here.
def _aggregate_repeated_failures(sessions, limit=10):
    agg = {}
    for s in sessions:
        session_groups = {}
        for err in (s.get('toolErrors') or []):
            if _classify_error(err.get('message')) != 'failure':
                continue
            key = (err.get('tool'), err.get('descriptor'), err.get('message'))
            session_groups[key] = session_groups.get(key, 0) + 1
        for key, count in session_groups.items():
            entry = agg.setdefault(key, {'count': 0, 'sessions': 0})
            entry['count'] += count
            entry['sessions'] += 1
    return sorted(
        (
            {'tool': tool, 'descriptor': descriptor, 'message': message, 'count': e['count'], 'sessions': e['sessions']}
            for (tool, descriptor, message), e in agg.items()
            if e['sessions'] >= 2
        ),
        key=lambda r: r['count'], reverse=True,
    )[:limit]


def _session_entry(s, idx, sessions_len, observed_compact_floor):
    per_model_cost = (s.get('perModelCost') or {}).values()
    ic = sum(c['input'] for c in per_model_cost)
    oc = sum(c['output'] for c in per_model_cost)
    cwc = sum(c['cacheWrite'] for c in per_model_cost)
    crc = sum(c['cacheRead'] for c in per_model_cost)
    cache_savings = sum(c['cacheSavings'] for c in per_model_cost)
    cache_roi = (cache_savings / cwc) if cwc > 0 else None
    model = s.get('model') or 'unknown'
    models = [m.replace('claude-', '') for m in (s.get('models') or [s.get('model')]) if m]
    total_tools = s.get('totalTools') or 0
    context_window = observed_compact_floor.get(model, get_context_window(model))

    return {
        'id': s['sessionId'],
        'filePath': s['filePath'],
        'title': s.get('title') or s['sessionId'][:8],
        'project': s['projectName'],
        'date': _fmt_date(s.get('firstTimestamp')),
        'firstTimestamp': s.get('firstTimestamp'),
        'model': model,
        'models': models,
        'modelShort': model.replace('claude-', ''),
        'cost': s['cost'],
        'messages': s['messageCount'],
        'input': s['usage']['input'],
        'output': s['usage']['output'],
        'cacheWrite': s['usage']['cacheWrite'],
        'cacheRead': s['usage']['cacheRead'],
        'inputCost': ic,
        'outputCost': oc,
        'cacheWriteCost': cwc,
        'cacheReadCost': crc,
        'cacheSavings': cache_savings,
        'cacheROI': cache_roi,
        'toolCounts': s.get('toolCounts') or {},
        'totalTools': total_tools,
        'toolErrors': [{**e, 'category': _classify_error(e.get('message'))} for e in (s.get('toolErrors') or [])],
        'permissionBlocks': s.get('permissionBlocks') or [],
        'costRank': idx + 1,
        'costRankPct': ((idx + 1) / sessions_len * 100) if sessions_len >= MIN_SESSIONS_FOR_PERCENTILES else None,
        'inputPerMsg': s.get('inputPerMsg') or [],
        'compactions': [{'trigger': c.get('trigger'), 'preTokens': c.get('preTokens'), 'timestamp': c.get('timestamp')} for c in (s.get('compactions') or [])],
        'contextWindow': context_window,
        'contextWindowSource': 'observed' if model in observed_compact_floor else 'estimated',
        'costAfterFirstCompaction': s.get('costAfterFirstCompaction') or 0.0,
        'cacheHitRate': s.get('cacheHitRate'),
        'durationMinutes': s.get('durationMinutes'),
        'entrypoint': s.get('entrypoint'),
        'projectPath': s.get('projectPath') or '',
        # Every day this session had activity on, not just the start day, so a session that
        # spans midnight still shows up when clicking either day in the daily chart.
        'days': sorted((s.get('perDay') or {}).keys()),
    }


# Shared by the markdown and raw reports: why a session cost money isn't the same question as
# what's worth reviewing about it, and collapsing them into one made a $30 session that ran long
# look the same as one that hit a real thrashing loop.
def _session_why_review(e):
    # "Building and carrying context" rather than "carrying context forward" for the combined
    # bucket: cache read is reusing context you already built, cache write is building it in
    # the first place, and folding both under "carrying forward" mislabels the write half.
    shares = sorted([
        ('building and carrying context', e['cacheReadCost'] + e['cacheWriteCost']),
        ('output', e['outputCost']),
        ('fresh input', e['inputCost']),
    ], key=lambda x: x[1], reverse=True)
    why_parts = []
    if e['cost'] > 0 and shares[0][1] > 0:
        why_parts.append(f'{shares[0][1] / e["cost"] * 100:.0f}% {shares[0][0]}')
    why = ', '.join(why_parts) if why_parts else '-'

    review = []
    # Excludes prevented-redundant-call and expected-benign-outcome errors (see _classify_error).
    # Those repeating 3x isn't the same signal as a command actually failing 3x.
    error_groups = {}
    for err in (e['toolErrors'] or []):
        if _classify_error(err.get('message')) != 'failure':
            continue
        key = (err.get('tool'), err.get('descriptor'), err.get('message'))
        error_groups[key] = error_groups.get(key, 0) + 1
    max_repeated_failure = max(error_groups.values()) if error_groups else 0
    if max_repeated_failure >= 3:
        review.append(f'same failure repeated {max_repeated_failure}×')

    return why, (', '.join(review) if review else '-')


def _mcp_project_display(path, project_name_by_path):
    # Falls back to the last path segment for a project ~/.claude.json knows about but that has
    # no parsed session data (so it never earned an entry in project_name_by_path).
    if path in project_name_by_path:
        return project_name_by_path[path]
    return (path or '').rstrip('/').rsplit('/', 1)[-1] or path


def _mcp_project_chip(name, count):
    # Reuses the same click target the project table/"Review X" links already use, so
    # clicking a project here filters the sessions table below to it, same as everywhere else.
    return (
        f'<span class="inline-link project-bar-row" data-project="{_escape_html(name)}">{_escape_html(name)}</span>'
        f' <span style="color:var(--muted)">({count}&times;)</span>'
    )


def _mcp_row(e, project_name_by_path):
    scope_label = 'Global' if e['scope'] == 'user' else 'Local'
    if not e['usedIn']:
        # No chip for a local-scoped server tied to a project with no session data: there's
        # nothing in the sessions table to filter to, so it's shown as plain text, not a link.
        project_note = f' ({_escape_html(_mcp_project_display(e["project"], project_name_by_path))})' if e['scope'] != 'user' else ''
        used_html = f'<span style="color:var(--muted)">No usage detected{project_note}</span>'
    else:
        used_html = ', '.join(
            _mcp_project_chip(project_name_by_path.get(u['project'], u['project']), u['count'])
            for u in e['usedIn']
        )
    tip_html = ''
    if e['status'] == 'move-candidate':
        move_proj = project_name_by_path.get(e['moveTo'], e['moveTo'])
        tip_html = (
            ' <span class="tip" tabindex="0" data-tip="Configured globally but only used in '
            f'{_escape_html(move_proj)}. Move it into that project\'s .mcp.json so it stops loading '
            '(and adding tool-schema overhead) in every other project.">?</span>'
        )
    return (
        f'<tr><td class="insight-file">{_escape_html(e["server"])}</td>'
        f'<td style="color:var(--muted)">{scope_label}</td>'
        f'<td>{used_html}{tip_html}</td></tr>'
    )


def generate_report(sessions, highlight_session=None, icon=None, mcp_servers=None):
    total_cost = sum(s['cost'] for s in sessions)
    total_cache_write = sum(s['usage']['cacheWrite'] for s in sessions)
    total_cache_read = sum(s['usage']['cacheRead'] for s in sessions)

    # The baseline everything in "What deserves attention" gets measured against: how far a
    # session/project sits from "typical" for THIS history, not an arbitrary fixed dollar figure.
    overall_median_cost = _percentile(sorted(s['cost'] for s in sessions), 50) if sessions else 0
    # Floor below which a dollar figure isn't worth a finding regardless of how extreme its ratio
    # looks: a tiny history where $5 is a large share of total spend still deserves a finding,
    # but a huge history shouldn't get findings over noise-level amounts.
    attention_dollar_floor = max(5.0, total_cost * 0.02)

    # Per-model accurate savings: what cache_read tokens would have cost at full input price minus what they actually cost
    total_cache_savings = sum(
        c['cacheSavings'] for s in sessions for c in (s.get('perModelCost') or {}).values()
    )

    by_day = {}
    for s in sessions:
        for day, c in (s.get('perDay') or {}).items():
            by_day[day] = by_day.get(day, 0) + c

    # Continuous calendar series (gaps filled with 0), not just the days with activity, because a
    # sparse-but-real gap (e.g. a weekend) must not read as "adjacent to the next active day"
    # on the x-axis, and the rolling average below needs true 7-calendar-day windows to agree
    # with the "Last 7 days" KPI further down, which is anchored the same way.
    today = datetime.now().date()
    known_days = sorted(d for d in by_day if d != 'unknown')
    start_date = datetime.strptime(known_days[0], '%Y-%m-%d').date() if known_days else today
    sorted_days = []
    day_cursor = start_date
    while day_cursor <= today:
        sorted_days.append(day_cursor.strftime('%Y-%m-%d'))
        day_cursor += timedelta(days=1)
    day_costs = [by_day.get(d, 0) for d in sorted_days]
    day_session_count = [sum(1 for s in sessions if d in (s.get('perDay') or {})) for d in sorted_days]

    current_month_prefix = datetime.now().strftime('%Y-%m')
    current_month_cost = sum(c for d, c in by_day.items() if d.startswith(current_month_prefix))

    by_project = {}
    for s in sessions:
        by_project[s['projectName']] = by_project.get(s['projectName'], 0) + s['cost']

    by_entrypoint = {}
    for s in sessions:
        key = s.get('entrypoint') or 'unknown'
        by_entrypoint[key] = by_entrypoint.get(key, 0) + s['cost']
    entrypoint_entries = sorted(by_entrypoint.items(), key=lambda kv: kv[1], reverse=True)
    entrypoint_names = [n for n, _ in entrypoint_entries]
    entrypoint_costs = [c for _, c in entrypoint_entries]
    # A doughnut chart where one slice is >95% isn't a comparison, it's just restating the total.
    show_entrypoint_chart = len(entrypoint_entries) > 1 and (entrypoint_costs[0] / max(total_cost, 1e-9)) < 0.95

    rolling_avg = []
    for i in range(len(sorted_days)):
        w = day_costs[max(0, i - 6):i + 1]
        rolling_avg.append(sum(w) / len(w))

    # Same continuous day_costs series as the chart/rolling avg above, sliced to calendar
    # windows, so the KPI and the chart's last point can never disagree the way they would
    # if this recomputed from by_day with its own (possibly gappy) date filter.
    last7_cost = sum(day_costs[-7:])
    prev7_window = day_costs[-14:-7]
    prev7_cost = sum(prev7_window)
    week_change_pct = ((last7_cost - prev7_cost) / prev7_cost * 100) if prev7_cost > 0 else None

    by_model = {}
    for s in sessions:
        for m, c in (s.get('perModelCost') or {}).items():
            by_model[m] = by_model.get(m, 0) + c['total']
    model_entries = sorted(((m, c) for m, c in by_model.items() if c >= 0.001), key=lambda kv: kv[1], reverse=True)
    model_names = [m.replace('claude-', '') for m, _ in model_entries]
    model_costs = [c for _, c in model_entries]

    # Sessions by project summary. Median + P90 instead of a plain average: a project with 2
    # sessions and one with 40 can have the same average and very different shapes, and P90 also
    # surfaces a project with a long tail of expensive sessions that the median alone would hide.
    project_stats = {}
    for s in sessions:
        st = project_stats.setdefault(s['projectName'], {'count': 0, 'cost': 0.0, 'costs': []})
        st['count'] += 1
        st['cost'] += s['cost']
        st['costs'].append(s['cost'])
    sorted_projects = sorted(project_stats.items(), key=lambda kv: kv[1]['cost'], reverse=True)
    top_project_stats = [
        (
            name,
            st['count'],
            st['cost'],
            _percentile(sorted(st['costs']), 50),
            _percentile(sorted(st['costs']), 90),
            st['count'] < COHORT_MIN_FOR_PERCENTILES,
        )
        for name, st in sorted_projects[:10]
    ]
    # Table below only ever shows the top 10, so say so explicitly rather than silently truncating,
    # which reads as "these are all your projects" when there are more.
    other_projects = sorted_projects[10:]

    # Weighted by token volume, not a per-session average, because a few huge sessions shouldn't be
    # drowned out by many tiny ones with a coincidentally high or low hit rate.
    cache_hit_rate = (total_cache_read / (total_cache_read + total_cache_write)) if (total_cache_read + total_cache_write) > 0 else 0

    # Cost concentration (Pareto): how much of total spend comes from the priciest slice of
    # sessions. Needs enough sessions for "top 10%" to mean more than 0 or 1 session.
    sorted_by_cost_desc = sorted(sessions, key=lambda s: s['cost'], reverse=True)
    top10_count = max(1, math.ceil(len(sessions) * 0.1))
    cost_concentration_pct = None
    if len(sessions) >= MIN_SESSIONS_FOR_PERCENTILES:
        cost_concentration_pct = sum(s['cost'] for s in sorted_by_cost_desc[:top10_count]) / max(total_cost, 1e-9) * 100

    # Observed auto-compact floor per model: Anthropic doesn't publish the trigger threshold, but
    # Claude Code logs a compact_boundary event with the exact token count each time it fires. The
    # check only runs at turn boundaries, so a single huge turn can overshoot the real threshold.
    # Taking the minimum observed value per model is the best estimate, since overshoot only ever
    # pushes the recorded number up, never down.
    observed_compact_floor = {}
    for s in sessions:
        for c in (s.get('compactions') or []):
            if c.get('trigger') != 'auto' or not c.get('preTokens') or not c.get('model'):
                continue
            m = c['model']
            observed_compact_floor[m] = min(observed_compact_floor.get(m, float('inf')), c['preTokens'])

    # All sessions with per-type costs for the detail panel
    sessions_by_cost_desc = sorted(sessions, key=lambda s: s['cost'], reverse=True)
    all_sessions = [
        _session_entry(s, idx, len(sessions), observed_compact_floor)
        for idx, s in enumerate(sessions_by_cost_desc)
    ]

    # Peer-relative cost + post-compaction exposure, computed for every session so both the
    # per-session detail panel (client-side) and "What deserves attention" below can reference
    # them. O(n²): for each session, _select_peers filters the full list down to its
    # (project, model, regime) cohort, fine at the scale this report runs at (thousands of
    # sessions, not millions), not worth a group-by pre-pass until it is.
    for e in all_sessions:
        peers, _lower_cost_peers, _cohort_candidates, quality, quality_reasons = _select_peers(e, all_sessions)
        peer_costs = sorted(p['cost'] for p in peers)
        peer_median_cost = _percentile(peer_costs, 50)
        e['peerCount'] = len(peers)
        e['peerMedianCost'] = peer_median_cost
        e['costVsPeerMedian'] = (e['cost'] / peer_median_cost) if peer_median_cost else None
        e['peerPercentile'] = _percentile_rank(e['cost'], peer_costs)
        e['comparisonQuality'] = quality
        e['peerCohortDescription'] = (
            'Same project and model, with similar session length'
            + (f', {quality_reasons[0]}' if quality_reasons else '')
        )

        compaction_count = len(e['compactions'])
        after_first_share = (e['costAfterFirstCompaction'] / e['cost']) if e['cost'] > 0 else 0.0
        e['compactionCount'] = compaction_count
        e['afterFirstSharePct'] = after_first_share * 100

        # "Material": worth a finding, not just present. Gated on both a relative share (this
        # wasn't a trivial tail end of the session) and an absolute floor (this wasn't trivial
        # money either), never framed as confirmed waste, just where the cost landed.
        e['postCompactionMaterial'] = (
            compaction_count >= 1
            and after_first_share >= 0.4
            and e['costAfterFirstCompaction'] >= attention_dollar_floor
        )
        # ≥COHORT_MIN_FOR_PERCENTILES peers (the same bar _select_peers itself needs before its
        # own percentiles are trustworthy) + high comparison quality + a real dollar amount, not a
        # $0.02 session that happens to be 3x its equally tiny peers.
        e['peerOutlierMaterial'] = (
            e['peerCount'] >= COHORT_MIN_FOR_PERCENTILES
            and quality == 'high'
            and e['cost'] >= 5
            and e['costVsPeerMedian'] is not None
            and e['costVsPeerMedian'] >= 1.5
        )

    # "What deserves attention": the report's only synthesis step. Everything else is data to
    # look at, this is the one place that says what to look at first. Three finding types, tried
    # in priority order, each contributing at most one card (naturally capping this at 3):
    #   1. peer outlier: a session that cost far more than genuinely comparable sessions
    #   2. post-compaction exposure: cost that landed after a session kept going past an
    #      automatic compaction instead of starting fresh (single session, or an all-time
    #      rollup when several qualify)
    #   3. small-sample high-average: a project too small to ever clear the peer-outlier bar
    #      above (no fair cohort to compare against) but whose sessions average far above the
    #      overall baseline anyway
    # If the same session clears both (1) and (2), they're merged into one combined card instead
    # of two separate ones. Historical cost concentration (a project's total dominated by a few
    # of its own sessions) moved out of here entirely, it only restated the project table below.
    # See that table's secondary line instead. No causal claims anywhere here: post-compaction
    # cost describes WHEN it accumulated, not proof continuing past the compaction caused it,
    # never "waste" or "avoidable."
    findings = []

    peer_outlier_candidates = sorted(
        (e for e in all_sessions if e['peerOutlierMaterial']),
        key=lambda e: e['costVsPeerMedian'], reverse=True,
    )
    post_compaction_candidates = sorted(
        (e for e in all_sessions if e['postCompactionMaterial']),
        key=lambda e: e['costAfterFirstCompaction'], reverse=True,
    )

    top_outlier = peer_outlier_candidates[0] if peer_outlier_candidates else None
    combined_with = top_outlier if (top_outlier and top_outlier['postCompactionMaterial']) else None

    if combined_with:
        e = combined_with
        findings.append(
            '<strong>COST OUTLIER · LONG SESSION</strong><br>'
            f'"{_escape_html(e["title"])}" cost {e["costVsPeerMedian"]:.1f}× its peer median and accumulated '
            f'{_fmt_short(e["costAfterFirstCompaction"])} ({e["afterFirstSharePct"]:.0f}%) after the first of '
            f'{e["compactionCount"]} automatic compactions.<br>'
            f'<span class="inline-link" onclick="openSession(\'{e["id"]}\')">Review session →</span>'
        )
        post_compaction_candidates = [c for c in post_compaction_candidates if c['id'] != e['id']]
    elif top_outlier:
        e = top_outlier
        findings.append(
            f'<strong>COST OUTLIER · {e["peerCount"]} COMPARABLE SESSIONS</strong><br>'
            f'"{_escape_html(e["title"])}" cost {e["costVsPeerMedian"]:.1f}× the median of comparable sessions: '
            f'{_fmt_short(e["cost"])} versus {_fmt_short(e["peerMedianCost"])}.<br>'
            f'<span class="inline-link" onclick="openSession(\'{e["id"]}\')">Review comparison →</span>'
        )

    if len(post_compaction_candidates) == 1:
        e = post_compaction_candidates[0]
        findings.append(
            f'<strong>LONG SESSION · {_escape_html(e["project"].upper())}</strong><br>'
            f'"{_escape_html(e["title"])}" crossed {e["compactionCount"]} automatic compaction'
            f'{"s" if e["compactionCount"] != 1 else ""}, {_fmt_short(e["costAfterFirstCompaction"])} of '
            f'{_fmt_short(e["cost"])} ({e["afterFirstSharePct"]:.0f}%) accumulated after the first.<br>'
            f'<span class="inline-link" onclick="openSession(\'{e["id"]}\')">Review session →</span>'
        )
    elif len(post_compaction_candidates) >= 2:
        total_after = sum(e['costAfterFirstCompaction'] for e in post_compaction_candidates)
        total_of = sum(e['cost'] for e in post_compaction_candidates)
        pct = (total_after / total_of * 100) if total_of else 0
        ids_json = _safe_json_dumps([e['id'] for e in post_compaction_candidates])
        findings.append(
            '<strong>LONG-SESSION EXPOSURE · ALL TIME</strong><br>'
            f'{len(post_compaction_candidates)} sessions crossed an automatic compaction, together '
            f'{_fmt_short(total_after)} ({pct:.0f}% of their combined cost) accumulated after the first.<br>'
            f'<span class="inline-link" onclick=\'setSessionIdFilter({ids_json}, "costAfterFirstCompaction")\'>'
            f'Review {len(post_compaction_candidates)} sessions →</span>'
        )

    # Small-sample-high-average: a project too small to ever have a fair peer cohort (2 sessions
    # can never reach the ≥COHORT_MIN_FOR_PERCENTILES bar peerOutlierMaterial requires above) but
    # whose sessions average far above the overall baseline anyway, the pycapnp case peer
    # comparison structurally can't catch. 5x the overall median is a high bar on purpose: this is
    # a cross-project comparison (different projects, models, session shapes), a much weaker
    # baseline than a real peer cohort, so it needs a bigger gap to be worth a finding.
    if len(findings) < 3:
        small_sample_best = None
        for name, st in project_stats.items():
            if st['count'] < 2 or st['count'] >= COHORT_MIN_FOR_PERCENTILES:
                continue
            if st['cost'] < attention_dollar_floor:
                continue
            median = _percentile(sorted(st['costs']), 50)
            if not overall_median_cost or median is None or median < overall_median_cost * 5:
                continue
            ratio = median / overall_median_cost
            if small_sample_best is None or ratio > small_sample_best[1]:
                small_sample_best = (name, ratio, st)
        if small_sample_best:
            name, ratio, st = small_sample_best
            findings.append(
                f'<strong>SMALL SAMPLE · {_escape_html(name.upper())}</strong><br>'
                f'{st["count"]} {_escape_html(name)} sessions averaged {_fmt_short(st["cost"] / st["count"])} each, '
                f'{ratio:.0f}× the {_fmt_short(overall_median_cost)} median across all {len(sessions)} sessions.<br>'
                f'<span class="inline-link project-bar-row" data-project="{_escape_html(name)}">Review {_escape_html(name)} →</span>'
            )

    attention_html = ''
    if findings:
        heading = 'Worth reviewing' if len(findings) == 1 else 'What deserves attention'
        attention_rows = ''.join(f'<li>{item}</li>' for item in findings[:3])
        attention_html = f"""  <div class="insight-card attention-card">
    <h2>{heading}</h2>
    <ul class="attention-list">
      {attention_rows}
    </ul>
  </div>

"""

    # ── dynamic HTML fragments ──────────────────────────────────────────
    icon_html = (
        f'<a href="https://github.com/czoido/burnie" target="_blank" style="display:inline-block;vertical-align:middle;margin-right:8px;margin-bottom:3px">'
        f'<img src="{icon}" alt="burnie" style="width:32px;height:32px;object-fit:contain;display:block">'
        f'</a>'
    ) if icon else ''
    generated_str = _format_generated(datetime.now())

    # Bar width relative to the TOTAL, not the priciest model, because sizing against each other instead
    # of against the whole makes a model that's 23% of total spend look like it's ~31% of the pie,
    # just because it happens to be cheaper than the most expensive model. Same fix as component_rows.
    total_model_cost = sum(model_costs) or 1
    model_rows = ''.join(
        f"""
          <div class="kpi-model-row">
            <span class="kpi-model-name">{m} <span style="color:var(--muted)">({_fmt_pct(c / total_model_cost * 100)})</span></span>
            <div class="kpi-model-bar-wrap">
              <div class="kpi-model-bar" style="width:{_js_round(c / total_model_cost * 100)}%"></div>
            </div>
            <span class="kpi-model-cost">{_fmt_short(c)}</span>
          </div>"""
        for m, c in zip(model_names, model_costs)
    )

    # One table instead of a bar-list card plus a separate "Sessions by project" table doing the
    # same breakdown twice: total (with a bar), share, sessions, and the median/P90 that a plain
    # average would hide. The bar-list row's click-to-filter class/attribute carries over as-is.
    max_project_cost = top_project_stats[0][2] if top_project_stats else 1

    def _project_row(name, count, cost, median, p90, small_n):
        small_n_tip = (
            ' <span class="tip" tabindex="0" data-tip="Small sample, treat median/P90 as low-confidence.">?</span>'
            if small_n else ''
        )
        share = (cost / total_cost * 100) if total_cost else 0
        # Same "concentrated" test the historical-concentration finding used to run in "What
        # deserves attention" before it moved here: a low median relative to the whole session
        # population means this project's total comes from having a lot of sessions, not from any
        # one of them being unusually expensive, so a "top N sessions" figure wouldn't say
        # anything a reader doesn't already see in Median/P90. count >= 3 so "top N" means more
        # than trivially restating the row's own total.
        concentrated = count >= 3 and median is not None and overall_median_cost and median > overall_median_cost * 1.5
        concentration_html = ''
        if concentrated:
            costs_desc = sorted((s['cost'] for s in sessions if s['projectName'] == name), reverse=True)
            n_needed, cum = _cost_concentration(costs_desc)
            concentration_html = (
                f'<div class="insight-subline">Top {n_needed} session{"s" if n_needed != 1 else ""}: {_fmt_short(cum)} · '
                f'{(cum / cost * 100) if cost else 0:.0f}% of project cost</div>'
            )
        return (
            f'<tr class="project-bar-row" data-project="{_escape_html(name)}" style="cursor:pointer">'
            f'<td class="insight-file" style="cursor:pointer">{_escape_html(name)}{concentration_html}</td>'
            f'<td style="width:120px;padding:4px 6px"><div style="display:flex;align-items:center;gap:6px">'
            f'<div style="flex:1;background:var(--surface2);border-radius:3px;height:6px"><div style="height:6px;border-radius:3px;background:var(--accent);width:{_js_round(cost / max_project_cost * 100)}%"></div></div>'
            f'<span style="white-space:nowrap;font-size:11px">{_fmt_short(cost)}</span></div></td>'
            f'<td class="insight-count" style="color:var(--muted)">{share:.0f}%</td>'
            f'<td class="insight-count" style="color:var(--muted);font-weight:400">{count}{small_n_tip}</td>'
            f'<td class="insight-count" style="color:var(--muted)">{_fmt_short(median)}</td>'
            f'<td class="insight-count" style="color:var(--muted)">{_fmt_short(p90)}</td></tr>'
        )

    project_rows = ''.join(_project_row(*row) for row in top_project_stats)
    if other_projects:
        other_count = sum(st['count'] for _, st in other_projects)
        other_cost = sum(st['cost'] for _, st in other_projects)
        other_share = (other_cost / total_cost * 100) if total_cost else 0
        project_rows += (
            f'<tr style="cursor:default"><td class="insight-file" style="cursor:default;color:var(--muted)">Other ({len(other_projects)} project{"s" if len(other_projects) != 1 else ""})</td>'
            f'<td style="width:120px;padding:4px 6px"><div style="display:flex;align-items:center;gap:6px">'
            f'<div style="flex:1;background:var(--surface2);border-radius:3px;height:6px"><div style="height:6px;border-radius:3px;background:var(--muted);width:{_js_round(other_cost / max_project_cost * 100)}%"></div></div>'
            f'<span style="white-space:nowrap;font-size:11px;color:var(--muted)">{_fmt_short(other_cost)}</span></div></td>'
            f'<td class="insight-count" style="color:var(--muted)">{other_share:.0f}%</td>'
            f'<td class="insight-count" style="color:var(--muted);font-weight:400">{other_count}</td>'
            f'<td class="insight-count" style="color:var(--muted)">-</td>'
            f'<td class="insight-count" style="color:var(--muted)">-</td></tr>'
        )

    # A raw dollar figure for the priciest handful of sessions reads as more tangible than an
    # abstract percentile: "top 10 sessions = $278" lands more concretely than "64th percentile".
    top_n_for_dollar = min(10, len(sessions))
    top_n_dollar_cost = sum(s['cost'] for s in sorted_by_cost_desc[:top_n_for_dollar])

    # Numbers only (top10_count, top_n_for_dollar), not user data, so it's safe to inline
    # them straight into onclick without the escaping session titles/projects need elsewhere.
    top10_link = f'<span class="inline-link" onclick="setTopNFilter({top10_count})">{top10_count}</span>'
    top_n_dollar_link = (
        f'top <span class="inline-link" onclick="setTopNFilter({top_n_for_dollar})">{top_n_for_dollar}</span> = {_fmt_short(top_n_dollar_cost)}'
        if top_n_for_dollar > 0 else ''
    )
    cost_concentration_value = f'{cost_concentration_pct:.0f}%' if cost_concentration_pct is not None else '-'
    cost_concentration_sub = (
        f'of spend ({top10_link} of {len(sessions)} sessions) · {top_n_dollar_link}'
        if cost_concentration_pct is not None
        else (top_n_dollar_link or f'need {MIN_SESSIONS_FOR_PERCENTILES}+ sessions')
    )

    # Absolute last-7-days spend is the primary number: "how much did I spend recently" is
    # answered directly by a total, not a rate. The average is still useful, but only per ACTIVE
    # day (last7_cost / days you actually used it), not per calendar day (which dilutes it with
    # zero-activity days and makes it read lower than what any session you actually ran cost).
    last7_active_days = sum(1 for c in day_session_count[-7:] if c > 0)
    cost_per_active_day = (last7_cost / last7_active_days) if last7_active_days > 0 else 0
    week_change_str = (
        f'{"+" if week_change_pct >= 0 else ""}{week_change_pct:.0f}% vs previous 7 days'
        if week_change_pct is not None else 'no prior week to compare'
    )
    last7_sub = (
        f'{week_change_str} · {last7_active_days} active day{"s" if last7_active_days != 1 else ""} · {_fmt_short(cost_per_active_day)}/active day'
        if last7_active_days > 0 else week_change_str
    )

    # Global cost-by-component breakdown: money, not raw token volume, is what lets the four
    # components (fresh input, output, cache write, cache read) be compared directly.
    total_input_cost = sum(e['inputCost'] for e in all_sessions)
    total_output_cost = sum(e['outputCost'] for e in all_sessions)
    total_cache_write_cost = sum(e['cacheWriteCost'] for e in all_sessions)
    total_cache_read_cost = sum(e['cacheReadCost'] for e in all_sessions)
    component_entries = sorted([
        ('Cache read', total_cache_read_cost, '#10b981'),
        ('Cache write', total_cache_write_cost, '#06b6d4'),
        ('Output', total_output_cost, '#a78bfa'),
        ('Fresh input', total_input_cost, '#6366f1'),
    ], key=lambda x: x[1], reverse=True)
    # Bar width relative to the TOTAL, not the largest component, because sizing bars against each other
    # instead of against the whole makes a 32%-of-total component look like it's ~64% of the pie.
    total_component_cost = sum(c for _, c, _ in component_entries) or 1
    component_rows = ''.join(
        f"""
          <div class="kpi-model-row">
            <span class="kpi-model-name">{name} <span style="color:var(--muted)">({_fmt_pct(c / total_component_cost * 100)})</span></span>
            <div class="kpi-model-bar-wrap">
              <div class="kpi-model-bar" style="width:{_js_round(c / total_component_cost * 100)}%;background:{color}"></div>
            </div>
            <span class="kpi-model-cost">{_fmt_short(c)}</span>
          </div>"""
        for name, c, color in component_entries
    )

    entrypoint_chart_card = f"""
    <div class="chart-card">
      <h2>Cost by entrypoint</h2>
      <div class="chart-wrap" style="height:300px"><canvas id="chartEntrypoint"></canvas></div>
    </div>""" if show_entrypoint_chart else ''

    project_name_by_path = {s['projectPath']: s['projectName'] for s in sessions}
    mcp_chart_card = ''
    if mcp_servers:
        mcp_rows = ''.join(_mcp_row(e, project_name_by_path) for e in mcp_servers)
        mcp_chart_card = f"""
    <div class="chart-card wide">
      <h2>MCP servers <span class="tip" tabindex="0" data-tip="Every configured MCP server's tool schemas are sent on every turn whether or not they're called. Global scope loads in every project; local scope loads in only one. Usage is only what's observed in these sessions via actual tool calls, so 'No usage detected' means not seen here, not proven unused.">?</span></h2>
      <table class="insight-table" style="table-layout:fixed;width:100%">
        <thead><tr><th style="text-align:left;color:var(--muted);font-size:10px;padding:0 4px 6px 0">Server</th><th style="width:150px;text-align:left;color:var(--muted);font-size:10px;padding:0 4px 6px">Scope</th><th style="text-align:left;color:var(--muted);font-size:10px;padding:0 0 6px 4px">Used in</th></tr></thead>
        <tbody>
        {mcp_rows}
        </tbody>
      </table>
    </div>"""

    charts_grid_html = f"""  <div class="charts-grid">
    <div class="chart-card wide">
      <h2>Daily cost</h2>
      <div class="chart-wrap" style="height:260px"><canvas id="chartDaily"></canvas></div>
    </div>
    <div class="chart-card wide">
      <h2>Cost by project <span class="tip" tabindex="0" data-tip="Click a row to filter the sessions table below to that project. Median/P90 use each project's own sessions, so a project with a long tail of expensive sessions shows it there even when the median looks cheap.">?</span></h2>
      <table class="insight-table" style="table-layout:fixed;width:100%">
        <thead><tr><th style="text-align:left;color:var(--muted);font-size:10px;padding:0 4px 6px 0">Project</th><th style="width:120px;text-align:left;color:var(--muted);font-size:10px;padding:0 4px 6px">Total</th><th style="width:48px;white-space:nowrap;text-align:right;color:var(--muted);font-size:10px;padding:0 4px 6px">Share</th><th style="width:56px;white-space:nowrap;text-align:right;color:var(--muted);font-size:10px;padding:0 4px 6px">Sessions</th><th style="width:56px;white-space:nowrap;text-align:right;color:var(--muted);font-size:10px;padding:0 4px 6px" title="The typical session: half cost more, half cost less">Median</th><th style="width:52px;white-space:nowrap;text-align:right;color:var(--muted);font-size:10px;padding:0 0 6px 4px" title="90th percentile: a project with a long tail of expensive sessions shows it here even when the median looks cheap">P90</th></tr></thead>
        <tbody>
        {project_rows}
        </tbody>
      </table>
    </div>{entrypoint_chart_card}{mcp_chart_card}
  </div>

"""

    header_html = f"""  <header>
    <div>
      <h1>{icon_html}Where did all my money go?</h1>
      <p>Generated {generated_str} &middot; {len(sessions)} sessions across {len(by_project)} projects</p>
      <p style="margin-top:2px">Prices as of {PRICING_UPDATED}, <a href="https://www.anthropic.com/pricing" target="_blank" style="color:var(--muted)">anthropic.com/pricing</a>. All costs below are estimates: published API rates applied to your token usage, not what you were actually billed, especially if you're on a Pro/Max subscription rather than pay-as-you-go API access.</p>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <a href="https://claude.ai/new#settings/usage" target="_blank" class="theme-btn" style="text-decoration:none" title="View your actual usage on claude.ai">↗ claude.ai usage</a>
      <button class="theme-btn" onclick="toggleTheme()">
        <span id="themeIcon">☀️</span>
        <span id="themeLabel">Light mode</span>
      </button>
    </div>
  </header>

"""

    kpi_html = f"""  <div class="kpi-grid">
    <div class="kpi accent">
      <div class="kpi-label">Last 7 days <span class="tip" tabindex="0" data-tip="Total spend over the last 7 calendar days, the answer to \'how much have I spent recently,\' not a rate. Active-day average divides by days you actually used it, not by all 7 calendar days (which would dilute it with zero-activity days).">?</span></div>
      <div class="kpi-value">{_fmt_short(last7_cost)}</div>
      <div class="kpi-sub">{last7_sub}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Total cost <span class="tip" tabindex="0" data-tip="Published API rates applied to your token usage, all time, not a real invoice. If you're on a Pro/Max subscription rather than API access, what you were actually billed may look very different.">?</span></div>
      <div class="kpi-value">{_fmt_short(total_cost)}</div>
      <div class="kpi-sub">{len(sessions)} sessions, all time</div>
      <div class="kpi-month">
        <span class="kpi-month-label">This month</span>
        <span class="kpi-month-value">{_fmt_short(current_month_cost)}</span>
      </div>
    </div>
    <div class="kpi kpi-wide">
      <div class="kpi-label">Cost by component <span class="tip" tabindex="0" data-tip="Where the money actually went, by token type, all time: a more direct answer than raw token volume, which mixes categories priced very differently. Cache read is the cost of carrying forward context you already built.">?</span></div>
      <div class="kpi-model-list">
        {component_rows}
      </div>
    </div>
    <div class="kpi amber">
      <div class="kpi-label">Top 10% of sessions <span class="tip" tabindex="0" data-tip="Share of all-time spend that comes from your priciest 10% of sessions. High = a handful of sessions dominate your bill, look at those first.">?</span></div>
      <div class="kpi-value">{cost_concentration_value}</div>
      <div class="kpi-sub">{cost_concentration_sub}</div>
    </div>
    <div class="kpi kpi-wide">
      <div class="kpi-label">Cost by model <span class="insight-sub">all time</span></div>
      <div class="kpi-model-list">
        {model_rows}
      </div>
    </div>
  </div>

"""

    # Pure call volume, no error rate, because a per-tool error percentage mixes real repeated failures
    # with normal one-off churn (a grep with no matches, a permission prompt) and doesn't tell you
    # anything actionable on its own. "Recurring failures" below carries that signal instead.
    script_consts = f"""const DAYS          = {_safe_json_dumps(sorted_days, ensure_ascii=False)}
const DAY_COSTS     = {_safe_json_dumps([round(v, 6) for v in day_costs])}
const ROLLING       = {_safe_json_dumps([round(v, 6) for v in rolling_avg])}
const DAY_SESSIONS  = {_safe_json_dumps(day_session_count)}
const ENTRYPOINT_NAMES = {_safe_json_dumps(entrypoint_names, ensure_ascii=False)}
const ENTRYPOINT_COSTS = {_safe_json_dumps([round(v, 6) for v in entrypoint_costs])}
const MODEL_NAMES   = {_safe_json_dumps(model_names, ensure_ascii=False)}
const MODEL_COSTS   = {_safe_json_dumps([round(v, 6) for v in model_costs])}
const ALL_SESSIONS  = {_safe_json_dumps(all_sessions, ensure_ascii=False)}
const COLORS        = {_safe_json_dumps(COLORS)}
const HIGHLIGHT_SESSION = {_safe_json_dumps(highlight_session)}

"""

    chart_js_html = f'<script>{_load_chart_js()}</script>'

    return (
        _STATIC_HEAD + chart_js_html + header_html + kpi_html + attention_html + charts_grid_html
        + _STATIC_SESSIONS_PANEL + script_consts + _STATIC_SCRIPT_BODY + _STATIC_TAIL
    )


def _md_escape(s):
    return str(s).replace('|', '\\|').replace('\n', ' ')


def _pct_of(part, whole):
    return _fmt_pct(part / whole * 100) if whole else '-'


# A plain-text counterpart to generate_report, same underlying numbers (reuses _session_entry
# for the per-session cost breakdown), but flattened into tables instead of an interactive page.
# No charts, no drill-down. The sessions table carries the same "notable" flags as the HTML
# detail view so a skim of the file still surfaces what's worth a closer look.
def generate_markdown_report(sessions):
    generated_str = _format_generated(datetime.now())
    total_cost = sum(s['cost'] for s in sessions)

    by_day = {}
    for s in sessions:
        for day, c in (s.get('perDay') or {}).items():
            by_day[day] = by_day.get(day, 0) + c

    today = datetime.now().date()
    last7_start, prev7_start, prev7_end = today - timedelta(days=6), today - timedelta(days=13), today - timedelta(days=7)
    last7_cost = sum(c for d, c in by_day.items() if d != 'unknown' and last7_start <= datetime.strptime(d, '%Y-%m-%d').date() <= today)
    prev7_cost = sum(c for d, c in by_day.items() if d != 'unknown' and prev7_start <= datetime.strptime(d, '%Y-%m-%d').date() <= prev7_end)
    daily_avg = last7_cost / 7
    prev_daily_avg = prev7_cost / 7
    week_change_pct = ((last7_cost - prev7_cost) / prev7_cost * 100) if prev7_cost > 0 else None
    week_change_str = (
        f'{"+" if week_change_pct >= 0 else ""}{week_change_pct:.0f}% vs prev 7d avg ({_fmt_short(prev_daily_avg)}/day)'
        if week_change_pct is not None else f'{_fmt_short(daily_avg)}/day, no prior week to compare'
    )

    by_model = {}
    for s in sessions:
        for m, c in (s.get('perModelCost') or {}).items():
            by_model[m] = by_model.get(m, 0) + c['total']
    model_entries = sorted(((m, c) for m, c in by_model.items() if c >= 0.001), key=lambda kv: kv[1], reverse=True)

    by_project = {}
    for s in sessions:
        by_project[s['projectName']] = by_project.get(s['projectName'], 0) + s['cost']

    project_stats = {}
    for s in sessions:
        st = project_stats.setdefault(s['projectName'], {'count': 0, 'cost': 0.0})
        st['count'] += 1
        st['cost'] += s['cost']
    top_project_stats = sorted(
        ((name, st['count'], st['cost'], st['cost'] / st['count'])
         for name, st in project_stats.items()),
        key=lambda r: r[2], reverse=True,
    )

    sorted_by_cost_desc = sorted(sessions, key=lambda s: s['cost'], reverse=True)
    top10_count = max(1, math.ceil(len(sessions) * 0.1))
    cost_concentration_pct = None
    if len(sessions) >= MIN_SESSIONS_FOR_PERCENTILES:
        cost_concentration_pct = sum(s['cost'] for s in sorted_by_cost_desc[:top10_count]) / max(total_cost, 1e-9) * 100
    top_n_for_dollar = min(10, len(sessions))
    top_n_dollar_cost = sum(s['cost'] for s in sorted_by_cost_desc[:top_n_for_dollar])

    global_tool_counts = {}
    for s in sessions:
        for tool, n in (s.get('toolCounts') or {}).items():
            global_tool_counts[tool] = global_tool_counts.get(tool, 0) + n
    top_tool_counts = sorted(global_tool_counts.items(), key=lambda kv: kv[1], reverse=True)

    observed_compact_floor = {}
    for s in sessions:
        for c in (s.get('compactions') or []):
            if c.get('trigger') != 'auto' or not c.get('preTokens') or not c.get('model'):
                continue
            m = c['model']
            observed_compact_floor[m] = min(observed_compact_floor.get(m, float('inf')), c['preTokens'])

    all_sessions = [_session_entry(s, idx, len(sessions), observed_compact_floor) for idx, s in enumerate(sorted_by_cost_desc)]

    component_entries = sorted([
        ('Cache read', sum(e['cacheReadCost'] for e in all_sessions)),
        ('Cache write', sum(e['cacheWriteCost'] for e in all_sessions)),
        ('Output', sum(e['outputCost'] for e in all_sessions)),
        ('Fresh input', sum(e['inputCost'] for e in all_sessions)),
    ], key=lambda x: x[1], reverse=True)

    lines = [
        '# Where did all my money go?',
        '',
        f'_Generated {generated_str} · {len(sessions)} sessions across {len(by_project)} projects · '
        f'prices as of {PRICING_UPDATED} (anthropic.com/pricing). Estimated by applying API rates to token usage, '
        f'not a real invoice, especially on a Pro/Max subscription._',
        '',
        '## Summary',
        '',
        f'- **Estimated cost:** {_fmt_short(total_cost)} ({len(sessions)} sessions)',
        f'- **Daily avg (last 7d):** {_fmt_short(daily_avg)}, {week_change_str}',
    ]
    if cost_concentration_pct is not None:
        lines.append(
            f'- **Cost concentration:** {cost_concentration_pct:.0f}% from priciest {top10_count} of '
            f'{len(sessions)} sessions · top {top_n_for_dollar} = {_fmt_short(top_n_dollar_cost)}'
        )
    else:
        lines.append(f'- **Top {top_n_for_dollar} sessions:** {_fmt_short(top_n_dollar_cost)}')
    lines += [
        '',
        '## Cost by component',
        '',
        '| Component | Cost | Share |',
        '|---|---|---|',
    ]
    lines += [f'| {name} | {_fmt_short(c)} | {_pct_of(c, total_cost)} |' for name, c in component_entries]

    lines += ['', '## Cost by model', '', '| Model | Cost | Share |', '|---|---|---|']
    lines += [f'| {m.replace("claude-", "")} | {_fmt_short(c)} | {_pct_of(c, total_cost)} |' for m, c in model_entries]

    lines += ['', '## Cost by project', '', '| Project | Sessions | Total | Avg/session |', '|---|---|---|---|']
    lines += [
        f'| {_md_escape(name)} | {count} | {_fmt_short(cost)} | {_fmt_short(avg)} |'
        for name, count, cost, avg in top_project_stats
    ]

    # Pure call volume, no error rate, because a per-tool error percentage mixes real repeated failures
    # with normal one-off churn (a grep with no matches, a permission prompt) and isn't actionable
    # on its own.
    if top_tool_counts:
        lines += ['', '## Tool calls', '', '| Tool | Calls |', '|---|---|']
        lines += [f'| {tool} | {n:,} |' for tool, n in top_tool_counts]

    # Two separate columns, on purpose: why a session cost money isn't the same question as
    # what's worth reviewing about it, and collapsing them into one made a $30 session that ran
    # long look the same as one that hit a real thrashing loop.
    lines += ['', f'## Sessions ({len(all_sessions)}, by cost)', '', '| # | Date | Project | Title | Model | Cost | Why | Worth reviewing |', '|---|---|---|---|---|---|---|---|']
    for i, e in enumerate(all_sessions, start=1):
        why, review = _session_why_review(e)
        lines.append(
            f'| {i} | {e["date"]} | {_md_escape(e["project"])} | {_md_escape(e["title"])} | '
            f'{e["modelShort"]} | {_fmt_short(e["cost"])} | {why} | {review} |'
        )

    lines += [
        '',
        '---',
        '',
        f'_Generated {generated_str} with [burnie](https://github.com/czoido/burnie)._',
    ]

    return '\n'.join(lines) + '\n'


_RAW_TOP_N_SESSIONS = 20
_RAW_TOP_N_LISTS = 10
_RAW_PEER_LIMIT = 15
_RAW_LOWER_COST_PEER_LIMIT = 8
_CURVE_MAX_TURNS_FULL = 40
_CURVE_SAMPLE_POINTS = 30
_CURVE_TAIL_TURNS = 10
_CURVE_TOP_JUMPS = 6

# Metrics tracked as distributions (not just a total/average) for the raw report: name, how to
# read it off a _session_entry, and how to format a value for display. Deliberately excludes
# spend_after_first_compaction, it's 0 for any session that never compacted, so folding it in
# here would dilute p50/p75 with zeros from sessions the metric doesn't even apply to. See
# _conditional_percentile_lines for that instead.
_RAW_METRICS = [
    ('cost', lambda e: e['cost'], _fmt_short),
    ('turns', lambda e: e['messages'], lambda v: f'{v:.0f}'),
    ('peak_context', lambda e: max(e['inputPerMsg'], default=0), _fmt_k),
    ('cache_read_cost', lambda e: e['cacheReadCost'], _fmt_short),
    ('cache_write_cost', lambda e: e['cacheWriteCost'], _fmt_short),
    ('output_cost', lambda e: e['outputCost'], _fmt_short),
    ('compactions', lambda e: len(e['compactions']), lambda v: f'{v:.0f}'),
]

# name, predicate selecting the sessions the metric even applies to, getter, formatter
_CONDITIONAL_RAW_METRICS = [
    ('spend_after_first_compaction', lambda e: bool(e['compactions']), lambda e: e['costAfterFirstCompaction'], _fmt_short),
]


def _percentile_lines(entries, minimum):
    out = []
    for name, getter, fmt in _RAW_METRICS:
        block = _percentile_block([getter(e) for e in entries], minimum=minimum)
        if block is None:
            continue
        parts = [f'{"p" + str(k) if k != "max" else "max"}={fmt(block[k])}' for k in (50, 75, 90, 95, 'max')]
        out.append(f'  {name}: ' + '  '.join(parts))
    return out


# Prevalence (how many sessions this even applies to) plus a percentile block computed only among
# those sessions: a session that never compacted or never crossed the context threshold has
# nothing to say about "how much cost lands there when it happens", so it shouldn't count as a p50
# of zero.
def _conditional_percentile_lines(entries, minimum):
    out = []
    for name, predicate, getter, fmt in _CONDITIONAL_RAW_METRICS:
        affected = [e for e in entries if predicate(e)]
        out.append(f'  {name}: {len(affected)}/{len(entries)} sessions affected')
        block = _percentile_block([getter(e) for e in affected], minimum=minimum)
        if block:
            parts = [f'{"p" + str(k) if k != "max" else "max"}={fmt(block[k])}' for k in (50, 75, 90, 95, 'max')]
            out.append(f'    among affected: ' + '  '.join(parts))
    return out


def _percentile_ranks(entry, entries):
    return {name: _percentile_rank(getter(entry), [getter(e) for e in entries]) for name, getter, _ in _RAW_METRICS}


def _fmt_ranks(ranks):
    return '  '.join(f'{name}=P{rank:.0f}' for name, rank in ranks.items() if rank is not None)


# A peer more than 2x (or less than half) the current session's turn count is a stretch to call
# "comparable" even after log-ratio ranking picked it as the closest available.
_PEER_TURN_RATIO_QUALITY_THRESHOLD = math.log(2)


# Symmetric turn-count distance: plain abs(a - b) treats 20-vs-40 (2x) as equally close as
# 220-vs-240 (1.09x), and log-ratio treats 20-vs-40 the same as 40-vs-20, neither of which
# abs(a/b - 1) gets right on its own.
def _turn_log_distance(a_turns, b_turns):
    return abs(math.log(max(a_turns, 1) / max(b_turns, 1)))


# Comparable sessions: same project + same primary model + same pricing regime (a session priced
# under an old rate isn't spending more or less "resources" than one under a new rate, it just
# has a different dollar figure for the same usage), ranked by turn-count log-ratio so a session
# twice as long looks equally distant whether the current session is the short or the long one.
# Deliberately not filtered by cost, peak context, or compactions, since those are what the
# comparison is meant to reveal, not a precondition for inclusion.
def _select_peers(entry, all_sessions, limit=_RAW_PEER_LIMIT):
    entry_regime = get_model_pricing(entry['model'], entry.get('firstTimestamp'))
    same = [
        e for e in all_sessions
        if e['id'] != entry['id'] and e['project'] == entry['project'] and e['model'] == entry['model']
        and get_model_pricing(e['model'], e.get('firstTimestamp')) == entry_regime
    ]
    same.sort(key=lambda e: _turn_log_distance(e['messages'], entry['messages']))
    peers = same[:limit]
    lower_cost_peers = sorted((e for e in peers if e['cost'] < entry['cost']), key=lambda e: e['cost'])[:_RAW_LOWER_COST_PEER_LIMIT]

    quality_reasons = []
    if len(same) < COHORT_MIN_FOR_PERCENTILES:
        quality_reasons.append(f'only {len(same)} session{"s" if len(same) != 1 else ""} matched project, model, and pricing regime')
    elif peers and max(_turn_log_distance(p['messages'], entry['messages']) for p in peers) > _PEER_TURN_RATIO_QUALITY_THRESHOLD:
        quality_reasons.append('even the closest peers differ substantially in turn count')
    entry_models = set(entry.get('models') or [])
    if len(entry_models) > 1 and peers:
        matching_mix = sum(1 for p in peers if set(p.get('models') or []) == entry_models)
        if matching_mix / len(peers) < 0.5:
            quality_reasons.append('current session used a multi-model mix most peers don\'t share')
    quality = 'low' if quality_reasons else 'high'

    # len(same), not len(peers): how many sessions actually matched project+model+regime, before
    # narrowing to the closest-by-turns subset, so a thin cohort is visible even when peers==same.
    return peers, lower_cost_peers, len(same), quality, quality_reasons


# Turn-by-turn cost/context curve for the current session only (never for the top-20 or peers,
# that would blow past "condensed"). Condensed: every turn if the session is short, otherwise a
# sample plus the tail plus the turns around the largest context jumps. Jumps carry the tools that
# ran in the *previous* turn as circumstantial context, not a causal attribution: the growth at a
# turn can include tool results, user messages, or other content beyond any single call.
def _build_turn_series(raw_session):
    costs = raw_session.get('costPerMsg') or []
    contexts = raw_session.get('inputPerMsg') or []
    tools_per_msg = raw_session.get('toolsPerMsg') or []
    n = len(costs)

    cumulative = 0.0
    points = []
    for i in range(n):
        cumulative += costs[i]
        points.append({'turn': i + 1, 'cumulative_cost': cumulative, 'context_tokens': contexts[i]})

    jumps = []
    for i in range(1, n):
        delta = contexts[i] - contexts[i - 1]
        if delta > 0:
            jumps.append((i, delta))
    jumps.sort(key=lambda j: j[1], reverse=True)
    top_jumps = jumps[:_CURVE_TOP_JUMPS]
    jump_entries = [
        {
            'turn': idx + 1,
            'tokens_added': delta,
            'tools_in_previous_turn': tools_per_msg[idx - 1] if idx - 1 < len(tools_per_msg) else [],
        }
        for idx, delta in top_jumps
    ]

    if n <= _CURVE_MAX_TURNS_FULL:
        kept_idx = set(range(n))
    else:
        step = max(1, n // _CURVE_SAMPLE_POINTS)
        kept_idx = set(range(0, n, step))
        kept_idx.update(range(max(0, n - _CURVE_TAIL_TURNS), n))
        for idx, _ in top_jumps:
            kept_idx.add(idx)
            kept_idx.add(max(0, idx - 1))
    condensed_points = [points[i] for i in sorted(kept_idx)]

    compaction_turns = [c['turnIndex'] for c in (raw_session.get('compactions') or []) if c.get('turnIndex') is not None]

    return condensed_points, jump_entries, compaction_turns


# A condensed, annotated counterpart to generate_markdown_report, meant to be read by an LLM
# directly from stdout rather than written to a file for a human. Condensed = top N sessions
# instead of every session, and annotated = every metric carries an inline one-line explanation
# (the terminal equivalent of the HTML report's hover tooltips) so the numbers don't need any
# extra context to reason about.
def _mcp_used_in_str(used_in, project_name_by_path):
    return ', '.join(f'{project_name_by_path.get(u["project"], u["project"])} ({u["count"]}x)' for u in used_in)


def _mcp_lines(mcp_servers, project_name_by_path):
    if not mcp_servers:
        return []
    lines = [
        '',
        "MCP SERVERS: a configured server's tool schemas are sent on every turn whether or not it's actually called. "
        "'global' scope loads in ALL projects, 'local' scope loads in only one. Usage below (call counts) is only what "
        "these sessions actually show via mcp__<server>__ tool calls, so 'no usage detected' means not observed here, not proven unused",
    ]
    for e in mcp_servers:
        scope_label = 'global' if e['scope'] == 'user' else 'local'
        if e['status'] == 'move-candidate':
            move_proj = project_name_by_path.get(e['moveTo'], e['moveTo'])
            lines.append(
                f'  [move candidate] {e["server"]} ({scope_label} scope, used in: {_mcp_used_in_str(e["usedIn"], project_name_by_path)}) '
                f'→ move it to {move_proj}\'s .mcp.json so it stops loading in every other project'
            )
        elif e['status'] == 'unused':
            project_part = f', {_mcp_project_display(e["project"], project_name_by_path)}' if e['scope'] != 'user' else ''
            lines.append(f'  [unused] {e["server"]} ({scope_label} scope{project_part}, no usage detected)')
        else:
            lines.append(f'  {e["server"]} ({scope_label} scope, used in: {_mcp_used_in_str(e["usedIn"], project_name_by_path)}) — fine as-is')
    return lines


def generate_raw_report(sessions, highlight_session=None, mcp_servers=None):
    generated_str = _format_generated(datetime.now())
    total_cost = sum(s['cost'] for s in sessions)
    total_cache_read = sum(s['usage']['cacheRead'] for s in sessions)
    total_cache_write = sum(s['usage']['cacheWrite'] for s in sessions)
    cache_hit_rate = (total_cache_read / (total_cache_read + total_cache_write)) if (total_cache_read + total_cache_write) > 0 else 0

    total_cache_savings = sum(
        c['cacheSavings'] for s in sessions for c in (s.get('perModelCost') or {}).values()
    )

    by_day = {}
    for s in sessions:
        for day, c in (s.get('perDay') or {}).items():
            by_day[day] = by_day.get(day, 0) + c

    today = datetime.now().date()
    last7_start, prev7_start, prev7_end = today - timedelta(days=6), today - timedelta(days=13), today - timedelta(days=7)
    last7_cost = sum(c for d, c in by_day.items() if d != 'unknown' and last7_start <= datetime.strptime(d, '%Y-%m-%d').date() <= today)
    prev7_cost = sum(c for d, c in by_day.items() if d != 'unknown' and prev7_start <= datetime.strptime(d, '%Y-%m-%d').date() <= prev7_end)
    daily_avg = last7_cost / 7
    week_change_pct = ((last7_cost - prev7_cost) / prev7_cost * 100) if prev7_cost > 0 else None
    week_change_str = f'{"+" if week_change_pct >= 0 else ""}{week_change_pct:.0f}% vs prev 7d' if week_change_pct is not None else 'no prior week to compare'

    by_model = {}
    for s in sessions:
        for m, c in (s.get('perModelCost') or {}).items():
            by_model[m] = by_model.get(m, 0) + c['total']
    model_entries = sorted(((m, c) for m, c in by_model.items() if c >= 0.001), key=lambda kv: kv[1], reverse=True)

    project_stats = {}
    for s in sessions:
        st = project_stats.setdefault(s['projectName'], {'count': 0, 'cost': 0.0, 'costs': []})
        st['count'] += 1
        st['cost'] += s['cost']
        st['costs'].append(s['cost'])
    top_project_stats = sorted(
        (
            (
                name, st['count'], st['cost'],
                _percentile(sorted(st['costs']), 50), _percentile(sorted(st['costs']), 90),
                st['count'] < COHORT_MIN_FOR_PERCENTILES,
            )
            for name, st in project_stats.items()
        ),
        key=lambda r: r[2], reverse=True,
    )[:_RAW_TOP_N_LISTS]

    sorted_by_cost_desc = sorted(sessions, key=lambda s: s['cost'], reverse=True)
    top10_count = max(1, math.ceil(len(sessions) * 0.1))
    cost_concentration_pct = None
    if len(sessions) >= MIN_SESSIONS_FOR_PERCENTILES:
        cost_concentration_pct = sum(s['cost'] for s in sorted_by_cost_desc[:top10_count]) / max(total_cost, 1e-9) * 100

    global_repeated_calls = {}
    for s in sessions:
        for r in (s.get('repeatedCalls') or []):
            key = (r['tool'], r['descriptor'])
            agg = global_repeated_calls.setdefault(key, {'extra': 0, 'sessions': 0, 'chars': 0})
            agg['extra'] += r['count'] - 1
            agg['sessions'] += 1
            agg['chars'] += r.get('chars', 0)
    top_repeated_calls = sorted(
        ({'tool': tool, 'descriptor': descriptor, 'extra': agg['extra'], 'sessions': agg['sessions'], 'chars': agg['chars']}
         for (tool, descriptor), agg in global_repeated_calls.items()),
        key=lambda r: r['chars'], reverse=True,
    )[:_RAW_TOP_N_LISTS]
    top_repeated_failures = _aggregate_repeated_failures(sessions, limit=_RAW_TOP_N_LISTS)

    observed_compact_floor = {}
    for s in sessions:
        for c in (s.get('compactions') or []):
            if c.get('trigger') != 'auto' or not c.get('preTokens') or not c.get('model'):
                continue
            m = c['model']
            observed_compact_floor[m] = min(observed_compact_floor.get(m, float('inf')), c['preTokens'])

    all_sessions = [_session_entry(s, idx, len(sessions), observed_compact_floor) for idx, s in enumerate(sorted_by_cost_desc)]
    sessions_by_id = {s['sessionId']: s for s in sessions}
    avg_cost_per_session = (total_cost / len(sessions)) if sessions else 0.0

    component_entries = sorted([
        ('Cache read', sum(e['cacheReadCost'] for e in all_sessions), 'reusing context already built, cheap per-token but adds up in long sessions'),
        ('Cache write', sum(e['cacheWriteCost'] for e in all_sessions), 'building context into the cache for later reuse'),
        ('Output', sum(e['outputCost'] for e in all_sessions), 'tokens Claude generated'),
        ('Fresh input', sum(e['inputCost'] for e in all_sessions), "new tokens sent that weren't cached, often prompts/tool results not yet cached"),
    ], key=lambda x: x[1], reverse=True)

    def _session_line(e, prefix):
        why, review = _session_why_review(e)
        title = _md_escape(e['title'])
        project = _md_escape(e['project'])
        peak_ctx = _fmt_k(max(e['inputPerMsg'], default=0))
        marker = '  ← current session' if highlight_session and e['id'] == highlight_session else ''
        return (
            f'{prefix}{e["date"]}  {project}  "{title}"  {e["modelShort"]}  {_fmt_short(e["cost"])}  turns={e["messages"]}  peak_ctx={peak_ctx}  id={e["id"]}{marker}\n'
            f'      why: {why}, review: {review}'
        )

    lines = [
        f'BURNIE RAW REPORT, generated {generated_str} · {len(sessions)} sessions across {len(project_stats)} projects · prices as of {PRICING_UPDATED}',
        '',
        'SUMMARY',
        f'  total_cost={_fmt_short(total_cost)}   total spend across all sessions on disk',
        f'  avg_cost_per_session={_fmt_short(avg_cost_per_session)}   mean cost per session, a quick figure only. See GLOBAL PERCENTILES below for the real baseline',
        f'  daily_avg_7d={_fmt_short(daily_avg)}   last 7 calendar days incl. zero-activity days, {week_change_str}',
    ]
    if cost_concentration_pct is not None:
        lines.append(f'  cost_concentration={cost_concentration_pct:.0f}%   % of total cost from priciest 10% of sessions ({top10_count} of {len(sessions)}). High = a few sessions dominate, look at those first')

    lines += [
        '',
        'TECHNICAL CONTEXT: background on caching, not directly actionable, rarely worth leading with',
        f'  cache_hit_rate={cache_hit_rate * 100:.0f}%   share of tokens served from cache vs fresh, mostly reflects session length, not efficiency',
        f'  cache_savings={_fmt_short(total_cache_savings)}   estimated cost avoided by caching (a technical counterfactual, not money recoverable by changing behavior)',
        '',
        "GLOBAL PERCENTILES: the average is skewed by the same expensive sessions you're trying to explain. Use these percentiles as the actual baseline",
    ]
    percentile_lines = _percentile_lines(all_sessions, minimum=MIN_SESSIONS_FOR_PERCENTILES)
    lines += percentile_lines if percentile_lines else [f'  not enough sessions yet (need {MIN_SESSIONS_FOR_PERCENTILES}+, have {len(all_sessions)})']

    lines += [
        '',
        'LONG-SESSION EXPOSURE: prevalence first, then how much cost lands there among only the sessions it actually applies to. Not a claim that compacting or long context caused the cost',
    ]
    lines += _conditional_percentile_lines(all_sessions, minimum=MIN_SESSIONS_FOR_PERCENTILES)

    lines += ['', 'COST BY COMPONENT: where the $ actually went, by token type']
    lines += [f'  {name}: {_fmt_short(c)} ({_pct_of(c, total_cost)})   {note}' for name, c, note in component_entries]

    lines += ['', 'COST BY MODEL']
    lines += [f'  {m.replace("claude-", "")}: {_fmt_short(c)} ({_pct_of(c, total_cost)})' for m, c in model_entries]

    if top_project_stats:
        lines += ['', f'COST BY PROJECT (top {len(top_project_stats)} of {len(project_stats)}): median/p90 shown alongside total since a couple of expensive sessions can skew a project\'s average']
        lines += [
            f'  {name}: {count} sessions, {_fmt_short(cost)} total, median={_fmt_short(median)}, p90={_fmt_short(p90)}'
            + ('   (few sessions, treat median/p90 as low-confidence)' if low_cohort else '')
            for name, count, cost, median, p90, low_cohort in top_project_stats
        ]

    highlight_entry, highlight_rank = None, None
    if highlight_session:
        for idx, e in enumerate(all_sessions):
            if e['id'] == highlight_session:
                highlight_entry, highlight_rank = e, idx + 1
                break
    if highlight_entry:
        raw_highlight = sessions_by_id.get(highlight_session)
        files_touched = len((raw_highlight or {}).get('filesChanged') or [])
        rank_str = f'#{highlight_rank} most expensive of {len(all_sessions)}' if highlight_rank <= _RAW_TOP_N_SESSIONS else f'rank {highlight_rank} of {len(all_sessions)} by cost (outside the top {_RAW_TOP_N_SESSIONS} below)'
        lines += [
            '', 'CURRENT SESSION: the session this report was invoked from (--session), a snapshot. If it\'s still running, later turns aren\'t reflected here',
            _session_line(highlight_entry, '  '),
            f'    rank: {rank_str}  ·  files_touched={files_touched}',
            f'    percentile_rank (global, vs. all {len(all_sessions)} sessions): {_fmt_ranks(_percentile_ranks(highlight_entry, all_sessions))}',
        ]
        # Explicit dollar figure, not just its percentile rank. Without this, describing "how
        # much landed after the first compaction" has no sourced number to quote, and gets
        # estimated by eyeballing the COST CURVE instead, which is not reliable.
        if highlight_entry['compactions']:
            lines.append(
                f'    spend_after_first_compaction={_fmt_short(highlight_entry["costAfterFirstCompaction"])}   '
                'cost recorded from the turn after the first compaction onward, describes WHEN it accumulated, not proof the compaction caused it'
            )

        peers, lower_cost_peers, cohort_candidates, comparison_quality, quality_reasons = _select_peers(highlight_entry, all_sessions)
        peer_turn_range = f'{min(p["messages"] for p in peers)}-{max(p["messages"] for p in peers)}' if peers else '-'
        lines += [
            '',
            '  COMPARABLE SESSIONS (peers): same project + same primary model + same pricing regime, closest by turn-count ratio. Not filtered by cost, context, or compactions: those are what you\'re comparing',
            '    criteria: same project + same primary model + same pricing regime, nearest by turn-count ratio (20↔40 turns counts as equally distant as 220↔240)',
            f'    cohort_candidates={cohort_candidates}   (sessions matching project+model+pricing regime, before narrowing to the closest {_RAW_PEER_LIMIT} by turn-count ratio)',
            f'    comparable_sessions_count={len(peers)}   current_turns={highlight_entry["messages"]}   peer_turn_range={peer_turn_range}',
            f'    comparison_quality={comparison_quality}' + (f'   reason={", ".join(quality_reasons)}' if quality_reasons else ''),
        ]
        if cohort_candidates < COHORT_MIN_FOR_PERCENTILES:
            lines.append(f'    not enough peers for percentiles (need {COHORT_MIN_FOR_PERCENTILES}+, have {cohort_candidates})')
        else:
            lines.append(f'    percentile_rank (cohort, vs. {len(peers)} peers): {_fmt_ranks(_percentile_ranks(highlight_entry, peers))}')
            lines += ['    ' + l for l in _percentile_lines(peers, minimum=COHORT_MIN_FOR_PERCENTILES)]
            lines += ['    ' + l for l in _conditional_percentile_lines(peers, minimum=COHORT_MIN_FOR_PERCENTILES)]
        if peers:
            lines.append('    peers:')
            lines += [f'      {p["date"]} {p["modelShort"]} cost={_fmt_short(p["cost"])} turns={p["messages"]} peak_ctx={_fmt_k(max(p["inputPerMsg"], default=0))} id={p["id"]}' for p in peers]
        if lower_cost_peers:
            lines.append('    lower-cost peers (subset of the above, cheaper than the current session):')
            lines += [f'      {p["date"]} cost={_fmt_short(p["cost"])} turns={p["messages"]} id={p["id"]}' for p in lower_cost_peers]

        if raw_highlight:
            curve_points, jump_entries, compaction_turns = _build_turn_series(raw_highlight)
            lines += ['', '  COST CURVE: cumulative cost and context by turn, use it to see WHEN it got expensive, not just that it did']
            if compaction_turns:
                lines.append(f'    compaction_turns={compaction_turns}')
            lines.append(f'    turn_series ({len(curve_points)} of {highlight_entry["messages"]} turns shown):')
            lines += [f'      turn {p["turn"]}: cumulative=${p["cumulative_cost"]:.2f} context={_fmt_k(p["context_tokens"])}' for p in curve_points]
            if jump_entries:
                lines.append('    largest context jumps (circumstantial: tools active in the turn before the jump, not a causal attribution):')
                lines += [
                    f'      turn {j["turn"]}: +{_fmt_k(j["tokens_added"])} tokens, previous turn included: {", ".join(j["tools_in_previous_turn"]) or "no tool calls"}'
                    for j in jump_entries
                ]

    top20_note = 'use COMPARABLE SESSIONS above for that' if highlight_entry else 'pass --session to get a COMPARABLE SESSIONS cohort for a specific session'
    lines += ['', f'TOP {_RAW_TOP_N_SESSIONS} MOST EXPENSIVE SESSIONS: global cost patterns across all sessions, not a "what\'s normal" baseline for any single session, {top20_note}']
    for i, e in enumerate(all_sessions[:_RAW_TOP_N_SESSIONS], start=1):
        lines.append(_session_line(e, f'  {i:>2}. '))

    if top_repeated_failures:
        lines += [
            '', 'RECURRING FAILURES, same tool call failing with the exact same error, recurring across 2+ separate sessions: a persistently broken command or script, not just one session\'s own thrashing loop',
        ]
        lines += [
            f'  {r["tool"]}  {_md_escape(r["descriptor"] or "")}  → {_md_escape(r["message"])}  ({r["count"]}× across {r["sessions"]} session{"s" if r["sessions"] != 1 else ""})'
            for r in top_repeated_failures
        ]

    if top_repeated_calls:
        lines += [
            '', 'FREQUENTLY ACCESSED TARGETS: background, not a finding. Matched on path/command/pattern only, not the full call, so different offsets into the same file or different search scopes still count as the same target, and repetition alone doesn\'t imply avoidable work',
        ]
        lines += [
            f'  {r["tool"]}  {_md_escape(r["descriptor"])}  ({r["extra"]} extra calls across {r["sessions"]} session{"s" if r["sessions"] != 1 else ""}, ~{_fmt_big(r["chars"])} chars returned)'
            for r in top_repeated_calls
        ]

    project_name_by_path = {s['projectPath']: s['projectName'] for s in sessions}
    lines += _mcp_lines(mcp_servers, project_name_by_path)

    return '\n'.join(lines) + '\n'


_STATIC_HEAD = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Where did all my money go?</title>
<style>
  :root[data-theme="dark"] {
    --bg: #0f0f13; --surface: #1a1a24; --surface2: #22223a; --surface3: #1e1e30;
    --border: #2e2e45; --text: #e2e2f0; --muted: #8888aa;
  }
  :root[data-theme="light"] {
    --bg: #f5f5fa; --surface: #ffffff; --surface2: #eeeef6; --surface3: #f0f0f8;
    --border: #d4d4e8; --text: #1a1a2e; --muted: #666688;
  }
  :root { --accent: #6366f1; --accent2: #a78bfa; --green: #10b981; --amber: #f59e0b; --cyan: #06b6d4; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { scroll-behavior: smooth; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; line-height: 1.5; transition: background 0.2s, color 0.2s; }
  .container { max-width: 1280px; margin: 0 auto; padding: 32px 24px; }
  header { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 40px; gap: 16px; }
  header h1 { font-size: 28px; font-weight: 700; background: linear-gradient(135deg, var(--accent), var(--accent2)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
  header p { color: var(--muted); margin-top: 4px; font-size: 13px; }
  .theme-btn { flex-shrink: 0; display: flex; align-items: center; gap: 8px; background: var(--surface); border: 1px solid var(--border); color: var(--muted); border-radius: 8px; padding: 8px 14px; font-size: 13px; cursor: pointer; transition: color 0.15s, border-color 0.15s; white-space: nowrap; }
  .theme-btn:hover { color: var(--text); border-color: var(--accent); }
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 40px; }
  .kpi { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px 24px; transition: background 0.2s, border-color 0.2s; }
  .kpi-wide { grid-column: span 2; }
  .kpi-doc-link { display: inline-flex; align-items: center; justify-content: center; width: 14px; height: 14px; border-radius: 99px; background: var(--surface2); color: var(--muted); font-size: 9px; font-weight: 700; text-decoration: none; margin-left: 5px; vertical-align: middle; }
  .kpi-doc-link:hover { background: var(--accent); color: #fff; }
  .kpi-model-list { display: flex; flex-direction: column; gap: 7px; margin-top: 10px; }
  .kpi-model-row { display: grid; grid-template-columns: 140px 1fr 70px; align-items: center; gap: 10px; font-size: 12px; }
  .kpi-model-name { color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-family: monospace; }
  .kpi-model-bar-wrap { background: var(--surface2); border-radius: 3px; height: 6px; }
  .kpi-model-bar { height: 6px; border-radius: 3px; background: var(--accent); }
  .kpi-model-cost { text-align: right; font-weight: 600; color: var(--accent2); font-variant-numeric: tabular-nums; }
  .kpi-label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 8px; }
  .tip { position: relative; display: inline-flex; align-items: center; justify-content: center; width: 14px; height: 14px; border-radius: 99px; background: var(--surface2); color: var(--muted); font-size: 9px; font-weight: 700; cursor: default; margin-left: 5px; vertical-align: middle; }
  .tip::after { content: attr(data-tip); position: absolute; bottom: calc(100% + 6px); left: 50%; transform: translateX(-50%); background: var(--text); color: var(--bg); font-size: 11px; font-weight: 400; letter-spacing: 0; text-transform: none; white-space: normal; width: 220px; padding: 6px 10px; border-radius: 6px; line-height: 1.4; pointer-events: none; opacity: 0; transition: opacity 0.15s; z-index: 10; }
  .tip:hover::after, .tip:focus::after, .tip.tip-open::after { opacity: 1; }
  .tip:focus { outline: 2px solid var(--accent); outline-offset: 2px; }
  .kpi-value { font-size: 26px; font-weight: 700; color: var(--text); }
  .kpi-sub { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .kpi.accent .kpi-value { color: var(--accent2); }
  .kpi.green  .kpi-value { color: var(--green); }
  .kpi.amber  .kpi-value { color: var(--amber); }
  .kpi-month { display: flex; justify-content: space-between; align-items: baseline; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border); }
  .kpi-month-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
  .kpi-month-value { font-size: 19px; font-weight: 700; color: var(--text); font-variant-numeric: tabular-nums; }
  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 40px; }
  .chart-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 24px; transition: background 0.2s, border-color 0.2s; }
  .chart-card.wide { grid-column: span 2; }
  .chart-card h2 { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 20px; }
  .chart-wrap { position: relative; }

  /* ── Sessions panel ─────────────────────────────── */
  .sessions-panel { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin-bottom: 40px; }
  .sessions-header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
  .sessions-header h2 { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }
  .sessions-controls { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .ctrl-input, .ctrl-select { background: var(--surface2); border: 1px solid var(--border); color: var(--text); border-radius: 7px; padding: 6px 10px; font-size: 13px; outline: none; }
  .ctrl-input { width: 200px; }
  .ctrl-input::placeholder { color: var(--muted); }
  .ctrl-select { cursor: pointer; }
  .sort-btn { background: var(--surface2); border: 1px solid var(--border); color: var(--muted); border-radius: 7px; padding: 6px 10px; font-size: 12px; cursor: pointer; }
  .sort-btn.active { border-color: var(--accent); color: var(--accent2); }
  .day-chip { display: inline-flex; align-items: center; gap: 6px; background: var(--accent); color: #fff; border-radius: 99px; padding: 4px 10px 4px 12px; font-size: 12px; }
  .day-chip button { background: none; border: none; color: #fff; cursor: pointer; font-size: 14px; line-height: 1; padding: 0; opacity: 0.8; }
  .day-chip button:hover { opacity: 1; }
  canvas { cursor: pointer; }
  .sessions-count { font-size: 12px; color: var(--muted); }

  table { width: 100%; border-collapse: collapse; }
  .table-scroll { overflow-x: auto; }
  .table-scroll table { min-width: 640px; }
  th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.07em; color: var(--muted); padding: 10px 12px; border-bottom: 1px solid var(--border); font-weight: 600; }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  .session-row { cursor: pointer; }
  .session-row:hover td { background: var(--surface2); }
  .session-row.highlighted td { background: var(--surface2); border-left: 3px solid var(--accent); }
  .session-row td:first-child { width: 28px; color: var(--muted); font-size: 16px; user-select: none; }
  .detail-row td { padding: 0; border-bottom: 1px solid var(--border); }
  .detail-inner { padding: 16px 48px 20px; background: var(--surface3); display: none; }
  .detail-inner.open { display: block; }
  .breakdown-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
  .breakdown-item { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
  .breakdown-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px; }
  .breakdown-tokens { font-size: 18px; font-weight: 700; color: var(--text); font-variant-numeric: tabular-nums; }
  .breakdown-cost { font-size: 12px; color: var(--accent2); margin-top: 2px; font-variant-numeric: tabular-nums; }
  .breakdown-bar { height: 4px; border-radius: 2px; margin-top: 8px; }
  .cost-pill { font-weight: 600; color: var(--accent2); font-variant-numeric: tabular-nums; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 99px; background: var(--surface2); font-size: 11px; color: var(--muted); }
  .tag-clickable { cursor: pointer; }
  .tag-clickable:hover { background: var(--accent); color: #fff; }
  .inline-link { cursor: pointer; color: var(--text); font-weight: 600; text-decoration: underline dotted; text-underline-offset: 2px; }
  .inline-link:hover { color: var(--accent); }
  .truncate { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: inline-block; vertical-align: bottom; }
  .insight-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px 24px; }
  .insight-card h2 { font-size: 14px; font-weight: 600; margin: 0 0 14px 0; }
  .attention-card { border-left: 3px solid var(--accent); margin-bottom: 24px; }
  .attention-list { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 8px; font-size: 13px; line-height: 1.5; color: var(--text); }
  .attention-list li::marker { color: var(--accent); }
  .insight-sub { font-size: 11px; font-weight: 400; color: var(--muted); margin-left: 4px; }
  .insight-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .insight-table tr { border-bottom: 1px solid var(--border); }
  .insight-table tr:last-child { border-bottom: none; }
  .insight-table td { padding: 5px 4px; vertical-align: middle; }
  .insight-file { font-family: monospace; color: var(--text); max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: copy; }
  .insight-file:hover { color: var(--accent); }
  .insight-path { color: var(--muted); font-family: monospace; font-size: 10px; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .insight-subline { color: var(--muted); font-size: 10px; font-weight: 400; margin-top: 2px; white-space: normal; }
  .insight-count { text-align: right; font-weight: 700; color: var(--accent); padding-left: 8px; white-space: nowrap; }
  .session-meta-row { display: flex; gap: 20px; margin-bottom: 14px; flex-wrap: wrap; }
  .session-meta-item { display: flex; flex-direction: column; gap: 2px; }
  .session-meta-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
  .session-meta-value { font-size: 14px; font-weight: 600; color: var(--text); font-variant-numeric: tabular-nums; }
  .session-meta-copy { display: inline-flex; align-items: center; gap: 5px; font-family: monospace; font-size: 11px; font-weight: 500; cursor: pointer; max-width: 320px; }
  .session-meta-copy:hover { color: var(--accent); }
  .session-meta-copy-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .session-meta-copy-icon { flex-shrink: 0; opacity: 0.6; font-size: 12px; }
  .session-meta-copy:hover .session-meta-copy-icon { opacity: 1; }
  .hit-rate-good { color: #10b981; }
  .hit-rate-mid  { color: #f59e0b; }
  .hit-rate-low  { color: #ef4444; }
  .sparkline-wrap { margin-top: 14px; }
  .sparkline-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin-bottom: 4px; }
  .detail-sections { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-top: 16px; }
  .detail-section { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 14px; min-width: 0; display: flex; flex-direction: column; }
  .detail-section h4 { font-size: 11px; text-transform: uppercase; letter-spacing: 0.07em; color: var(--muted); margin: 0 0 10px 0; font-weight: 600; }
  .file-list { list-style: none; margin: 0; padding: 0 8px 0 0; font-size: 11px; font-family: monospace; max-height: 160px; overflow-y: auto; scrollbar-width: thin; scrollbar-color: var(--border) transparent; }
  .file-list::-webkit-scrollbar { width: 6px; }
  .file-list::-webkit-scrollbar-track { background: transparent; }
  .file-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  .file-list::-webkit-scrollbar-thumb:hover { background: var(--muted); }
  .file-list li { padding: 3px 0; border-bottom: 1px solid var(--border); }
  .file-list li:last-child { border-bottom: none; }
  .empty-note { color: var(--muted); font-size: 12px; font-style: italic; }

  @media (max-width: 768px) {
    .charts-grid { grid-template-columns: 1fr; }
    .chart-card.wide { grid-column: span 1; }
    .breakdown-grid { grid-template-columns: 1fr 1fr; }
    .ctrl-input { width: 140px; }
  }
</style>
</head>
<body>
<div class="container">
"""


_STATIC_SESSIONS_PANEL = """  <!-- Sessions detail panel -->
  <div class="sessions-panel">
    <div class="sessions-header">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <h2>All sessions</h2>
        <span class="sessions-count" id="sessionsCount"></span>
        <span id="dayChip" style="display:none"></span>
        <span id="projectChip" style="display:none"></span>
        <span id="topNChip" style="display:none"></span>
        <span id="sessionIdChip" style="display:none"></span>
      </div>
      <div class="sessions-controls">
        <input class="ctrl-input" id="searchInput" type="text" placeholder="Search title…" oninput="renderTable()">
        <select class="ctrl-select" id="projectFilter" onchange="renderTable()">
          <option value="">All projects</option>
        </select>
        <button class="sort-btn active" id="sortCost"     onclick="setSort('cost')">Cost ↓</button>
        <button class="sort-btn"        id="sortDate"     onclick="setSort('date')">Date</button>
        <button class="sort-btn"        id="sortMessages" onclick="setSort('messages')">Messages</button>
      </div>
    </div>
    <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th></th>
          <th>Title</th>
          <th>Project</th>
          <th>Date</th>
          <th>Model</th>
          <th style="text-align:right">Msgs</th>
          <th style="text-align:right">Cost</th>
        </tr>
      </thead>
      <tbody id="sessionsTbody"></tbody>
    </table>
    </div>
    <div id="noResults" style="display:none;padding:24px;text-align:center;color:var(--muted);font-size:13px">No sessions match your filter.</div>
  </div>
  <footer style="text-align:center;margin-top:32px;padding-top:20px;border-top:1px solid var(--border);color:var(--muted);font-size:12px">
    Generated with <a href="https://github.com/czoido/burnie" target="_blank" style="color:var(--muted)">burnie</a>
  </footer>
</div>

<script>
"""

# Everything below runs client-side in the browser once the report is opened, so it has no
# access to Python values, only to the DATA/ALL_SESSIONS consts injected just above it.
_STATIC_SCRIPT_BODY = """// ── theme ──────────────────────────────────────────
function getTheme() { return document.documentElement.getAttribute('data-theme') }
function applyThemeToCharts(theme) {
  const label = theme === 'dark' ? '#8888aa' : '#666688'
  const grid  = theme === 'dark' ? '#2e2e4540' : '#d4d4e840'
  Chart.defaults.color = label
  Chart.defaults.borderColor = grid
  Object.values(Chart.instances).forEach(c => {
    c.options.scales && Object.values(c.options.scales).forEach(ax => {
      if (ax.grid)  ax.grid.color  = grid
      if (ax.ticks) ax.ticks.color = label
    })
    if (c.options.plugins?.legend?.labels) c.options.plugins.legend.labels.color = label
    c.update('none')
  })
}
function toggleTheme() {
  const next = getTheme() === 'dark' ? 'light' : 'dark'
  document.documentElement.setAttribute('data-theme', next)
  document.getElementById('themeIcon').textContent  = next === 'dark' ? '☀️' : '🌙'
  document.getElementById('themeLabel').textContent = next === 'dark' ? 'Light mode' : 'Dark mode'
  applyThemeToCharts(next)
}

// ── charts ─────────────────────────────────────────
Chart.defaults.color = '#8888aa'
Chart.defaults.borderColor = '#2e2e4540'
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
Chart.defaults.font.size = 12

new Chart(document.getElementById('chartDaily'), {
  type: 'bar',
  data: { labels: DAYS, datasets: [
    { label: 'Sessions', data: DAY_SESSIONS, backgroundColor: '#06b6d428', borderColor: '#06b6d460', borderWidth: 1, borderRadius: 4, yAxisID: 'ySessions', order: 3 },
    { label: 'Daily cost', data: DAY_COSTS, backgroundColor: '#6366f180', borderColor: '#6366f1', borderWidth: 1, borderRadius: 4, yAxisID: 'yCost', order: 2 },
    { label: '7-day avg', data: ROLLING, type: 'line', borderColor: '#a78bfa', borderWidth: 2, pointRadius: 0, tension: 0.4, fill: false, yAxisID: 'yCost', order: 1 },
  ]},
  options: { responsive: true, maintainAspectRatio: false,
    plugins: { legend: { position: 'top' }, tooltip: { callbacks: { label: ctx => ctx.dataset.yAxisID === 'ySessions' ? ' ' + ctx.parsed.y + ' sessions' : ' $' + ctx.parsed.y.toFixed(2) } } },
    scales: {
      x: { grid: { color: '#2e2e4540' }, ticks: { maxTicksLimit: 14 } },
      yCost:     { position: 'left',  grid: { color: '#2e2e4540' }, ticks: { callback: v => '$' + v.toFixed(2) } },
      ySessions: { position: 'right', grid: { display: false }, ticks: { stepSize: 1, color: '#06b6d4' }, title: { display: true, text: 'sessions', color: '#06b6d4', font: { size: 10 } } },
    },
    onClick: (_, els) => { if (els[0]) setDayFilter(dayFilter === DAYS[els[0].index] ? null : DAYS[els[0].index]) },
  },
})
if (document.getElementById('chartEntrypoint')) {
  new Chart(document.getElementById('chartEntrypoint'), {
    type: 'doughnut',
    data: { labels: ENTRYPOINT_NAMES, datasets: [{ data: ENTRYPOINT_COSTS, backgroundColor: COLORS, borderWidth: 0, hoverOffset: 8 }] },
    options: { responsive: true, maintainAspectRatio: false, cutout: '60%',
      plugins: { legend: { position: 'right', labels: { boxWidth: 12, font: { size: 11 } } }, tooltip: { callbacks: { label: ctx => ' $' + ctx.parsed.toFixed(2) + ': ' + ctx.label } } },
    },
  })
}

// ── sessions table ─────────────────────────────────
const fmt = (n) => n >= 0.01 ? '$' + n.toFixed(2) : '$' + n.toFixed(4)
const fmtK = (n) => n >= 1000 ? (n / 1000).toFixed(1) + 'K' : n.toString()
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))

// Feedback swaps just the icon (clipboard -> check) rather than the value text, so the copied
// path/id stays visible instead of being replaced by a "Copied!" message.
function copyToClipboard(text, iconEl) {
  navigator.clipboard.writeText(text)
  const original = iconEl.textContent
  iconEl.textContent = '✓'
  setTimeout(() => { iconEl.textContent = original }, 1200)
}

// Tooltips otherwise only show on :hover, delegated here (not per-element) since most .tip
// spans are re-created on every renderTable() call inside the session detail rows.
document.addEventListener('click', (e) => {
  const tip = e.target.closest('.tip')
  document.querySelectorAll('.tip.tip-open').forEach(t => { if (t !== tip) t.classList.remove('tip-open') })
  if (tip) tip.classList.toggle('tip-open')

  const bar = e.target.closest('.project-bar-row')
  if (bar) {
    const current = document.getElementById('projectFilter').value
    setProjectFilter(current === bar.dataset.project ? null : bar.dataset.project)
  }
})

const SORT_LABELS = { cost: 'Cost', date: 'Date', messages: 'Messages' }
let sortKey = 'cost'
let sortDir = 'desc'
let openRow = null
let dayFilter = null
let topNFilter = null
let sessionIdFilter = null
let sessionIdSortField = null

function setDayFilter(day) {
  dayFilter = day
  const chip = document.getElementById('dayChip')
  if (day) {
    chip.style.display = 'inline-flex'
    chip.innerHTML = `<span class="day-chip">${day}<button onclick="setDayFilter(null)" title="Clear">✕</button></span>`
    document.getElementById('sessionsTbody').closest('.sessions-panel').scrollIntoView({ behavior: 'smooth', block: 'start' })
  } else {
    chip.style.display = 'none'
    chip.innerHTML = ''
  }
  renderTable()
}

function setProjectFilter(project) {
  const sel = document.getElementById('projectFilter')
  sel.value = project || ''
  const chip = document.getElementById('projectChip')
  if (project) {
    chip.style.display = 'inline-flex'
    chip.innerHTML = `<span class="day-chip">${escapeHtml(project)}<button onclick="setProjectFilter(null)" title="Clear">✕</button></span>`
    document.getElementById('sessionsTbody').closest('.sessions-panel').scrollIntoView({ behavior: 'smooth', block: 'start' })
  } else {
    chip.style.display = 'none'
    chip.innerHTML = ''
  }
  renderTable()
}

// Populate project filter
const projects = [...new Set(ALL_SESSIONS.map(s => s.project))].sort()
const sel = document.getElementById('projectFilter')
sel.onchange = () => setProjectFilter(sel.value || null)
projects.forEach(p => { const o = document.createElement('option'); o.value = p; o.textContent = p; sel.appendChild(o) })

function updateSortButtons() {
  for (const key of Object.keys(SORT_LABELS)) {
    const btn = document.getElementById('sort' + key.charAt(0).toUpperCase() + key.slice(1))
    btn.classList.toggle('active', key === sortKey)
    btn.textContent = SORT_LABELS[key] + (key === sortKey ? (sortDir === 'desc' ? ' ↓' : ' ↑') : '')
  }
}

function setSort(key) {
  sortDir = (sortKey === key) ? (sortDir === 'desc' ? 'asc' : 'desc') : 'desc'
  sortKey = key
  updateSortButtons()
  renderTable()
}

function setTopNFilter(n) {
  topNFilter = topNFilter === n ? null : n
  const chip = document.getElementById('topNChip')
  if (topNFilter) {
    chip.style.display = 'inline-flex'
    chip.innerHTML = `<span class="day-chip">Top ${topNFilter} priciest<button onclick="setTopNFilter(null)" title="Clear">✕</button></span>`
  } else {
    chip.style.display = 'none'
    chip.innerHTML = ''
  }
  renderTable()
  document.getElementById('sessionsTbody').closest('.sessions-panel').scrollIntoView({ behavior: 'smooth', block: 'start' })
}

// A specific set of flagged session ids (from a "What deserves attention" card), not a column
// value. An inline-link finding, not a filter widget with its own state to remember, so it
// clears itself out via clearSessionIdFilter's ✕ rather than a select/input the user re-drives.
function setSessionIdFilter(ids, sortField) {
  sessionIdFilter = new Set(ids)
  sessionIdSortField = sortField || null
  const chip = document.getElementById('sessionIdChip')
  chip.style.display = 'inline-flex'
  chip.innerHTML = `<span class="day-chip">${ids.length} flagged sessions<button onclick="clearSessionIdFilter()" title="Clear">✕</button></span>`
  renderTable()
  document.getElementById('sessionsTbody').closest('.sessions-panel').scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function clearSessionIdFilter() {
  sessionIdFilter = null
  sessionIdSortField = null
  const chip = document.getElementById('sessionIdChip')
  chip.style.display = 'none'
  chip.innerHTML = ''
  renderTable()
}

// Opens the sessions table on exactly one session, regardless of whatever filters/search/sort
// are currently active. Used by "Review session →" links in "What deserves attention", which
// need to reach a session even if it's currently hidden by an unrelated filter. Mirrors the
// HIGHLIGHT_SESSION open-on-load behavior below, as a reusable function instead of load-only code.
function clearAllFilters() {
  document.getElementById('searchInput').value = ''
  setDayFilter(null)
  setProjectFilter(null)
  topNFilter = null
  document.getElementById('topNChip').style.display = 'none'
  document.getElementById('topNChip').innerHTML = ''
  clearSessionIdFilter()
}

function openSession(id) {
  clearAllFilters()
  const idx = filtered().findIndex(s => s.id === id)
  if (idx === -1) return
  toggleRow(idx)
  setTimeout(() => document.getElementById('row-' + idx)?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 100)
}

function filtered() {
  const q  = document.getElementById('searchInput').value.toLowerCase()
  const pr = document.getElementById('projectFilter').value
  const dir = sortDir === 'asc' ? 1 : -1
  const rows = ALL_SESSIONS
    .filter(s =>
      (!q  || s.title.toLowerCase().includes(q) || s.project.toLowerCase().includes(q)
          || s.models.some(m => m.toLowerCase().includes(q)) || s.id.toLowerCase().includes(q)) &&
      (!pr || s.project === pr) &&
      // s.days covers every day the session had activity on, so a session that started
      // before the clicked day but ran into it still shows up (unlike a plain s.date check).
      (!dayFilter || s.days.includes(dayFilter)) &&
      (!topNFilter || s.costRank <= topNFilter) &&
      (!sessionIdFilter || sessionIdFilter.has(s.id))
    )
  // A flagged-session set sorts by whatever earned it the flag (e.g. costAfterFirstCompaction),
  // not the table's current cost/date/messages sort, that's the whole point of "ordered by the
  // number that made these worth reviewing" rather than whatever the user last clicked.
  if (sessionIdFilter && sessionIdSortField) {
    return rows.sort((a, b) => b[sessionIdSortField] - a[sessionIdSortField])
  }
  return rows.sort((a, b) => dir * (sortKey === 'date' ? a.date.localeCompare(b.date) : sortKey === 'messages' ? a.messages - b.messages : a.cost - b.cost))
}

function toggleRow(idx) {
  const detail = document.getElementById('detail-' + idx)
  const arrow  = document.getElementById('arrow-' + idx)
  const inner  = detail.querySelector('.detail-inner')
  if (openRow !== null && openRow !== idx) {
    document.getElementById('detail-' + openRow)?.querySelector('.detail-inner')?.classList.remove('open')
    document.getElementById('arrow-' + openRow).textContent = '▶'
  }
  const isOpen = inner.classList.toggle('open')
  arrow.textContent = isOpen ? '▼' : '▶'
  openRow = isOpen ? idx : null
}

function renderTable() {
  const rows = filtered()
  const tbody = document.getElementById('sessionsTbody')
  document.getElementById('sessionsCount').textContent = rows.length + ' of ' + ALL_SESSIONS.length
  document.getElementById('noResults').style.display = rows.length === 0 ? 'block' : 'none'
  tbody.innerHTML = ''
  openRow = null

  rows.forEach((s, i) => {
    const total = s.cost || 1
    const bars = [
      { pct: s.inputCost      / total * 100, color: '#6366f1' },
      { pct: s.outputCost     / total * 100, color: '#a78bfa' },
      { pct: s.cacheWriteCost / total * 100, color: '#06b6d4' },
      { pct: s.cacheReadCost  / total * 100, color: '#10b981' },
    ]

    const sessionRow = document.createElement('tr')
    sessionRow.className = 'session-row' + (HIGHLIGHT_SESSION && s.id === HIGHLIGHT_SESSION ? ' highlighted' : '')
    sessionRow.id = 'row-' + i
    sessionRow.onclick = () => toggleRow(i)
    sessionRow.innerHTML = `
      <td id="arrow-${i}" style="color:var(--muted);font-size:13px">▶</td>
      <td><span class="truncate" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</span></td>
      <td><span class="tag tag-clickable">${escapeHtml(s.project)}</span></td>
      <td style="color:var(--muted);font-variant-numeric:tabular-nums">${s.date}</td>
      <td>${s.models.map(m => `<span class="tag">${m}</span>`).join(' ')}</td>
      <td style="text-align:right;color:var(--muted)">${s.messages}</td>
      <td class="cost-pill" style="text-align:right"><span style="display:flex;align-items:center;justify-content:flex-end;gap:6px">${s.costRankPct !== null && s.costRankPct <= 10 ? `<span style="color:var(--amber);font-weight:700;font-size:11px" title="Top ${Math.max(1, Math.round(s.costRankPct))}% priciest session, worth opening to see why">${'🔥'.repeat(Math.max(1, Math.min(5, 6 - Math.ceil(s.costRankPct / 2))))}</span>` : ''}${fmt(s.cost)}</span></td>
    `
    // Bound via closure, not interpolated into the onclick markup above, because s.project is
    // user-controlled (a directory name), so building it into an HTML/JS string would let a
    // crafted project name break out of the attribute or the string literal.
    sessionRow.querySelector('.tag-clickable').onclick = (ev) => { ev.stopPropagation(); setProjectFilter(s.project) }

    // --- tool usage section --- a text percentage instead of a bar: a bar needs a container
    // width it can be trusted to fit inside, and that kept overflowing this narrow, variable-width
    // card regardless of how it was constrained. A <table> with fixed/percentage column widths
    // turned out to have the same problem one level up (columns past the first silently lost
    // their width in this flex/grid nesting), a flex <li>, the same pattern already working
    // fine in Errors/Repeated calls right next to this card, doesn't depend on table column
    // width resolution at all.
    const toolRows = Object.entries(s.toolCounts)
      .sort((a, b) => b[1] - a[1])
      .map(([name, n]) => `<li style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:5px 0;border-bottom:1px solid var(--border)">
        <span style="font-family:monospace;color:var(--muted)">${name}</span>
        <span style="flex-shrink:0;color:var(--muted)">${Math.round(n / s.totalTools * 100)}% <span style="color:var(--text);font-weight:600">${n}</span></span>
      </li>`).join('')

    // --- errors: tool + what was attempted + the actual error message. Grouped by exact match
    // (same tool, same descriptor, same message) instead of one row per occurrence, so the same
    // command failing the same way several times reads as one row with a count instead of noise.
    // e.category (from _classify_error in report.py) tells prevented-redundant-call and expected
    // no-match outcomes apart from an actual failure, see CATEGORY_LABEL below. ---
    const errorKey = e => `${e.tool}|${e.descriptor || ''}|${e.message}`
    const errorGroupsMap = new Map()
    for (const e of (s.toolErrors || [])) {
      const k = errorKey(e)
      if (!errorGroupsMap.has(k)) errorGroupsMap.set(k, { ...e, count: 0 })
      errorGroupsMap.get(k).count++
    }
    const uniqueErrors = [...errorGroupsMap.values()]
    // Only real failures count toward the "worth reviewing" signal below, a hook preventing a
    // redundant re-read, or a benign no-match exit, repeating 3x isn't the same thing as a
    // command actually failing 3x.
    const failureCounts = uniqueErrors.filter(e => e.category === 'failure').map(e => e.count)
    const maxRepeatedFailure = failureCounts.length ? Math.max(...failureCounts) : 0
    const CATEGORY_LABEL = { prevented: 'prevented redundant call', expected: 'expected outcome' }
    const errorRows = uniqueErrors.map(e => {
      const label = e.descriptor ? `${e.tool}: ${e.descriptor}` : e.tool
      const full = `${label}\\n→ ${e.message}`
      const badge = e.count > 1 ? ` <span style="color:var(--muted)">×${e.count}</span>` : ''
      const catLabel = CATEGORY_LABEL[e.category]
      const catBadge = catLabel ? ` <span class="tag" style="font-size:9px">${catLabel}</span>` : ''
      return `<li style="display:block;padding:5px 0;border-bottom:1px solid var(--border)">
        <div style="min-width:0;font-family:monospace;font-size:11px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(full)}">${escapeHtml(label)}${badge}${catBadge}</div>
        <div style="min-width:0;color:var(--muted);font-size:10px;margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(full)}">${escapeHtml(e.message)}</div>
      </li>`
    }).join('')

    // --- permission blocks: tool calls the user declined, not the tool failing ---
    const permissionRows = (s.permissionBlocks || []).map(p => `<li style="display:block;padding:5px 0;border-bottom:1px solid var(--border)">
        <span style="min-width:0;font-family:monospace;font-size:11px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block" title="${escapeHtml(p.descriptor || p.tool)}">${escapeHtml(p.tool)}${p.descriptor ? ': ' + escapeHtml(p.descriptor) : ''}</span>
      </li>`).join('')

    const detailRow = document.createElement('tr')
    detailRow.className = 'detail-row'
    detailRow.id = 'detail-' + i
    const durationStr = s.durationMinutes === null ? '-'
      : s.durationMinutes < 1 ? '<1 min'
      : s.durationMinutes < 60 ? Math.round(s.durationMinutes) + ' min'
      : (s.durationMinutes / 60).toFixed(1) + ' h'

    function sparklineSVG(data) {
      if (!data || data.length < 2) return ''
      const w = 200, h = 36
      const max = Math.max(...data), min = Math.min(...data)
      const range = max - min || 1
      const pts = data.map((v, i) => {
        const x = (i / (data.length - 1)) * w
        const y = h - 4 - ((v - min) / range) * (h - 8)
        return x + ',' + y
      }).join(' ')
      return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" style="display:block"><polyline points="' + pts + '" fill="none" stroke="#6366f1" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    }

    // --- context signal: verdict is based on the PEAK turn, not the last one. A session that
    // spiked to 300K and got compacted down to 20K is still a session that paid for 300K of
    // context, and the final-turn number alone would hide that entirely. The growth-ratio check
    // is also gated on an absolute floor: 2K growing to 22K is 11x but still cheap in real
    // terms, and ratio alone would flag it the same as a session that grew into six figures.
    // The amber observation itself needs more than "it got big": a single compaction doing its
    // job isn't evidence the user should have compacted manually. It's gated on stronger signals:
    // multiple compactions (the pattern kept recurring), or a large share of the session's cost
    // landing after the first compaction (continuing cost more than starting fresh would have).
    // A single compaction that doesn't clear either bar gets a neutral observation, not advice.
    // Phrased as an observation, not an instruction: this describes WHEN the cost accumulated,
    // not proof that compacting earlier or continuing past it caused it, so no "try X earlier"
    // or "avoidable" framing, and amber rather than red. ---
    const MIN_PEAK_FOR_GROWTH_FLAG = 20000
    let contextMsg = null, contextClass = '', currentCtx = 0, peakCtx = 0
    if (s.inputPerMsg.length > 0) {
      currentCtx = s.inputPerMsg[s.inputPerMsg.length - 1]
      peakCtx = Math.max(...s.inputPerMsg)
      const firstCtx = s.inputPerMsg[0]
      const growthRatio = s.inputPerMsg.length > 1 ? peakCtx / Math.max(firstCtx, 1) : null
      const growthFlags = growthRatio !== null && peakCtx >= MIN_PEAK_FOR_GROWTH_FLAG
      const peakRatioToFloor = peakCtx / s.contextWindow
      const afterFirstSharePct = total > 0 ? Math.round(s.costAfterFirstCompaction / total * 100) : 0
      const strongCompactSignal = s.compactions.length >= 2 || (s.compactions.length >= 1 && afterFirstSharePct >= 30)
      if (strongCompactSignal) {
        contextMsg = s.compactions.length >= 2
          ? `${s.compactions.length} automatic compactions, ${fmt(s.costAfterFirstCompaction)} (${afterFirstSharePct}%) accumulated after the first. If the thread crossed distinct milestones, consider starting a fresh session between them.`
          : `${afterFirstSharePct}% of this session's cost came after its first compaction. If the thread crossed a distinct milestone, consider starting a fresh session there next time.`
        contextClass = 'hit-rate-mid'
      } else if (s.compactions.length >= 1) {
        contextMsg = `Context reached ${fmtK(peakCtx)} before an automatic compaction.`
      } else if ((growthFlags && growthRatio >= 3) || peakRatioToFloor >= 0.5) {
        contextMsg = 'Grew a lot. /compact if a session like this happens again.'
        contextClass = 'hit-rate-mid'
      }
      var contextTip = `Peak context: ${fmtK(peakCtx)} tokens, the single largest turn, not a running total (unlike cache read/write below, which sum across every turn). (${s.contextWindowSource === 'observed' ? `${peakRatioToFloor.toFixed(1)}× the lowest auto-compact trigger observed for ${s.modelShort}` : `${Math.round(peakRatioToFloor * 100)}% of ${s.modelShort}'s estimated window`}). Final turn: ${fmtK(currentCtx)} tokens${growthRatio !== null ? ` · peak was ${growthRatio.toFixed(1)}× the first turn (${fmtK(firstCtx)} → ${fmtK(peakCtx)})` : ''}${peakCtx !== currentCtx ? ' · dropped after a compaction' : ''}`
    }

    // --- causal synthesis, kept as two separate lines on purpose: WHY the session cost what it
    // did (which component dominated, reusing the same cost breakdown as the KPI cards above but
    // scoped to this session) versus WHAT'S WORTH REVIEWING (signals suggesting avoidable work).
    // Mixing the two made it read like "expensive == wasteful", which isn't always true.
    // "Building and carrying context" rather than "carrying context forward" for the combined
    // bucket: cache read is reusing context you already built, cache write is building it in
    // the first place, and folding both under "carrying forward" mislabels the write half.
    const shares = [
      { label: 'building and carrying context (cache read + write)', cost: s.cacheReadCost + s.cacheWriteCost },
      { label: 'output', cost: s.outputCost },
      { label: 'fresh input', cost: s.inputCost },
    ].sort((a, b) => b.cost - a.cost)
    const whyParts = []
    if (total > 0 && shares[0].cost > 0) whyParts.push(`${Math.round(shares[0].cost / total * 100)}% from ${shares[0].label}`)
    if (s.inputPerMsg.length > 1) whyParts.push(`peak context ${fmtK(peakCtx)}`)
    if (s.compactions.length > 0) {
      let compactionPart = `${s.compactions.length} compaction${s.compactions.length > 1 ? 's' : ''}`
      // How much kept accumulating after the session first hit the auto-compact floor and the
      // session carried on instead of starting fresh. The count alone doesn't say whether that
      // continuation was cheap or most of the bill.
      if (total > 0 && s.costAfterFirstCompaction > 0.02) {
        const afterPct = Math.round(s.costAfterFirstCompaction / total * 100)
        if (afterPct >= 5) compactionPart += ` · ${fmt(s.costAfterFirstCompaction)} after first (${afterPct}%)`
      }
      whyParts.push(compactionPart)
    }

    const reviewItems = []
    if (maxRepeatedFailure >= 3) reviewItems.push(`the same error signature appeared ${maxRepeatedFailure} times`)

    detailRow.innerHTML = `<td colspan="7">
      <div class="detail-inner">
        <div style="margin-bottom:14px;padding:10px 14px;background:var(--surface2);border-radius:8px;font-size:13px">
          <div><strong>Why this cost ${fmt(s.cost)}:</strong> ${whyParts.join(' · ') || 'too small to break down'}</div>
          ${reviewItems.length > 0 ? `<div style="margin-top:6px">🔥 <strong>Worth reviewing:</strong> ${reviewItems.join(' · ')}</div>` : ''}
        </div>
        <div class="session-meta-row">
          ${s.entrypoint ? `<div class="session-meta-item">
            <span class="session-meta-label">Entrypoint</span>
            <span class="session-meta-value" style="font-size:12px">${s.entrypoint}</span>
          </div>` : ''}
          <div class="session-meta-item">
            <span class="session-meta-label">Session ID</span>
            <span class="session-meta-value session-meta-copy" title="Click to copy">
              <span class="session-meta-copy-text">${escapeHtml(s.id)}</span>
              <span class="session-meta-copy-icon">⧉</span>
            </span>
          </div>
          ${s.projectPath ? `<div class="session-meta-item">
            <span class="session-meta-label">Project path</span>
            <span class="session-meta-value session-meta-copy" title="${escapeHtml(s.projectPath)} (click to copy)">
              <span class="session-meta-copy-text">${escapeHtml(s.projectPath)}</span>
              <span class="session-meta-copy-icon">⧉</span>
            </span>
          </div>` : ''}
          ${s.inputPerMsg.length > 1 ? `<div class="session-meta-item">
            <span class="session-meta-label">Context <span class="tip" tabindex="0" data-tip="${contextTip}">?</span></span>
            ${sparklineSVG(s.inputPerMsg)}
            <span style="font-size:11px;color:var(--muted)">peak ${fmtK(peakCtx)}${s.compactions.length > 0 ? ` · ${s.compactions.length} auto-compaction${s.compactions.length > 1 ? 's' : ''}` : ''}${peakCtx !== currentCtx ? ` · final ${fmtK(currentCtx)}` : ''}</span>
            ${contextMsg ? `<div class="${contextClass}" style="font-size:13px;font-weight:500;margin-top:2px">${contextMsg}</div>` : ''}
          </div>` : ''}
          ${s.peerCount >= 5 && s.comparisonQuality !== 'low' && s.cost >= 5 ? `<div class="session-meta-item" style="max-width:320px">
            <span class="session-meta-label">Peer comparison <span class="tip" tabindex="0" data-tip="Compares this session's cost to sessions matching the same project, model, and pricing regime, closest by turn count. Only shown when there are enough of them and they're close enough in length to trust the comparison.">?</span></span>
            <span style="font-size:12px;color:var(--text);line-height:1.4">This session cost ${fmt(s.cost)}, ${s.costVsPeerMedian.toFixed(1)}× the ${fmt(s.peerMedianCost)} median of ${s.peerCount} comparable sessions.</span>
            <span style="font-size:10px;color:var(--muted)">${s.peerCohortDescription} · ${s.comparisonQuality} confidence</span>
          </div>` : ''}
        </div>
        <details style="margin:-6px 0 14px">
          <summary style="cursor:pointer;font-size:11px;color:var(--muted)">Technical details</summary>
          <div style="font-size:11px;color:var(--muted);margin-top:4px">
            ${[
              `Duration ${durationStr} <span class="tip" tabindex="0" data-tip="First to last message timestamp, includes any idle time the session sat open, not just active work">?</span>`,
              s.cacheHitRate !== null ? `Cache reuse ${Math.round(s.cacheHitRate * 100)}% <span class="tip" tabindex="0" data-tip="Share of cacheable tokens served from cache instead of resent as fresh input. Mostly a byproduct of session length and how much context is being carried forward, not a score to maximize.">?</span>` : '',
              s.cacheROI !== null ? `Cache ROI ${s.cacheROI.toFixed(1)}× <span class="tip" tabindex="0" data-tip="Money saved by cache reads, divided by money spent writing cache. Mostly driven by how long the session ran, not something you directly control, technical detail, not a signal to act on.">?</span>` : '',
            ].filter(Boolean).join(' · ')}
          </div>
        </details>
        <div class="breakdown-grid">
          ${[
            { label: 'Fresh input', tokens: s.input,      cost: s.inputCost,      color: '#6366f1', tip: 'Fresh, non-cached tokens sent this call, not just what you typed. Includes any tool results (file reads, bash output, etc.) that fell outside the cached prefix. Summed across every turn this session, not a single-turn snapshot.' },
            { label: 'Output',      tokens: s.output,      cost: s.outputCost,     color: '#a78bfa' },
            { label: 'Cache write', tokens: s.cacheWrite,  cost: s.cacheWriteCost, color: '#06b6d4', tip: 'Context cached for the first time this session, often file reads or tool results just added to the conversation. Costs more than input once, then pays off as cache read. Summed across every turn this session, not a single-turn snapshot.' },
            { label: 'Cache read',  tokens: s.cacheRead,   cost: s.cacheReadCost,  color: '#10b981', tip: 'Context served from cache instead of reprocessed, e.g. a previously-read file reused on later turns. 10× cheaper than input. Summed across every turn this session: this is why it can dwarf the peak context number below, which is a single-turn snapshot, not a running total.' },
          ].map(b => `
            <div class="breakdown-item">
              <div class="breakdown-label">${b.label}${b.tip ? ` <span class="tip" tabindex="0" data-tip="${b.tip}">?</span>` : ''}</div>
              <div class="breakdown-tokens">${fmtK(b.tokens)}</div>
              <div class="breakdown-cost">${fmt(b.cost)}</div>
              <div class="breakdown-bar" style="background:${b.color};width:${Math.max(b.cost/total*100,2)}%"></div>
            </div>
          `).join('')}
        </div>

        <div class="detail-sections">
          <div class="detail-section">
            <h4>Tool usage · ${s.totalTools} calls</h4>
            ${s.totalTools > 0
              ? `<ul class="file-list" style="max-height:220px">${toolRows}</ul>`
              : '<span class="empty-note">No tool calls recorded</span>'}
          </div>
          ${s.toolErrors.length > 0 ? `<div class="detail-section">
            <h4>Errors · ${s.toolErrors.length}</h4>
            <ul class="file-list" style="max-height:220px">${errorRows}</ul>
          </div>` : ''}
          ${s.permissionBlocks.length > 0 ? `<div class="detail-section">
            <h4>Permission blocks · ${s.permissionBlocks.length}</h4>
            <ul class="file-list" style="max-height:220px">${permissionRows}</ul>
          </div>` : ''}
        </div>

      </div>
    </td>`

    // Bound via closure, not interpolated into the markup above, same reasoning as
    // s.project's handler: s.id/s.projectPath shouldn't be trusted inside an inline onclick.
    detailRow.querySelectorAll('.session-meta-copy').forEach((el, idx) => {
      const value = idx === 0 ? s.id : s.projectPath
      el.onclick = (ev) => { ev.stopPropagation(); copyToClipboard(value, el.querySelector('.session-meta-copy-icon')) }
    })

    tbody.appendChild(sessionRow)
    tbody.appendChild(detailRow)
  })
}

updateSortButtons()
renderTable()

if (HIGHLIGHT_SESSION) {
  const idx = filtered().findIndex(s => s.id === HIGHLIGHT_SESSION)
  if (idx !== -1) {
    toggleRow(idx)
    setTimeout(() => document.getElementById('row-' + idx)?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 100)
  }
}
"""

_STATIC_TAIL = """</script>
</body>
</html>"""

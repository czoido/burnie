import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .pricing import calc_cost_components, PRICING_UPDATED

__all__ = ['load_all_sessions', 'PRICING_UPDATED']


# Bash commands that just move around or look, without redoing any real work. Repeating one
# of these isn't the same signal as re-running an expensive command or re-reading a file.
_TRIVIAL_BASH = re.compile(r'^(cd|pwd|ls|clear)(\s|$)')


# Descriptor of "what was attempted" for a tool_use, so an error can say more than just the tool
# name, and so repeated calls can be matched on what a command actually does.
def _tool_descriptor(name, input_):
    if not input_:
        return None
    if name == 'Bash':
        command = (input_.get('command') or '').strip()
        if not command:
            return None
        if _TRIVIAL_BASH.match(command) and not any(sep in command for sep in ('&&', ';', '\n', '|')):
            return None
        # Match on the full command, normalized to one line, because matching on just the first line
        # (the old behavior) groups every `python3 -c "<script>"` or heredoc under the same
        # descriptor regardless of what the script actually does, making dozens of unrelated
        # one-off scripts look like the same call repeated. Kept full (not truncated further
        # than 500 chars): the UI truncates only the visible label and relies on a hover
        # tooltip to show the rest, so the underlying data shouldn't lose it upfront.
        return re.sub(r'\s+', ' ', command)[:500]
    if name in ('Read', 'Edit', 'Write', 'Glob'):
        return input_.get('file_path') or input_.get('pattern')
    if name == 'Grep':
        return input_.get('pattern')
    if name == 'Agent':
        return input_.get('description')
    return None


# The real error text, not the raw tool_result blob: strip Bash's "Exit code N" prefix,
# then take the last non-empty line, since for stderr that's usually the actual error (e.g. the
# "No such file or directory" line, or a Python exception message at the end of a traceback).
# Not truncated tightly: the UI shows this in full on hover, only clipping the inline label.
def _extract_error_message(content):
    if not isinstance(content, str) or not content.strip():
        return 'no error output'
    stripped = re.sub(r'^Exit code \d+\n?', '', content, count=1)
    lines = [l.strip() for l in stripped.split('\n') if l.strip()]
    # A non-zero exit with no stderr at all is usually a benign signal, not a real failure
    # (grep/test/diff use it to mean "false" or "no match"), so say so instead of showing nothing.
    if not lines:
        return 'non-zero exit, no output (often just grep/test finding nothing, not a real failure)'
    return lines[-1][:2000]


# Character count as a token-volume proxy for what a tool call handed back, because Anthropic doesn't
# itemize input tokens per tool_result, only per assistant message, so this is the closest
# available signal for "how much did this tool call actually return."
def _content_length(content):
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(p.get('text', '')) for p in content if isinstance(p, dict))
    return 0


# Claude Code emits this exact message for ANY tool call the user declines, not just
# ExitPlanMode. It's a user decision, not a system failure, so it shouldn't count as an error
# (a plan revision showing up as a "55% error rate" is actively misleading).
_REJECTION_SNIPPET = "the user doesn't want to proceed with this tool use"


def _is_user_rejection(content):
    return isinstance(content, str) and _REJECTION_SNIPPET in content.lower()


def _parse_timestamp(ts):
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None


def _parse_session_file(file_path):
    events = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    title = None
    cwd = None
    entrypoint = None
    per_model = {}  # { modelName: { input, output, cacheWrite5m, cacheWrite1h, cacheRead } }
    per_model_cost = {}  # { modelName: { input, output, cacheWrite, cacheRead, cacheSavings, total } }: dollar
                          # amounts, accumulated per message using that message's own timestamp, so a session
                          # spanning a price change gets the correct total instead of one reconstructed later
                          # from a single date applied to the whole session.
    first_timestamp = None
    last_timestamp = None
    message_count = 0
    tool_counts = {}       # { toolName: count }
    tool_use_meta = {}     # tool_use id -> { name, descriptor }, to explain errors, not just count them
    tool_errors = []       # [{ tool, descriptor, message }]: the actual failures, for display
    permission_blocks = []  # [{ tool, descriptor }]: tool calls the user declined. Tracked
                             # separately from tool_errors: this is a workflow/authorization
                             # signal ("needs a different approach"), not the tool itself failing.
    tool_output_chars = {}  # { toolName: total chars returned }, success or error alike
    tool_call_signatures = {}  # { toolName: { descriptor: count } }: same call repeated within a session
    tool_call_output_chars = {}  # { toolName: { descriptor: total chars } }: volume behind each
                                  # repeated-call signature, so repeats can be ranked by impact
    bash_commands = []     # first line of each Bash command
    input_per_msg = []     # input_tokens per assistant message (context growth)
    cost_per_msg = []      # cost of each assistant message, same index as input_per_msg, so a
                            # session can tie "context was this big" to "and that's how much it cost"
    msg_timestamps = []    # parallel to cost_per_msg, to bucket cost against compaction events
    msg_ids = []           # parallel to cost_per_msg, so a turn's cost/context can be joined back
                            # to the tools that ran in that same turn (see tool_names_by_msg_id)
    tool_names_by_msg_id = {}  # message.id -> set of tool names used in that assistant turn, so
                                # the raw report can say what ran in the turn *before* a context jump,
                                # without claiming which call caused it
    files_changed = set()  # distinct Edit/Write file paths (a mechanical fact, not a quality signal)
    compactions = []       # [{ trigger, preTokens, model, timestamp, turnIndex }] from compact_boundary events
    last_model = None      # tracked alongside compactions, since the boundary event itself has no model field
    counted_message_ids = set()  # a single API response can appear as multiple JSONL lines
                                  # (one per content block: thinking/text/tool_use), each
                                  # repeating the SAME usage, so count each message.id once
    per_day = {}            # { 'YYYY-MM-DD': cost }, bucketed by each message's own timestamp,
                             # not the session's start day, since a session can run past midnight
                             # and Anthropic's usage dashboard counts tokens on the day spent

    for ev in events:
        ev_type = ev.get('type')
        if ev_type == 'ai-title':
            title = ev.get('aiTitle')

        if ev_type == 'user':
            if not cwd and ev.get('cwd'):
                cwd = ev['cwd']
            if not entrypoint and ev.get('entrypoint'):
                entrypoint = ev['entrypoint']
            content = (ev.get('message') or {}).get('content')
            if isinstance(content, list):
                for part in content:
                    if part.get('type') != 'tool_result':
                        continue
                    meta = tool_use_meta.get(part.get('tool_use_id')) or {'name': 'unknown', 'descriptor': None}
                    content_len = _content_length(part.get('content'))
                    tool_output_chars[meta['name']] = tool_output_chars.get(meta['name'], 0) + content_len
                    # Per-descriptor, not just per-tool: lets a repeated-call signature carry how
                    # much it actually returned, so repeats can be ranked by volume, not just count.
                    if meta['descriptor'] is not None:
                        tool_call_output_chars.setdefault(meta['name'], {})
                        tool_call_output_chars[meta['name']][meta['descriptor']] = (
                            tool_call_output_chars[meta['name']].get(meta['descriptor'], 0) + content_len
                        )
                    if part.get('is_error'):
                        if _is_user_rejection(part.get('content')):
                            permission_blocks.append({'tool': meta['name'], 'descriptor': meta['descriptor']})
                            continue
                        tool_errors.append({
                            'tool': meta['name'],
                            'descriptor': meta['descriptor'],
                            'message': _extract_error_message(part.get('content')),
                        })

        if ev_type == 'assistant' and isinstance((ev.get('message') or {}).get('content'), list):
            msg_id = ev['message'].get('id')
            for part in ev['message']['content']:
                if part.get('type') != 'tool_use':
                    continue
                name = part.get('name')
                tool_counts[name] = tool_counts.get(name, 0) + 1
                descriptor = _tool_descriptor(name, part.get('input'))
                if part.get('id'):
                    tool_use_meta[part['id']] = {'name': name, 'descriptor': descriptor}
                if msg_id:
                    tool_names_by_msg_id.setdefault(msg_id, set()).add(name)
                # Only descriptor-bearing, read-like calls count as "repeated": rereading the same
                # file or rerunning the same command without an intervening change is suspicious,
                # but editing or writing the same file multiple times is normal iterative work and
                # would otherwise flood this signal with false positives.
                if descriptor is not None and name not in ('Edit', 'Write'):
                    tool_call_signatures.setdefault(name, {})
                    tool_call_signatures[name][descriptor] = tool_call_signatures[name].get(descriptor, 0) + 1
                if name in ('Edit', 'Write') and descriptor:
                    files_changed.add(descriptor)
                command = (part.get('input') or {}).get('command')
                if name == 'Bash' and command:
                    bash_commands.append(command.split('\n')[0].strip()[:80])

        if ev_type == 'system' and ev.get('subtype') == 'compact_boundary':
            meta = ev.get('compactMetadata') or {}
            compactions.append({
                'trigger': meta.get('trigger', 'unknown'),
                'preTokens': meta.get('preTokens'),
                'model': last_model,
                'timestamp': ev.get('timestamp'),
                # message_count so far = how many turns were counted before this boundary fired,
                # and it's already sitting in the loop, no extra bookkeeping needed to place it on the curve.
                'turnIndex': message_count,
            })

        message = ev.get('message') or {}
        if ev_type == 'assistant' and message.get('usage') and not (message.get('id') and message['id'] in counted_message_ids):
            if message.get('id'):
                counted_message_ids.add(message['id'])
            u = message['usage']
            m = message.get('model')
            msg_cost = 0.0
            if m and m != '<synthetic>':
                last_model = m
                pm = per_model.setdefault(m, {'input': 0, 'output': 0, 'cacheWrite5m': 0, 'cacheWrite1h': 0, 'cacheRead': 0})
                cache_creation = u.get('cache_creation') or {}
                pm['input'] += u.get('input_tokens') or 0
                pm['output'] += u.get('output_tokens') or 0
                pm['cacheWrite5m'] += cache_creation.get('ephemeral_5m_input_tokens', u.get('cache_creation_input_tokens') or 0)
                pm['cacheWrite1h'] += cache_creation.get('ephemeral_1h_input_tokens', 0)
                pm['cacheRead'] += u.get('cache_read_input_tokens') or 0
                comp = calc_cost_components(u, m, ev.get('timestamp'))
                msg_cost = comp['input'] + comp['output'] + comp['cacheWrite'] + comp['cacheRead']
                pmc = per_model_cost.setdefault(m, {'input': 0.0, 'output': 0.0, 'cacheWrite': 0.0, 'cacheRead': 0.0, 'cacheSavings': 0.0, 'total': 0.0})
                pmc['input'] += comp['input']
                pmc['output'] += comp['output']
                pmc['cacheWrite'] += comp['cacheWrite']
                pmc['cacheRead'] += comp['cacheRead']
                pmc['cacheSavings'] += comp['cacheSavings']
                pmc['total'] += msg_cost
                # Bucketed by this message's own timestamp, not the session's start day, because a
                # session can run past midnight, and Anthropic's usage dashboard counts tokens
                # on the day they were actually sent, not the day the session began.
                if ev.get('timestamp'):
                    day = ev['timestamp'][:10]
                    per_day[day] = per_day.get(day, 0.0) + msg_cost
            # Total context size this turn = fresh input + cached tokens (both create and read)
            input_per_msg.append((u.get('input_tokens') or 0) + (u.get('cache_read_input_tokens') or 0) + (u.get('cache_creation_input_tokens') or 0))
            cost_per_msg.append(msg_cost)
            msg_timestamps.append(ev.get('timestamp'))
            msg_ids.append(message.get('id'))
            message_count += 1
            if ev.get('timestamp'):
                if not first_timestamp:
                    first_timestamp = ev['timestamp']
                last_timestamp = ev['timestamp']

    # Aggregate totals across all models
    usage = {'input': 0, 'output': 0, 'cacheWrite': 0, 'cacheRead': 0}
    for m, u in per_model.items():
        usage['input'] += u['input']
        usage['output'] += u['output']
        usage['cacheWrite'] += u['cacheWrite5m'] + u['cacheWrite1h']
        usage['cacheRead'] += u['cacheRead']
    # sum(cost_per_msg), not a recomputation from aggregated per-model totals, because each message
    # already carries its own correctly-dated cost, so this can't drift from a pricing change
    # mid-session the way reconstructing from a single date would.
    cost = sum(cost_per_msg)

    # Primary model = highest cost contributor
    model = 'unknown'
    if per_model_cost:
        model = max(per_model_cost.items(), key=lambda item: item[1]['total'])[0]

    models = list(per_model.keys())
    total_tools = sum(tool_counts.values())
    repeated_calls = sorted(
        (
            {
                'tool': tool, 'descriptor': descriptor, 'count': count,
                'chars': tool_call_output_chars.get(tool, {}).get(descriptor, 0),
            }
            for tool, descriptors in tool_call_signatures.items()
            for descriptor, count in descriptors.items()
            if count > 1
        ),
        key=lambda r: r['count'],
        reverse=True,
    )[:20]
    cache_hit_rate = (
        usage['cacheRead'] / (usage['cacheRead'] + usage['cacheWrite'])
        if (usage['cacheRead'] + usage['cacheWrite']) > 0 else None
    )
    duration_minutes = None
    if first_timestamp and last_timestamp:
        t0, t1 = _parse_timestamp(first_timestamp), _parse_timestamp(last_timestamp)
        if t0 and t1:
            duration_minutes = (t1 - t0).total_seconds() / 60

    # How much this session cost after it first crossed the auto-compact threshold: a session
    # that keeps going past that point is choosing to keep paying premium-context prices rather
    # than starting fresh, which "N compactions happened" alone doesn't put a dollar figure on.
    cost_after_first_compaction = 0.0
    compaction_timestamps = [c['timestamp'] for c in compactions if c.get('timestamp')]
    if compaction_timestamps:
        first_compact_ts = min(compaction_timestamps)
        cost_after_first_compaction = sum(
            c for c, ts in zip(cost_per_msg, msg_timestamps) if ts and ts > first_compact_ts
        )

    # Per-turn tool names, index-aligned with cost_per_msg/input_per_msg, so a report can say what
    # ran in the turn *before* a context jump without claiming which call caused it.
    tools_per_msg = [sorted(tool_names_by_msg_id.get(mid, [])) for mid in msg_ids]

    return {
        'title': title, 'model': model, 'models': models, 'perModel': per_model, 'perModelCost': per_model_cost, 'cwd': cwd,
        'usage': usage, 'cost': cost, 'perDay': per_day,
        'firstTimestamp': first_timestamp, 'lastTimestamp': last_timestamp,
        'messageCount': message_count, 'filePath': str(file_path),
        'toolCounts': tool_counts, 'totalTools': total_tools,
        'toolErrors': tool_errors, 'permissionBlocks': permission_blocks, 'repeatedCalls': repeated_calls,
        'toolOutputChars': tool_output_chars,
        'bashCommands': bash_commands, 'inputPerMsg': input_per_msg, 'costPerMsg': cost_per_msg,
        'toolsPerMsg': tools_per_msg,
        'compactions': compactions, 'costAfterFirstCompaction': cost_after_first_compaction,
        'cacheHitRate': cache_hit_rate, 'durationMinutes': duration_minutes, 'entrypoint': entrypoint,
        'filesChanged': sorted(files_changed),
    }


# Claude Code encodes project paths by replacing /, -, and . all with -.
# This makes decoding ambiguous: "conan-py-build" and "conan/py/build" produce
# the same encoded string. We resolve the ambiguity by walking the filesystem:
# at each position we try joining 1..5 parts with hyphens and take the first
# candidate that exists on disk.
def _resolve_project_path(dir_name):
    parts = [p for p in dir_name.split('-') if p]

    def search(idx, current_path):
        if idx >= len(parts):
            return current_path
        for j in range(idx + 1, min(idx + 6, len(parts)) + 1):
            segment = '-'.join(parts[idx:j])
            candidate = os.path.join(current_path, segment)
            if os.path.exists(candidate):
                result = search(j, candidate)
                if result:
                    return result
        return None

    return search(0, '/') or dir_name


def load_all_sessions():
    projects_dir = os.path.join(os.path.expanduser('~'), '.claude', 'projects')
    if not os.path.isdir(projects_dir):
        return []

    project_dirs = [
        d for d in os.listdir(projects_dir)
        if os.path.isdir(os.path.join(projects_dir, d))
    ]

    jobs = []
    for dir_name in project_dirs:
        dir_path = os.path.join(projects_dir, dir_name)
        for file in os.listdir(dir_path):
            if file.endswith('.jsonl'):
                jobs.append((dir_name, os.path.join(dir_path, file), file[:-len('.jsonl')]))

    def process(job):
        dir_name, file_path, session_id = job
        try:
            session = _parse_session_file(file_path)
        except Exception:
            return None
        if session['messageCount'] == 0:
            return None
        # cwd from the JSONL is the authoritative project path, otherwise fall back to filesystem resolver
        project_path = session['cwd'] or _resolve_project_path(dir_name)
        parts = [p for p in project_path.split('/') if p]
        project_name = parts[-1] if parts else dir_name
        session['sessionId'] = session_id
        session['projectName'] = project_name
        session['projectPath'] = project_path
        return session

    sessions = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        for result in pool.map(process, jobs):
            if result is not None:
                sessions.append(result)

    sessions.sort(key=lambda s: s.get('firstTimestamp') or '', reverse=True)
    return sessions

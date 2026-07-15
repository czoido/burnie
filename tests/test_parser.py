from burnie.pricing import calc_cost

from .helpers import assistant_event, parse_fixture, tool_result_event, tool_use, usage


def test_parses_title_and_cwd(tmp_path):
    events = [
        {'type': 'ai-title', 'aiTitle': 'Refactor the auth flow'},
        {'type': 'user', 'cwd': '/Users/dev/code/demo-project', 'message': {'content': []}},
        assistant_event('msg-1', usage_=usage(input_tokens=500, output_tokens=100)),
    ]
    session = parse_fixture(tmp_path, events)
    assert session['title'] == 'Refactor the auth flow'
    assert session['cwd'] == '/Users/dev/code/demo-project'
    assert session['messageCount'] == 1


def test_cost_matches_pricing_calculation(tmp_path):
    u = usage(input_tokens=10_000, output_tokens=2_000)
    events = [assistant_event('msg-1', model='claude-sonnet-4-6', usage_=u)]
    session = parse_fixture(tmp_path, events)
    assert session['cost'] == calc_cost(u, 'claude-sonnet-4-6')


def test_duplicate_message_id_only_counted_once(tmp_path):
    u = usage(input_tokens=1000, output_tokens=100)
    events = [
        assistant_event('msg-1', usage_=u),
        assistant_event('msg-1', usage_=u),  # same id, e.g. thinking + text blocks of one response
    ]
    session = parse_fixture(tmp_path, events)
    assert session['messageCount'] == 1


def test_tool_error_recorded_with_descriptor_and_message(tmp_path):
    events = [
        assistant_event('msg-1', tool_uses=[tool_use('tu-1', 'Bash', {'command': 'pytest -q'})], usage_=None),
        tool_result_event('tu-1', 'Exit code 1\nFileNotFoundError: config.yaml not found', is_error=True),
        assistant_event('msg-2', usage_=usage()),
    ]
    session = parse_fixture(tmp_path, events)
    assert len(session['toolErrors']) == 1
    err = session['toolErrors'][0]
    assert err['tool'] == 'Bash'
    assert err['descriptor'] == 'pytest -q'
    assert err['message'] == 'FileNotFoundError: config.yaml not found'


def test_user_rejection_becomes_permission_block_not_error(tmp_path):
    events = [
        assistant_event('msg-1', tool_uses=[tool_use('tu-1', 'Bash', {'command': 'rm -rf build/'})], usage_=None),
        tool_result_event('tu-1', 'The user doesn\'t want to proceed with this tool use.', is_error=True),
        assistant_event('msg-2', usage_=usage()),
    ]
    session = parse_fixture(tmp_path, events)
    assert session['toolErrors'] == []
    assert len(session['permissionBlocks']) == 1
    assert session['permissionBlocks'][0]['tool'] == 'Bash'


def test_repeated_read_calls_tracked_as_repeated_signature(tmp_path):
    events = [
        assistant_event('msg-1', tool_uses=[
            tool_use('tu-1', 'Read', {'file_path': '/repo/src/main.py'}),
        ], usage_=None),
        tool_result_event('tu-1', 'file contents'),
        assistant_event('msg-2', tool_uses=[
            tool_use('tu-2', 'Read', {'file_path': '/repo/src/main.py'}),
        ], usage_=None),
        tool_result_event('tu-2', 'file contents'),
        assistant_event('msg-3', usage_=usage()),
    ]
    session = parse_fixture(tmp_path, events)
    repeated = {(r['tool'], r['descriptor']): r['count'] for r in session['repeatedCalls']}
    assert repeated[('Read', '/repo/src/main.py')] == 2


def test_compaction_recorded(tmp_path):
    events = [
        assistant_event('msg-1', model='claude-sonnet-4-6', usage_=usage()),
        {
            'type': 'system',
            'subtype': 'compact_boundary',
            'timestamp': '2026-07-01T11:00:00Z',
            'compactMetadata': {'trigger': 'auto', 'preTokens': 150_000},
        },
        assistant_event('msg-2', usage_=usage()),
    ]
    session = parse_fixture(tmp_path, events)
    assert len(session['compactions']) == 1
    assert session['compactions'][0]['trigger'] == 'auto'
    assert session['compactions'][0]['preTokens'] == 150_000

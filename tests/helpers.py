import json

from burnie.parser import _parse_session_file


def write_jsonl(path, events):
    path.write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')


def usage(input_tokens=1000, output_tokens=200, cache_read=0, cache_write_5m=0):
    return {
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'cache_read_input_tokens': cache_read,
        'cache_creation_input_tokens': cache_write_5m,
    }


def assistant_event(msg_id, model='claude-sonnet-4-6', timestamp='2026-07-01T10:00:00Z',
                     tool_uses=None, usage_=None):
    content = list(tool_uses or [])
    if usage_ is not None:
        content.append({'type': 'text', 'text': 'ok'})
    return {
        'type': 'assistant',
        'timestamp': timestamp,
        'message': {
            'id': msg_id,
            'model': model,
            'usage': usage_,
            'content': content,
        },
    }


def tool_use(tool_id, name, input_=None):
    return {'type': 'tool_use', 'id': tool_id, 'name': name, 'input': input_ or {}}


def tool_result_event(tool_use_id, content, is_error=False, timestamp='2026-07-01T10:00:01Z'):
    return {
        'type': 'user',
        'timestamp': timestamp,
        'message': {
            'content': [{
                'type': 'tool_result',
                'tool_use_id': tool_use_id,
                'content': content,
                'is_error': is_error,
            }],
        },
    }


def parse_fixture(tmp_path, events, session_id='test-session-id', project_name='demo-project'):
    file_path = tmp_path / f'{session_id}.jsonl'
    write_jsonl(file_path, events)
    session = _parse_session_file(file_path)
    session['sessionId'] = session_id
    session['projectName'] = project_name
    session['projectPath'] = f'/Users/dev/code/{project_name}'
    return session

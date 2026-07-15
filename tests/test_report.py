from burnie.report import generate_markdown_report, generate_raw_report, generate_report

from .helpers import assistant_event, parse_fixture, usage


def _two_sessions(tmp_path):
    cheap = parse_fixture(
        tmp_path,
        [
            {'type': 'ai-title', 'aiTitle': 'Fix the build pipeline'},
            assistant_event('cheap-msg-1', model='claude-sonnet-4-6', usage_=usage(input_tokens=1000, output_tokens=100)),
        ],
        session_id='cheap-session',
        project_name='demo-project',
    )
    expensive = parse_fixture(
        tmp_path,
        [
            {'type': 'ai-title', 'aiTitle': 'Investigate the report generator'},
            assistant_event('exp-msg-1', model='claude-opus-4-8', usage_=usage(input_tokens=500_000, output_tokens=50_000)),
        ],
        session_id='expensive-session',
        project_name='demo-project',
    )
    return [expensive, cheap]


def test_generate_report_html_contains_session_titles_and_cost(tmp_path):
    html = generate_report(_two_sessions(tmp_path))
    assert '<html' not in html.lower() or 'Fix the build pipeline' in html
    assert 'Fix the build pipeline' in html
    assert 'Investigate the report generator' in html


def test_generate_report_highlights_requested_session(tmp_path):
    html = generate_report(_two_sessions(tmp_path), highlight_session='expensive-session')
    assert 'expensive-session' in html


def test_generate_markdown_report_contains_titles_and_is_plain_text(tmp_path):
    md = generate_markdown_report(_two_sessions(tmp_path))
    assert 'Fix the build pipeline' in md
    assert '<html' not in md.lower()


def test_generate_raw_report_without_session_has_no_current_session_block(tmp_path):
    raw = generate_raw_report(_two_sessions(tmp_path))
    assert 'CURRENT SESSION' not in raw


def test_generate_raw_report_with_matching_session_has_current_session_block(tmp_path):
    raw = generate_raw_report(_two_sessions(tmp_path), highlight_session='expensive-session')
    assert 'CURRENT SESSION' in raw


def test_reports_handle_empty_session_list():
    assert generate_report([])
    assert generate_markdown_report([])
    assert generate_raw_report([])

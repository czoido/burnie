import argparse
import base64
import json
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from .parser import _parse_session_file, load_all_sessions, load_mcp_servers
from .report import generate_report, generate_markdown_report, generate_raw_report


def _load_icon():
    try:
        icon_bytes = resources.files('burnie').joinpath('assets', 'burnie-icon.png').read_bytes()
    except (FileNotFoundError, ModuleNotFoundError):
        return None
    return f'data:image/png;base64,{base64.b64encode(icon_bytes).decode()}'


def _repo_skills_dir():
    # __file__ still points at the real checkout under an editable install or a source
    # run. skills/ sits two levels up from src/burnie/cli.py. A wheel install has neither.
    candidate = Path(__file__).resolve().parents[2] / 'skills'
    return candidate if candidate.is_dir() else None


def _link_skill(src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or dest.exists():
        shutil.rmtree(dest) if dest.is_dir() and not dest.is_symlink() else dest.unlink()
    dest.symlink_to(src)
    print(f'{src.name} skill linked at {dest}')


def _install_skill():
    dest_root = Path.home() / '.claude' / 'skills'
    repo_skills_dir = _repo_skills_dir()

    if repo_skills_dir is not None:
        # burnie-update edits src/burnie/pricing.py in place, so it's only useful here,
        # alongside the source tree.
        for name in ('burnie', 'burnie-update'):
            _link_skill(repo_skills_dir / name, dest_root / name)
        return

    # No checkout: install just the packaged skill from the wheel.
    packaged = resources.files('burnie').joinpath('skills', 'burnie', 'SKILL.md')
    if not packaged.is_file():
        print("Couldn't find the bundled skill file. Try reinstalling burnie.")
        return

    dest_file = dest_root / 'burnie' / 'SKILL.md'
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    dest_file.write_text(packaged.read_text(encoding='utf-8'), encoding='utf-8')
    print(f'burnie skill installed at {dest_file}')


# Dollar thresholds for escalating the flame count: no flame below the first one, so it
# reads as a warning that kicks in once a session gets pricey, not a badge on every session.
# Picked against real session costs (median ~$1, p90 ~$14, priciest sessions seen so far ~$140).
_FLAME_THRESHOLDS = (10, 30, 75, 150)


def _flame_count(cost):
    return sum(cost >= t for t in _FLAME_THRESHOLDS)


def _statusline():
    # Invoked by Claude Code itself as the statusLine command, with a JSON payload on stdin
    # that includes transcript_path: the active session's own .jsonl file.
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, TypeError):
        payload = {}
    transcript_path = payload.get('transcript_path')
    try:
        cost = _parse_session_file(transcript_path)['cost']
    except (OSError, TypeError, KeyError):
        cost = 0.0
    flames = '🔥' * _flame_count(cost)
    print(f'💰 ${cost:.2f}{" " + flames if flames else ""}')


def _install_statusline():
    settings_path = Path.home() / '.claude' / 'settings.json'
    settings = {}
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            print(f"{settings_path} isn't valid JSON, leaving it untouched.")
            return
    settings['statusLine'] = {'type': 'command', 'command': 'burnie --statusline', 'padding': 0, 'refreshInterval': 1}
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + '\n', encoding='utf-8')
    print(f'Statusline installed in {settings_path}.')
    print("This only shows up in Claude Code's terminal UI, not the VS Code extension. Restart your terminal session to see it.")


def _open_in_browser(path):
    opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
    try:
        subprocess.run([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _build_parser():
    parser = argparse.ArgumentParser(
        prog='burnie',
        description='Generate a Claude Code session cost report from local session transcripts.',
    )
    parser.add_argument(
        'output_file', nargs='?', default=None,
        help='output file path (default: burnie-report.html, or burnie-report.md with --markdown)',
    )
    parser.add_argument('--session', metavar='ID', help='highlight a specific session in the report')
    parser.add_argument(
        '--install-skill', action='store_true',
        help='install the /burnie skill into ~/.claude/skills (also /burnie-update, in a repo checkout)',
    )
    parser.add_argument(
        '--install-statusline', action='store_true',
        help="configure Claude Code's statusLine (terminal UI only) to show the active session's running cost",
    )
    parser.add_argument(
        '--statusline', action='store_true',
        help=argparse.SUPPRESS,  # invoked by Claude Code itself, not meant to be run by hand
    )

    output_mode = parser.add_mutually_exclusive_group()
    output_mode.add_argument('--markdown', action='store_true', help='write a Markdown report instead of HTML')
    output_mode.add_argument(
        '--raw', action='store_true',
        help='print a condensed, LLM-readable report to stdout (no file written)',
    )

    return parser


def main():
    args = _build_parser().parse_args()

    if args.install_skill:
        _install_skill()
        return

    if args.install_statusline:
        _install_statusline()
        return

    if args.statusline:
        _statusline()
        return

    if args.raw:
        sessions = load_all_sessions()
        print(generate_raw_report(sessions, highlight_session=args.session, mcp_servers=load_mcp_servers(sessions)))
        return

    out_file = args.output_file or ('burnie-report.md' if args.markdown else 'burnie-report.html')
    out_path = Path(out_file).resolve()

    print('Reading Claude Code sessions...')
    sessions = load_all_sessions()
    print(f'Found {len(sessions)} sessions')

    content = (
        generate_markdown_report(sessions) if args.markdown
        else generate_report(sessions, highlight_session=args.session, icon=_load_icon(), mcp_servers=load_mcp_servers(sessions))
    )
    out_path.write_text(content, encoding='utf-8')
    print(f'Report written to: {out_path}')

    if not args.markdown:
        _open_in_browser(out_path)


if __name__ == '__main__':
    main()

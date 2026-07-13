import argparse
import base64
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from .parser import load_all_sessions
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

    if args.raw:
        sessions = load_all_sessions()
        print(generate_raw_report(sessions, highlight_session=args.session))
        return

    out_file = args.output_file or ('burnie-report.md' if args.markdown else 'burnie-report.html')
    out_path = Path(out_file).resolve()

    print('Reading Claude Code sessions...')
    sessions = load_all_sessions()
    print(f'Found {len(sessions)} sessions')

    content = (
        generate_markdown_report(sessions) if args.markdown
        else generate_report(sessions, highlight_session=args.session, icon=_load_icon())
    )
    out_path.write_text(content, encoding='utf-8')
    print(f'Report written to: {out_path}')

    if not args.markdown:
        _open_in_browser(out_path)


if __name__ == '__main__':
    main()

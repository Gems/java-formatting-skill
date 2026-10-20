#!/usr/bin/env python3
"""Git pre-commit hook: format the staged Java files this commit changes."""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from format_changed import (
    FormattingError, formatter_environment, formatter_executable, git_root, runtime_directory,
)


IN_PROGRESS = ['MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD']
SKIP_KEY = 'java-formatting.skip'


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(['git', '-C', str(root), *args], input=data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if result.returncode:
        raise FormattingError(result.stderr.decode(errors='replace').strip())
    return result.stdout


def staged_java_files(root: Path) -> dict[str, str]:
    """Maps each staged, added or modified regular `.java` file to its index mode."""
    names = git(root, 'diff', '--cached', '--name-only', '-z', '--diff-filter=ACMR', '--', '*.java')
    files = [os.fsdecode(name) for name in names.split(b'\0') if name]
    if not files:
        return {}
    modes = {}
    for record in git(root, 'ls-files', '--stage', '-z', '--', *files).split(b'\0'):
        if record:
            meta, name = record.split(b'\t', 1)
            mode = meta.split()[0].decode()
            if mode in {'100644', '100755'}:
                modes[os.fsdecode(name)] = mode
    return modes


def format_files(root: Path, files: list[Path]) -> None:
    """Runs the preformatter and then one IntelliJ formatter process over `files`."""
    skill = Path(__file__).resolve().parent.parent
    python = skill / '.venv/bin/python'
    if not python.is_file():
        raise FormattingError(f'Preformatter environment is missing; run python3 {skill / "scripts/setup_preformatter.py"}')
    runtime = runtime_directory()
    runtime.mkdir(parents=True, exist_ok=True)
    command = [str(formatter_executable())]
    settings = root / '.idea/codeStyles/Project.xml'
    command.extend(['-s', str(settings)] if settings.is_file() else ['-allowDefaults'])
    command.extend(str(path) for path in files)
    log_path = runtime / 'pre-commit.log'
    with (runtime / 'formatter.lock').open('a') as lock, log_path.open('wb') as log:
        fcntl.flock(lock, fcntl.LOCK_EX)
        steps = [
            ([str(python), '-B', str(skill / 'scripts/preformat_java.py'), *map(str, files)], None, 30),
            (command, formatter_environment(runtime), 120),
        ]
        for step, env, timeout in steps:
            try:
                result = subprocess.run(step, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
            except subprocess.TimeoutExpired as error:
                raise FormattingError(f'{Path(step[0]).name} exceeded {timeout} seconds; see {log_path}') from error
            if result.returncode:
                raise FormattingError(f'{Path(step[0]).name} exited {result.returncode}; see {log_path}')


def format_staged(root: Path) -> int:
    """
    Formats the staged content of the Java files this commit adds or modifies. Fully staged files are updated in
    the working tree too; partially staged ones only in the index, so their unstaged edits stay unstaged.
    """
    if any(subprocess.run(['git', '-C', str(root), 'rev-parse', '-q', '--verify', ref],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0 for ref in IN_PROGRESS):
        return 0  # Merged or picked changes are someone else's code.
    modes = staged_java_files(root)
    if not modes:
        return 0
    with tempfile.TemporaryDirectory(prefix='java-formatting-staged-') as directory:
        # Mirror the staged files with the staged EditorConfig files that govern them.
        prefix = directory + os.sep
        configs = [os.fsdecode(name) for name in git(root, 'ls-files', '-z', '--', '.editorconfig', '*/.editorconfig').split(b'\0') if name]
        git(root, 'checkout-index', f'--prefix={prefix}', '--', *modes, *configs)
        originals = {name: (Path(directory) / name).read_bytes() for name in modes}
        format_files(root, [Path(directory) / name for name in modes])
        count = 0
        for name, mode in modes.items():
            content = (Path(directory) / name).read_bytes()
            if content == originals[name]:
                continue
            blob = git(root, 'hash-object', '-w', '--stdin', '--no-filters', data=content).decode().strip()
            git(root, 'update-index', '--cacheinfo', f'{mode},{blob},{name}')
            working = root / name
            if not working.is_symlink() and working.is_file() and working.read_bytes() == originals[name]:
                working.write_bytes(content)
            else:
                print(f'java-formatting: {name} is partially staged; formatted the staged version only.', file=sys.stderr)
            count += 1
        return count


def skip_requested(cwd: Path) -> bool:
    """Tells whether the commit asked to skip formatting: `git -c java-formatting.skip=true commit` or the env var."""
    if os.environ.get('JAVA_FORMATTING_SKIP'):
        return True
    result = subprocess.run(['git', '-C', str(cwd), 'config', '--type=bool', '--get', SKIP_KEY],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return result.stdout.strip() == 'true'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, help='Repository or subdirectory (defaults to cwd)')
    args = parser.parse_args()
    cwd = args.repo or Path.cwd()
    if skip_requested(cwd):
        return 0
    try:
        root = git_root(cwd)
        count = format_staged(root) if root is not None else 0
        if count:
            print(f'java-formatting: formatted {count} staged Java file(s).', file=sys.stderr)
        return 0
    except (OSError, ValueError, FormattingError, subprocess.SubprocessError) as error:
        print(f'java-formatting: {error}\n'
            f'Commit aborted. To commit without formatting: git -c {SKIP_KEY}=true commit ...', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""Batch-format Git-changed Java files, automatically only for Codex Stop."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


CACHE_PATH = Path('.idea/tracking-changes.hashes')
CONFLICTS = {b'DD', b'AU', b'UD', b'UA', b'DU', b'AA', b'UU'}


class FormattingError(Exception):
    pass


def git_root(cwd: Path) -> Path | None:
    result = subprocess.run(
        ['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
    )
    if result.returncode:
        return None
    return Path(os.fsdecode(result.stdout.rstrip(b'\n')))


def changed_java_files(root: Path) -> list[str]:
    result = subprocess.run(
        ['git', '-C', str(root), 'status', '--porcelain=v1', '-z',
         '--untracked-files=all', '--', '*.java'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    if result.returncode:
        raise FormattingError(result.stderr.decode(errors='replace').strip())
    records = iter(result.stdout.split(b'\0'))
    files = set()
    for record in records:
        if not record:
            continue
        status, raw_path = record[:2], record[3:]
        if b'R' in status or b'C' in status:
            next(records)  # Porcelain -z puts the destination before its source.
        relative = os.fsdecode(raw_path)
        path = root / relative
        if not relative.endswith('.java'):
            continue
        if status in CONFLICTS:
            raise FormattingError(f'Unresolved Java conflict: {relative}')
        if not path.is_symlink() and path.is_file():
            files.add(relative)
    return sorted(files)


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def read_hashes(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        or len(value) != 64 or any(c not in '0123456789abcdef' for c in value)
        for key, value in data.items()
    ):
        raise FormattingError(f'Invalid hash cache: {path}')
    return data


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=path.parent,
            prefix=f'.{path.name}.', delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def runtime_directory() -> Path:
    override = os.environ.get('JAVA_FORMATTING_CACHE_DIR')
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Caches/java-formatting'
    return Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'java-formatting'


def formatter_executable() -> Path:
    override = os.environ.get('JAVA_FORMATTING_FORMATTER')
    if override:
        executable = shutil.which(os.path.expanduser(override))
        if executable:
            return Path(executable)
        raise FormattingError(f'Formatter executable not found: {override}')
    candidates = []
    if os.environ.get('IDEA_HOME'):
        home = Path(os.environ['IDEA_HOME']).expanduser()
        candidates.extend([home / 'bin/format.sh', home / 'Contents/bin/format.sh'])
    candidates.extend([
        Path.home() / 'Applications/IntelliJ IDEA.app/Contents/bin/format.sh',
        Path('/Applications/IntelliJ IDEA.app/Contents/bin/format.sh'),
        Path('/Applications/IntelliJ IDEA CE.app/Contents/bin/format.sh'),
    ])
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise FormattingError('IntelliJ formatter not found; set JAVA_FORMATTING_FORMATTER to format.sh')


def formatter_environment(runtime: Path) -> dict[str, str]:
    # Keep all headless IDEA state separate from the interactive IDE.
    profile = runtime / 'idea'
    profile.mkdir(parents=True, exist_ok=True)
    properties = profile / 'formatter.properties'
    text = ''.join(
        f'idea.{name}.path={(profile / name).as_posix()}\n'
        for name in ['config', 'system', 'plugins', 'log']
    )
    if not properties.exists() or properties.read_text(encoding='utf-8') != text:
        properties.write_text(text, encoding='utf-8')
    return dict(os.environ, IDEA_PROPERTIES=str(properties))


def pipeline_fingerprint(root: Path, files: list[str]) -> str:
    skill = Path(__file__).resolve().parent.parent
    paths = {skill / 'requirements.txt', skill / 'scripts/preformat_java.py', Path(__file__).resolve()}
    settings = root / '.idea/codeStyles/Project.xml'
    if settings.exists():
        paths.add(settings)
    # Include inherited and nested EditorConfig files without hashing every source.
    directories = set(root.parents) | {root}
    for name in files:
        directory = (root / name).parent
        while directory.is_relative_to(root):
            directories.add(directory)
            if directory == root:
                break
            directory = directory.parent
    paths.update(directory / '.editorconfig' for directory in directories
        if (directory / '.editorconfig').exists())
    value = hashlib.sha256()
    for path in sorted(paths):
        value.update(os.fsencode(path) + b'\0' + path.read_bytes() + b'\0')
    return value.hexdigest()


def preformat_command(root: Path, files: list[str]) -> list[str]:
    skill = Path(__file__).resolve().parent.parent
    python = skill / '.venv/bin/python'
    if not python.is_file():
        raise FormattingError(
            f'Preformatter environment is missing; run python3 {skill / "scripts/setup_preformatter.py"}')
    return [str(python), '-B', str(skill / 'scripts/preformat_java.py'),
        *(str(root / name) for name in files)]


def format_repository(root: Path) -> int:
    # A clean repository must not create state or start the formatter.
    if not changed_java_files(root):
        return 0
    runtime = runtime_directory()
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / 'formatter.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Refresh after waiting: another hook may have formatted these files.
        files = changed_java_files(root)
        cache_path = root / CACHE_PATH
        cache = read_hashes(cache_path)
        state_path = runtime / 'repositories' / (hashlib.sha256(os.fsencode(root)).hexdigest() + '.json')
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        fingerprint = pipeline_fingerprint(root, files)
        pipeline_changed = state.get('pipeline') != fingerprint
        before = {name: digest(root / name) for name in files}
        pending = [name for name, value in before.items() if pipeline_changed or cache.get(name) != value]
        if not pending:
            return 0
        command = [str(formatter_executable())]
        settings = root / '.idea/codeStyles/Project.xml'
        command.extend(['-s', str(settings)] if settings.is_file() else ['-allowDefaults'])
        command.extend(str(root / name) for name in pending)
        log_path = runtime / 'formatter.log'
        with log_path.open('wb') as log:
            try:
                prepared = subprocess.run(preformat_command(root, pending), cwd=root,
                    stdout=log, stderr=subprocess.STDOUT, timeout=30)
                if prepared.returncode:
                    raise FormattingError(f'Preformatter exited {prepared.returncode}; see {log_path}')
                result = subprocess.run(
                    command, cwd=root, env=formatter_environment(runtime),
                    stdout=log, stderr=subprocess.STDOUT, timeout=120,
                )
            except subprocess.TimeoutExpired as error:
                stage = 'Preformatter exceeded 30 seconds' if 'preformat_java.py' in str(error.cmd) else 'Formatter exceeded 120 seconds'
                raise FormattingError(f'{stage}; see {log_path}') from error
        if result.returncode:
            raise FormattingError(f'Formatter exited {result.returncode}; see {log_path}')
        # Commit the complete batch only after success. Never cache input hashes.
        cache.update({name: digest(root / name) for name in pending})
        write_json(cache_path, cache)
        write_json(state_path, {'pipeline': fingerprint})
        return len(pending)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hook', action='store_true', help='Read a Codex Stop event from stdin')
    parser.add_argument('--repo', type=Path, help='Repository or subdirectory (defaults to cwd)')
    args = parser.parse_args()
    try:
        cwd = args.repo or Path.cwd()
        if args.hook:
            event = json.load(sys.stdin)
            if event.get('hook_event_name') != 'Stop':
                print('{}')
                return 0
            cwd = Path(event.get('cwd') or cwd)
        root = git_root(cwd)
        count = format_repository(root) if root is not None else 0
        if args.hook:
            print('{}')  # Quiet success; never request another agent turn.
        elif root is None:
            print('Skipped: not a Git working tree.')
        else:
            print(f'Formatted {count} Java file(s).' if count else 'No Java files need formatting.')
        return 0
    except (OSError, ValueError, FormattingError, subprocess.SubprocessError) as error:
        message = f'Java formatting failed: {error}'
        if args.hook:
            print(json.dumps({'systemMessage': message}))
        print(message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

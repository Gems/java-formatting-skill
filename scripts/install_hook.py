#!/usr/bin/env python3
"""Register the turn-end formatter, preserving existing user hooks and trust."""

import argparse
import json
from pathlib import Path
import shlex
import shutil

from format_changed import write_json


def install(hooks_file: Path, script: Path) -> bool:
    data = json.loads(hooks_file.read_text(encoding='utf-8')) if hooks_file.exists() else {}
    groups = data.setdefault('hooks', {}).setdefault('Stop', [])
    command = f'python3 {shlex.quote(str(script.resolve()))} --hook'
    handler = {
        'type': 'command',
        'command': command,
        'timeout': 180,
        'statusMessage': 'Formatting changed Java files',
    }
    for group in groups:
        for existing in group.get('hooks', []):
            if existing.get('command') == command:
                return False
    groups.append({'hooks': [handler]})
    if hooks_file.exists():
        backup = hooks_file.with_name(hooks_file.name + '.java-formatting.bak')
        if not backup.exists():
            shutil.copy2(hooks_file, backup)
    write_json(hooks_file, data)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hooks-file', type=Path, default=Path.home() / '.codex/hooks.json')
    parser.add_argument('--script', type=Path, default=Path(__file__).with_name('format_changed.py'))
    args = parser.parse_args()
    changed = install(args.hooks_file.expanduser(), args.script.expanduser())
    print('Installed Stop hook.' if changed else 'Stop hook already installed.')
    print('Review/trust the new hook in Codex /hooks before it can run.')


if __name__ == '__main__':
    main()

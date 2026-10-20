#!/usr/bin/env python3
"""Install the pinned preformatter dependencies once, outside hook execution."""

from pathlib import Path
import shutil
import subprocess
import sys
import venv


def main():
    if sys.version_info < (3, 10):
        raise SystemExit('The preformatter requires Python 3.10 or later.')
    skill = Path(__file__).resolve().parent.parent
    environment = skill / '.venv'
    python = environment / 'bin/python'
    uv = shutil.which('uv')
    if uv:
        if not python.exists():
            subprocess.run([uv, 'venv', '--python', sys.executable, str(environment)], check=True)
        subprocess.run([uv, 'pip', 'install', '--python', str(python),
            '-r', str(skill / 'requirements.txt')], check=True)
    else:
        if not python.exists():
            venv.EnvBuilder(with_pip=True).create(environment)
        subprocess.run([str(python), '-m', 'pip', 'install',
            '-r', str(skill / 'requirements.txt')], check=True)
    subprocess.run([str(python), '-c',
        'import editorconfig, tree_sitter, tree_sitter_java'], check=True)
    print('Preformatter dependencies installed in', environment)


if __name__ == '__main__':
    main()

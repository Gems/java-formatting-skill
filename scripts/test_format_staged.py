#!/usr/bin/env python3
"""Behavioral checks for the pre-commit runner, using temporary Git repositories and a stand-in formatter."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name('format_staged.py')
ORIGINAL = 'class Sample {\n  int a =   messy;\n  int b =   messy;\n}\n'


class PreCommitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='java-formatting-staged-test-')
        self.directory = Path(self.temporary.name).resolve()
        self.repo = self.directory / 'repo with spaces'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Formatter Test')
        self.git('config', 'user.email', 'formatter@example.invalid')
        self.write('.editorconfig', 'root = true\n\n[*]\nmax_line_length = 160\n')
        self.write('src/Sample.java', ORIGINAL)
        self.git('add', '.')
        self.git('commit', '-qm', 'Initial fixtures', '--no-verify')
        formatter = self.directory / 'fake formatter'
        formatter.write_text(
            '#!' + sys.executable + '\n'
            'import os, pathlib, sys\n'
            'if os.environ.get("FORMAT_TEST_FAIL"):\n'
            '    sys.exit(3)\n'
            'for arg in sys.argv[1:]:\n'
            '    path = pathlib.Path(arg)\n'
            '    if path.suffix == ".java":\n'
            '        path.write_text(path.read_text().replace("=   ", "= "))\n',
            encoding='utf-8',
        )
        formatter.chmod(0o755)
        self.env = dict(
            os.environ,
            JAVA_FORMATTING_FORMATTER=str(formatter),
            JAVA_FORMATTING_CACHE_DIR=str(self.directory / 'runtime'),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True,
            stdout=subprocess.PIPE, text=True).stdout

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def run_hook(self, **env):
        return subprocess.run([sys.executable, str(SCRIPT), '--repo', str(self.repo)],
            env=dict(self.env, **env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def staged(self, name='src/Sample.java'):
        return self.git('show', f':{name}')

    def test_formats_the_whole_of_a_fully_staged_file(self):
        self.write('src/Sample.java', ORIGINAL.replace('a =   messy;', 'a =   messy + 1;'))
        self.git('add', '.')

        result = self.run_hook()

        expected = 'class Sample {\n  int a = messy + 1;\n  int b = messy;\n}\n'
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.staged(), expected)
        self.assertEqual((self.repo / 'src/Sample.java').read_text(), expected)

    def test_formats_only_the_index_of_a_partially_staged_file(self):
        staged = ORIGINAL.replace('a =   messy;', 'a =   messy + 1;')
        self.write('src/Sample.java', staged)
        self.git('add', '.')
        unstaged = staged.replace('b =   messy;', 'b =   messy + 2;')
        self.write('src/Sample.java', unstaged)

        result = self.run_hook()

        self.assertEqual(self.staged(), 'class Sample {\n  int a = messy + 1;\n  int b = messy;\n}\n')
        self.assertEqual((self.repo / 'src/Sample.java').read_text(), unstaged)
        self.assertIn('partially staged', result.stderr)

    def test_formats_the_whole_of_a_new_file(self):
        self.write('src/Added.java', 'class Added {\n  int a =   messy;\n}\n')
        self.git('add', '.')

        self.run_hook()

        self.assertEqual(self.staged('src/Added.java'), 'class Added {\n  int a = messy;\n}\n')

    def test_leaves_a_merge_alone(self):
        self.write('src/Sample.java', ORIGINAL.replace('a =   messy;', 'a =   messy + 1;'))
        self.git('add', '.')
        (self.repo / '.git/MERGE_HEAD').write_text(self.git('rev-parse', 'HEAD'))

        self.run_hook()

        self.assertIn('=   messy + 1', self.staged())

    def test_fails_when_formatting_fails(self):
        self.write('src/Sample.java', ORIGINAL.replace('a =   messy;', 'a =   messy + 1;'))
        self.git('add', '.')

        result = self.run_hook(FORMAT_TEST_FAIL='1')

        self.assertEqual(result.returncode, 1)
        self.assertIn('Commit aborted', result.stderr)
        self.assertIn('=   messy + 1', self.staged())

    def install_hook(self):
        hook = self.repo / '.git/hooks/pre-commit'
        hook.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{SCRIPT}"\n', encoding='utf-8')
        hook.chmod(0o755)
        self.write('src/Sample.java', ORIGINAL.replace('a =   messy;', 'a =   messy + 1;'))
        self.git('add', '.')

    def commit(self, *options, **env):
        return subprocess.run(['git', '-C', str(self.repo), *options, 'commit', '-qm', 'Change a'],
            env=dict(self.env, **env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_commit_through_the_hook_records_the_formatted_file(self):
        self.install_hook()

        self.assertEqual(self.commit().returncode, 0)

        self.assertEqual(self.git('show', 'HEAD:src/Sample.java'), 'class Sample {\n  int a = messy + 1;\n  int b = messy;\n}\n')
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_failed_formatting_aborts_the_commit(self):
        self.install_hook()
        head = self.git('rev-parse', 'HEAD')

        result = self.commit(FORMAT_TEST_FAIL='1')

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)

    def test_skip_option_commits_without_formatting(self):
        self.install_hook()

        result = self.commit('-c', 'java-formatting.skip=true', FORMAT_TEST_FAIL='1')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('=   messy + 1', self.git('show', 'HEAD:src/Sample.java'))


if __name__ == '__main__':
    unittest.main()

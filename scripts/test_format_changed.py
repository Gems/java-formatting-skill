#!/usr/bin/env python3
"""Behavioral checks using temporary Git repositories and a stand-in formatter."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from install_hook import install


SCRIPT = Path(__file__).with_name('format_changed.py')


class FormattingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='java-formatting-test-')
        self.directory = Path(self.temporary.name).resolve()
        self.repo = self.directory / 'repo with spaces'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Formatter Test')
        self.git('config', 'user.email', 'formatter@example.invalid')
        self.write('.gitignore', '.idea/\nignored/\n')
        self.write('Staged.java', 'class Staged {}\n')
        self.write('Unstaged.java', 'class Unstaged {}\n')
        self.write('Deleted.java', 'class Deleted {}\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Initial fixtures')
        self.calls = self.directory / 'calls.jsonl'
        formatter = self.directory / 'fake formatter'
        formatter.write_text(
            '#!' + sys.executable + '\n'
            'import json, os, pathlib, sys, time\n'
            'with open(os.environ["FORMAT_TEST_CALLS"], "a") as log:\n'
            '    log.write(json.dumps(sys.argv[1:]) + "\\n")\n'
            'time.sleep(float(os.environ.get("FORMAT_TEST_DELAY", "0")))\n'
            'if os.environ.get("FORMAT_TEST_FAIL"):\n'
            '    sys.exit(3)\n'
            'for arg in sys.argv[1:]:\n'
            '    path = pathlib.Path(arg)\n'
            '    if path.suffix == ".java":\n'
            '        text = path.read_text()\n'
            '        if not text.endswith("// formatted\\n"):\n'
            '            path.write_text(text.rstrip() + "\\n// formatted\\n")\n',
            encoding='utf-8',
        )
        formatter.chmod(0o755)
        self.env = dict(
            os.environ,
            JAVA_FORMATTING_FORMATTER=str(formatter),
            JAVA_FORMATTING_CACHE_DIR=str(self.directory / 'runtime'),
            FORMAT_TEST_CALLS=str(self.calls),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *arguments):
        return subprocess.check_output([
            'git', '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null',
            '-C', str(self.repo), *arguments,
        ])

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def run_formatter(self, event=None, env=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), '--hook'],
            input=json.dumps(event or {'hook_event_name': 'Stop', 'cwd': str(self.repo)}),
            text=True, capture_output=True, env=env or self.env, timeout=15,
        )

    def invocations(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def cache(self):
        return json.loads((self.repo / '.idea/tracking-changes.hashes').read_text())

    def assert_success(self, result):
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({}, json.loads(result.stdout))

    def test_batches_staged_unstaged_and_untracked_and_preserves_index(self):
        self.write('Staged.java', 'class Staged { int staged; }\n')
        self.git('add', 'Staged.java')
        index = self.git('show', ':Staged.java')
        self.write('Unstaged.java', 'class Unstaged { int unstaged; }\n')
        self.write('nested/Space and\nnewline.java', 'class New {}\n')
        self.write('ignored/Ignore.java', 'class Ignore {}\n')
        self.write('notes.txt', 'not java')
        (self.repo / 'Deleted.java').unlink()
        self.assert_success(self.run_formatter())
        calls = self.invocations()
        self.assertEqual(1, len(calls))
        expected = {'Staged.java', 'Unstaged.java', 'nested/Space and\nnewline.java'}
        actual = {str(Path(arg).relative_to(self.repo)) for arg in calls[0] if arg.endswith('.java')}
        self.assertEqual(expected, actual)
        self.assertEqual(expected, set(self.cache()))
        self.assertEqual(index, self.git('show', ':Staged.java'))
        for name, value in self.cache().items():
            self.assertEqual(hashlib.sha256((self.repo / name).read_bytes()).hexdigest(), value)

    def test_cached_turn_does_not_bootstrap_and_subsequent_edit_reformats_only_that_file(self):
        self.write('First.java', 'class First {}\n')
        self.write('Second.java', 'class Second {}\n')
        self.assert_success(self.run_formatter())
        self.assert_success(self.run_formatter())
        self.assertEqual(1, len(self.invocations()))
        self.write('Second.java', 'class Second { int changed; }\n')
        self.assert_success(self.run_formatter())
        self.assertEqual(2, len(self.invocations()))
        self.assertEqual([str(self.repo / 'Second.java')], self.invocations()[1][1:])

    def test_no_java_changes_create_no_cache_or_runtime(self):
        self.write('notes.txt', 'new notes')
        self.assert_success(self.run_formatter())
        self.assertEqual([], self.invocations())
        self.assertFalse((self.repo / '.idea').exists())
        self.assertFalse((self.directory / 'runtime').exists())

    def test_failed_batch_does_not_advance_cache_and_is_retried(self):
        self.write('First.java', 'class First {}\n')
        self.assert_success(self.run_formatter())
        cache_before = self.cache()
        self.write('First.java', 'class First { int changed; }\n')
        self.write('Second.java', 'class Second {}\n')
        failed = self.run_formatter(env=dict(self.env, FORMAT_TEST_FAIL='1'))
        self.assertEqual(1, failed.returncode)
        self.assertIn('Formatter exited 3', json.loads(failed.stdout)['systemMessage'])
        self.assertEqual(cache_before, self.cache())
        self.assert_success(self.run_formatter())
        self.assertEqual(3, len(self.invocations()))
        self.assertEqual({'First.java', 'Second.java'}, set(self.cache()))

    def test_rename_uses_destination_and_symlinks_are_skipped(self):
        self.git('mv', 'Staged.java', 'Renamed.java')
        (self.repo / 'Link.java').symlink_to(self.repo / 'Unstaged.java')
        self.assert_success(self.run_formatter())
        self.assertEqual([str(self.repo / 'Renamed.java')], self.invocations()[0][1:])
        self.assertEqual({'Renamed.java'}, set(self.cache()))

    def test_uses_project_settings_when_present(self):
        settings = self.write('.idea/codeStyles/Project.xml', '<code_scheme name="Test" />')
        self.write('New.java', 'class New {}\n')
        self.assert_success(self.run_formatter())
        self.assertEqual(['-s', str(settings)], self.invocations()[0][:2])

    def test_other_events_and_non_git_directories_are_no_ops(self):
        self.write('New.java', 'class New {}\n')
        self.assert_success(self.run_formatter({'hook_event_name': 'PostToolUse', 'cwd': str(self.repo)}))
        self.assert_success(self.run_formatter({'hook_event_name': 'Stop', 'cwd': str(self.directory)}))
        self.assertEqual([], self.invocations())

    def test_concurrent_turns_share_one_batch(self):
        self.write('New.java', 'class New {}\n')
        event = json.dumps({'hook_event_name': 'Stop', 'cwd': str(self.repo)})
        processes = [subprocess.Popen(
            [sys.executable, str(SCRIPT), '--hook'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(self.env, FORMAT_TEST_DELAY='0.2'),
        ) for _ in range(2)]
        for process in processes:
            process.stdin.write(event)
            process.stdin.close()
            process.stdin = None
        for process in processes:
            output, error = process.communicate(timeout=15)
            self.assertEqual(0, process.returncode, error)
            self.assertEqual({}, json.loads(output))
        self.assertEqual(1, len(self.invocations()))

    def test_missing_hash_entry_reformats_unchanged_content(self):
        self.write('New.java', 'class New {}\n')
        self.assert_success(self.run_formatter())
        (self.repo / '.idea/tracking-changes.hashes').write_text('{}\n')
        self.assert_success(self.run_formatter())
        self.assertEqual(2, len(self.invocations()))

    def test_editorconfig_change_invalidates_cached_source_hashes(self):
        self.write('New.java', 'class New {}\n')
        self.assert_success(self.run_formatter())
        before = self.cache()
        self.write('.editorconfig', 'root = true\n[*.java]\nmax_line_length = 100\n')
        self.assert_success(self.run_formatter())
        self.assertEqual(2, len(self.invocations()))
        self.assertEqual(before, self.cache())
        self.assert_success(self.run_formatter())
        self.assertEqual(2, len(self.invocations()))

    def test_preformatter_is_applied_before_native_formatter(self):
        path = self.write('New.java', '''class New {
  void method(
      String first,
      String second) {
    call(
        first,
        second);
  }
}
''')
        self.assert_success(self.run_formatter())
        text = path.read_text()
        self.assertIn('  void method(String first, String second) {', text)
        self.assertIn('    call(first, second);', text)
        self.assertEqual(1, len(self.invocations()))


class InstallerTests(unittest.TestCase):
    def test_preserves_existing_hooks_and_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'hooks.json'
            original = {'hooks': {
                'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'guard'}]}],
                'Stop': [{'hooks': [{'type': 'command', 'command': 'existing-stop'}]}],
            }}
            path.write_text(json.dumps(original))
            self.assertTrue(install(path, SCRIPT))
            first = path.read_bytes()
            self.assertFalse(install(path, SCRIPT))
            self.assertEqual(first, path.read_bytes())
            data = json.loads(first)
            self.assertEqual(original['hooks']['PreToolUse'], data['hooks']['PreToolUse'])
            self.assertEqual(original['hooks']['Stop'][0], data['hooks']['Stop'][0])
            self.assertEqual(2, len(data['hooks']['Stop']))
            backup = path.with_name('hooks.json.java-formatting.bak')
            self.assertEqual(original, json.loads(backup.read_text()))


if __name__ == '__main__':
    unittest.main()

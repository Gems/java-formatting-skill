---
name: java-formatting
description: Rewrap Java argument and parameter lists, then format Git-changed files with IntelliJ once at turn end. Use to maintain or diagnose this hook and its per-repository hash cache.
---

# Java formatting

The Codex `Stop` hook runs `scripts/format_changed.py --hook` once at turn end.
Do not invoke the formatter after individual edits or install edit-tool hooks.

The runner discovers staged, unstaged, and untracked `.java` files using Git.
It compares their working-tree SHA-256 hashes with `.idea/tracking-changes.hashes`.
A missing or different hash queues the file. A lightweight Tree-sitter Java
pass first rewraps argument and parameter lists only; all queued files then go to
one IntelliJ formatter process, which handles every other formatting decision. Only a successful batch updates the cache, using the hashes
of the **formatted** files. Unchanged batches do not start IntelliJ.

The cache is a JSON object mapping repository-relative paths to SHA-256 strings.
The runner changes working-tree files and leaves the Git index alone. Deleted
files and symlinks are skipped; unresolved Java conflicts stop formatting.
Existing dirty Java files are eligible even if the agent did not edit them.
Keep `.idea/tracking-changes.hashes` out of version control. Repositories that
already ignore `.idea` need no additional ignore rule.

## Argument layout

`scripts/preformat_java.py` makes targeted changes in method/constructor
signatures and ordinary expressions, assignments, initializers, returns and throws.
It rewrites only the whitespace that an argument or parameter list owns: after `(`,
around commas and before `)`. Every other byte stays identical, so existing line
breaks in method chains, ternaries, `+` concatenations, after `->` or `=` are never
joined, and no new breaks are added there; IntelliJ handles the rest. Each line
(between such fixed breaks) follows these priorities:

1. Keep the line's lists on one line if it fits `max_line_length`.
2. Otherwise, put all arguments of the line's last list together on the following
   line, one continuation indent deeper, if the two lines fit.
3. Otherwise, put each argument on its own line and recursively apply the same
   priorities to nested arguments. Closing parentheses, commas and suffixes count
   against the line width; keep the closing tokens on the last argument's line.

Only the last list on a line may wrap, and only when closing tokens or
argument-free calls such as `.toList()` follow it. A list followed by `.call(x)`,
`?`/`:`, `+` or another operator can only collapse onto one line, so wrapping never
leaves an expression dangling after `)`. A list keeps its own layout (nested lists
may still change) when an argument spans a fixed line break, or when it has two or
more rows of equally many arguments, such as `Map.of` key/value pairs.

Two blank-line rules complete the pass, because IntelliJ's Java blank-line settings
only cover declarations and cannot add or remove blank lines inside bodies:

- When a method or constructor signature fits on one line, blank lines between its
  `{` and the first body token are removed. Wrapped signatures and empty bodies
  keep theirs.
- A control statement (`if`, `for`, `while`, `do`, `switch`, `try`, `synchronized`,
  labeled loops) that follows another statement in the same block gets one blank
  line before it. Own-line comments directly above it stay attached, so the blank
  line goes above them. Statements that open a block, follow a `case` label or
  share a line with the previous statement are left alone.

Read width, indentation, comma spacing and formatter tags through the EditorConfig
library, including inherited and nested `.editorconfig` files. The parser verifies
that tree structure and exact token, literal and comment contents remain unchanged,
and refuses to write if any other whitespace changed.
Control-flow headers (`if`, `switch`, `for`, `while`, etc.) are excluded; ordinary
statements inside their bodies remain eligible. Existing comment-bearing spans,
text blocks, block lambdas, anonymous-class bodies and formatter-off regions are
protected. Files with unsupported/incomplete syntax or non-UTF-8 encoding are left
to IntelliJ. A line that no layout can fit (for example, an indivisible argument)
is left unchanged.

## Setup and maintenance

Run `python3 scripts/install_hook.py` from this skill to register the user-level
`Stop` hook in `~/.codex/hooks.json`. The installer preserves other hooks and is
idempotent. Codex requires a one-time review/trust of the new definition through
`/hooks`; do not edit hook trust hashes or bypass that review. Official event and
trust behavior: <https://learn.chatgpt.com/docs/hooks>.

Run `python3 scripts/setup_preformatter.py` once to install the three pinned
dependencies from `requirements.txt` into this skill's `.venv`. Setup requires
Python 3.10+; hooks never install packages or access the network. The existing
global Codex and Claude Code hook commands do not need to change.

The runner discovers the installed IntelliJ formatter on macOS or through
`IDEA_HOME`. Set `JAVA_FORMATTING_FORMATTER` to an explicit `format.sh` executable
when necessary. It passes `.idea/codeStyles/Project.xml` with `-s` when present,
and otherwise uses `-allowDefaults`; IntelliJ reads applicable `.editorconfig`
files. The bootstrap uses a separate IDEA profile so it can coexist with the
interactive IDE. Runs share a lock to protect that profile and the hash caches.

Runtime state and the latest formatter log live under
`~/Library/Caches/java-formatting` on macOS, or
`${XDG_CACHE_HOME:-~/.cache}/java-formatting` elsewhere. Override this directory
with `JAVA_FORMATTING_CACHE_DIR` for isolated tests. Per-repository pipeline
fingerprints also live there: changing the scripts, dependency pins, EditorConfig
or the project code style invalidates cache hits for currently Git-changed files.
The hook runner uses the standard library; its preformatter runs in the isolated
environment. Python 3.10+ and Git are required.

For an explicit diagnostic run (outside the automatic turn-end workflow):

```sh
python3 scripts/format_changed.py --repo /absolute/path/to/repository
.venv/bin/python -m unittest discover -s scripts -p 'test_*.py'
.venv/bin/python scripts/preformat_java.py --check /absolute/path/Example.java
```

To force a dirty file to be formatted again without changing its content, remove
its cache entry. Never manually replace the hashes with Git blob
IDs: this cache contains SHA-256 hashes of working-tree bytes.

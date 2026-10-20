#!/usr/bin/env python3
"""Rewrap Java argument and parameter lists; leave all other whitespace to IntelliJ."""

from __future__ import annotations

import argparse
from bisect import bisect_left
from dataclasses import dataclass
from functools import cached_property
import hashlib
import json
from pathlib import Path
import re
import sys

import editorconfig
from tree_sitter import Language, Parser
import tree_sitter_java


LANGUAGE = Language(tree_sitter_java.language())
GROUP_TYPES = {'argument_list', 'formal_parameters'}
DECLARATIONS = {'method_declaration', 'constructor_declaration'}
STATEMENTS = {
    'expression_statement', 'local_variable_declaration', 'field_declaration',
    'return_statement', 'throw_statement', 'explicit_constructor_invocation',
}
CONTROL = {
    'if_statement', 'for_statement', 'enhanced_for_statement', 'while_statement',
    'do_statement', 'switch_expression', 'synchronized_statement',
    'try_with_resources_statement', 'catch_clause',
}
COMPOUND_STATEMENTS = {
    'if_statement', 'for_statement', 'enhanced_for_statement', 'while_statement',
    'do_statement', 'switch_expression', 'synchronized_statement', 'try_statement',
    'try_with_resources_statement', 'labeled_statement',
}
STATEMENT_LISTS = {'block', 'constructor_body', 'switch_block_statement_group'}
COMMENTS = {'line_comment', 'block_comment'}
LITERALS = {'string_literal', 'character_literal'}
BLOCKS = {'block', 'constructor_body', 'class_body', 'enum_body', 'switch_block'}
# Closing delimiters and argument-free member access may follow a wrapped list.
CLOSING_TAIL = re.compile(r'(?:\s*(?:[)\]};,{]|\.\s*[\w$]+(?:\s*\(\s*\))?))*\s*')


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


def tokens(node):
    if node.type in LITERALS | COMMENTS or not node.children:
        yield node
    else:
        for child in node.children:
            yield from tokens(child)


def syntax_digest(node, source: bytes) -> bytes:
    """Include tree shape and exact literal/comment bytes, but no trivia."""
    result = hashlib.sha256()

    def add(current):
        result.update(current.type.encode() + b'\0')
        if current.type in LITERALS | COMMENTS or not current.children:
            value = source[current.start_byte:current.end_byte]
            result.update(str(len(value)).encode() + b':' + value)
        else:
            result.update(str(len(current.children)).encode() + b'[')
            for child in current.children:
                add(child)
            result.update(b']')

    add(node)
    return result.digest()


@dataclass(frozen=True)
class Style:
    maximum: int | None = 160
    continuation: int = 4
    tab_width: int = 4
    tabs: bool = False
    comma_before: bool = False
    comma_after: bool = True
    tags_enabled: bool = True
    off_tag: str = '@formatter:off'
    on_tag: str = '@formatter:on'

    @classmethod
    def for_file(cls, path: Path):
        properties = editorconfig.get_properties(str(path.resolve()))
        maximum = properties.get('max_line_length', '160')
        indent = properties.get('indent_size', '2')
        tab_width = int(properties.get('tab_width', indent if indent.isdigit() else '4'))
        size = int(indent) if indent.isdigit() else tab_width
        return cls(
            maximum=None if maximum in {'off', '0'} else int(maximum),
            continuation=int(properties.get('ij_java_continuation_indent_size',
                properties.get('ij_continuation_indent_size', str(2 * size)))),
            tab_width=tab_width,
            tabs=properties.get('indent_style') == 'tab',
            comma_before=properties.get('ij_java_space_before_comma', 'false') == 'true',
            comma_after=properties.get('ij_java_space_after_comma', 'true') == 'true',
            tags_enabled=properties.get('ij_formatter_tags_enabled', 'true') == 'true',
            off_tag=properties.get('ij_formatter_off_tag', '@formatter:off'),
            on_tag=properties.get('ij_formatter_on_tag', '@formatter:on'),
        )

    def width(self, value: str) -> int:
        column = 0
        for character in value:
            if character == '\t':
                column += self.tab_width - column % self.tab_width
            else:
                column += 2 if ord(character) > 0xffff else 1
        return column

    def fits(self, line: str) -> bool:
        return self.maximum is None or self.width(line) <= self.maximum

    def deeper(self, indent: str) -> str:
        column = self.width(indent) + self.continuation
        if self.tabs:
            return '\t' * (column // self.tab_width) + ' ' * (column % self.tab_width)
        return indent + ' ' * self.continuation

    @property
    def comma(self):
        return (' ' if self.comma_before else '') + ','

    @property
    def separator(self):
        return self.comma + (' ' if self.comma_after else '')


@dataclass(frozen=True)
class Arguments:
    values: tuple[Document, ...]
    separator: str
    signature: bool = False

    @cached_property
    def flat(self):
        return '(' + self.separator.join(value.flat for value in self.values) + ')'


@dataclass(frozen=True)
class Document:
    """One line of source: verbatim text around rewrappable lists."""
    parts: tuple[str | Arguments, ...]

    @cached_property
    def flat(self):
        return ''.join(part if isinstance(part, str) else part.flat for part in self.parts)


def layout(document: Document, indent: str, style: Style, suffix='', depth=0):
    """Try one line, two lines, then recursively one line per argument."""
    if depth > 128:
        return None
    line = indent + document.flat + suffix
    if style.fits(line):
        return [line]
    # Only the last list may wrap, and only before closing tokens: wrapping an
    # earlier one would leave a chain, ternary or concatenation after its ')'.
    index = next((index for index in reversed(range(len(document.parts)))
        if isinstance(document.parts[index], Arguments) and document.parts[index].values), None)
    if index is None:
        return None
    part = document.parts[index]
    tail = document.parts[index + 1:]
    if not part.signature and not CLOSING_TAIL.fullmatch(Document(tail).flat):
        return None
    header = indent + Document(document.parts[:index]).flat + '('
    if not style.fits(header):
        return None
    deeper = style.deeper(indent)
    packed = deeper + part.separator.join(arg.flat for arg in part.values)
    packed += ')' + Document(tail).flat + suffix
    if style.fits(packed):
        return [header, packed]
    lines = [header]
    for number, argument in enumerate(part.values):
        if number == len(part.values) - 1:
            value = Document(argument.parts + (')',) + tail)
            rendered = layout(value, deeper, style, suffix, depth + 1)
        else:
            rendered = layout(argument, deeper, style, style.comma, depth + 1)
        if rendered is None:
            return None
        lines.extend(rendered)
    return lines


class Source:
    def __init__(self, source: bytes, style: Style):
        self.bytes = source
        self.style = style
        self.tree = Parser(LANGUAGE).parse(source)
        self.leaves = list(tokens(self.tree.root_node))
        self.starts = [node.start_byte for node in self.leaves]
        self.newline = '\r\n' if b'\r\n' in source else '\n'
        self.lists, self.flexible = self.rewrappable_lists()

    def text(self, node):
        return self.bytes[node.start_byte:node.end_byte].decode('utf-8')

    def inner_leaves(self, node):
        return self.leaves[bisect_left(self.starts, node.start_byte):bisect_left(self.starts, node.end_byte)]

    def rows(self, group):
        """Detect deliberate rows of equally many arguments, such as Map.of pairs."""
        if group.type != 'argument_list':
            return False
        sizes, children = [0], group.children
        for index, child in enumerate(children):
            if child.type == ',':
                if b'\n' in self.bytes[children[index - 1].end_byte:children[index + 1].start_byte]:
                    sizes.append(0)
            elif child.is_named:
                sizes[-1] += 1
        return len(sizes) > 1 and sizes[0] > 1 and len(set(sizes)) == 1

    def rewrappable_lists(self):
        """Return rewrappable lists by start offset and the gaps they own.

        A list owns the gaps between its direct children: after '(', around
        commas and before ')'. A list stays fixed when it keeps deliberate rows,
        contains a comment or multi-line literal, or when an argument contains
        a line break that no rewrappable nested list owns (a wrapped chain,
        ternary, concatenation or lambda arrow). Fixed lists keep their gaps
        byte-identical, while lists nested in them may still be rewrapped.
        """
        lists, flexible = {}, set()
        groups = [node for node in walk(self.tree.root_node) if node.type in GROUP_TYPES]
        for group in reversed(groups):  # Nested lists precede their containers.
            own = {child.end_byte for child in group.children[:-1]}
            inner = self.inner_leaves(group)
            fixed = self.rows(group) or any(
                leaf.type in COMMENTS or leaf.type in LITERALS and b'\n' in self.bytes[leaf.start_byte:leaf.end_byte]
                for leaf in inner
            ) or any(
                left.end_byte not in own and left.end_byte not in flexible
                and b'\n' in self.bytes[left.end_byte:right.start_byte]
                for left, right in zip(inner, inner[1:])
            )
            if not fixed:
                lists[group.start_byte] = group
                flexible |= own
        return lists, flexible

    def document(self, start: int, end: int):
        parts = []
        position = bisect_left(self.starts, start)
        previous_end = None
        while position < len(self.leaves):
            current = self.leaves[position]
            if current.start_byte >= end:
                break
            if previous_end is not None and previous_end < current.start_byte:
                parts.append(self.bytes[previous_end:current.start_byte].decode('utf-8'))
            group = self.lists.get(current.start_byte)
            if group is not None and group.end_byte <= end:
                values = tuple(self.document(arg.start_byte, arg.end_byte)
                    for arg in group.named_children)
                signature = group.type == 'formal_parameters' and group.parent.type in DECLARATIONS
                parts.append(Arguments(values, self.style.separator, signature))
                previous_end = group.end_byte
                position = bisect_left(self.starts, group.end_byte)
            else:
                parts.append(self.text(current))
                previous_end = current.end_byte
                position += 1
        return Document(tuple(parts))

    def segments(self, start: int, end: int):
        """Split a span at every line break that no rewrappable list owns."""
        leaves = self.leaves[bisect_left(self.starts, start):bisect_left(self.starts, end)]
        begin = start
        for left, right in zip(leaves, leaves[1:]):
            if left.end_byte not in self.flexible and b'\n' in self.bytes[left.end_byte:right.start_byte]:
                yield begin, left.end_byte
                begin = right.start_byte
        yield begin, end

    def disabled_ranges(self):
        if not self.style.tags_enabled:
            return []
        ranges, start = [], None
        for node in self.leaves:
            if node.type not in COMMENTS:
                continue
            text = self.text(node)
            if self.style.off_tag and self.style.off_tag in text and start is None:
                start = node.start_byte
            if self.style.on_tag and self.style.on_tag in text and start is not None:
                ranges.append((start, node.end_byte))
                start = None
        if start is not None:
            ranges.append((start, len(self.bytes)))
        return ranges

    def in_control_header(self, node):
        ancestor = node.parent
        while ancestor is not None:
            if ancestor.type in CONTROL:
                bodies = [ancestor.child_by_field_name(name)
                    for name in ['body', 'consequence', 'alternative']]
                if ancestor.type == 'try_with_resources_statement':
                    body = ancestor.child_by_field_name('body')
                    if body is not None and node.start_byte >= body.start_byte:
                        ancestor = ancestor.parent
                        continue  # Catch/finally bodies are not resource headers.
                if not any(body is not None and body.start_byte <= node.start_byte
                    and node.end_byte <= body.end_byte for body in bodies):
                    return True
            ancestor = ancestor.parent
        return False

    def candidates(self):
        disabled = self.disabled_ranges()
        for node in walk(self.tree.root_node):
            if node.type not in DECLARATIONS | STATEMENTS or self.in_control_header(node):
                continue
            start, end = node.start_byte, node.end_byte
            body = node.child_by_field_name('body') if node.type in DECLARATIONS else None
            if body is not None:
                end = body.start_byte + 1  # Include '{', but never the body.
            modifiers = next((child for child in node.children if child.type == 'modifiers'), None)
            if modifiers is not None:
                for child in modifiers.children:
                    if child.type not in {'annotation', 'marker_annotation'}:
                        break
                    start = child.end_byte
                    while start < end and self.bytes[start:start + 1] in b' \t\r\n':
                        start += 1
            line_start = self.bytes.rfind(b'\n', 0, start) + 1
            if self.bytes[line_start:start].strip(b' \t') or start >= end:
                continue
            line_end = self.bytes.find(b'\n', end)
            if line_end < 0:
                line_end = len(self.bytes)
            tail = bisect_left(self.starts, end)
            stop = bisect_left(self.starts, line_end)
            if any(leaf.type in COMMENTS for leaf in self.leaves[tail:stop]):
                continue  # A trailing comment also consumes the line budget.
            if any(start < stop and begin < end for begin, stop in disabled):
                continue
            protected = any(
                start <= child.start_byte and child.end_byte <= end and (
                    child.type in COMMENTS | BLOCKS
                    or child.type in LITERALS and b'\n' in self.bytes[child.start_byte:child.end_byte]
                ) for child in walk(node)
            )
            if not protected:
                yield start, end, body is not None

    def opening_blank_lines(self, offset: int):
        """Return an edit that drops blank lines between a body's '{' and its first token."""
        position = bisect_left(self.starts, offset)
        if position == len(self.leaves) or self.leaves[position].type == '}':
            return None  # Leave blank lines of empty bodies to IntelliJ.
        gap = self.bytes[offset:self.leaves[position].start_byte]
        if gap.count(b'\n') < 2:
            return None
        return offset, self.leaves[position].start_byte, gap[:gap.index(b'\n') + 1] + gap[gap.rindex(b'\n') + 1:]

    def compound_blank_lines(self):
        """Yield edits that set off control statements from a preceding statement by a blank line.

        Own-line comments directly above a statement stay attached to it. Statements
        that open a block or follow a case label, or share a line, are left alone.
        """
        disabled = self.disabled_ranges()
        newline = self.newline.encode()
        for node in walk(self.tree.root_node):
            if node.type not in COMPOUND_STATEMENTS or node.parent is None or node.parent.type not in STATEMENT_LISTS:
                continue
            position = bisect_left(self.starts, node.start_byte)
            while position > 1 and self.leaves[position - 1].type in COMMENTS and b'\n' in self.bytes[
                    self.leaves[position - 2].end_byte:self.leaves[position - 1].start_byte]:
                position -= 1
            if position < 2:
                continue
            previous, anchor = self.leaves[position - 1], self.leaves[position]
            closing = self.leaves[position - 2] if previous.type in COMMENTS else previous
            if closing.type not in {';', '}'}:
                continue
            gap = self.bytes[previous.end_byte:anchor.start_byte]
            if gap.count(b'\n') != 1 or any(begin < anchor.start_byte and previous.end_byte < end for begin, end in disabled):
                continue
            split = gap.index(b'\n') + 1
            yield previous.end_byte, anchor.start_byte, gap[:split] + newline + gap[split:]

    def verify(self, result: bytes, blank_gaps=frozenset()):
        """Refuse output that changes syntax, literals, comments or other whitespace.

        Besides list-owned gaps, only the blank-line gaps after the given offsets may change.
        """
        after = Parser(LANGUAGE).parse(result)
        if after.root_node.has_error or syntax_digest(self.tree.root_node, self.bytes) != syntax_digest(after.root_node, result):
            raise ValueError('Preformatting changed Java syntax, literal or comment content; refusing to write')
        leaves = list(tokens(after.root_node))
        before = [self.bytes[:self.leaves[0].start_byte]] + [self.bytes[left.end_byte:right.start_byte]
            for left, right in zip(self.leaves, self.leaves[1:])] + [self.bytes[self.leaves[-1].end_byte:]]
        now = [result[:leaves[0].start_byte]] + [result[left.end_byte:right.start_byte]
            for left, right in zip(leaves, leaves[1:])] + [result[leaves[-1].end_byte:]]
        owned = [False] + [left.end_byte in self.flexible or left.end_byte in blank_gaps for left in self.leaves[:-1]] + [False]
        if any(old != new and not flexible for old, new, flexible in zip(before, now, owned)):
            raise ValueError('Preformatting changed whitespace outside argument or parameter lists or blank lines; refusing to write')


def preformat(source: bytes, style: Style):
    source.decode('utf-8')  # Refuse to reinterpret a non-UTF-8 file.
    parsed = Source(source, style)
    if parsed.tree.root_node.has_error:
        return source, {'changed_spans': 0, 'unbreakable_spans': 0, 'parse_skipped': True}
    edits, blank_gaps, unbreakable, last_end = [], set(), 0, -1
    for start, end, body in parsed.candidates():
        if start < last_end:
            continue
        last_end = end
        segments = list(parsed.segments(start, end))
        for begin, finish in segments:
            # Each segment starts its own line; text after it stays on that line.
            indent = source[source.rfind(b'\n', 0, begin) + 1:begin].decode('utf-8')
            line_end = source.find(b'\n', finish)
            suffix = source[finish:len(source) if line_end < 0 else line_end].decode('utf-8').rstrip('\r')
            document = parsed.document(begin, finish)
            lines = layout(document, indent, style, suffix)
            if lines is None:
                unbreakable += any(isinstance(part, Arguments) and part.values for part in document.parts)
                continue
            text = parsed.newline.join(lines)
            replacement = text[len(indent):len(text) - len(suffix)].encode('utf-8')
            if source[begin:finish] != replacement:
                edits.append((begin, finish, replacement))
        if body and len(segments) == 1 and lines is not None and len(lines) == 1:
            # A signature that fits on one line needs no blank line to set off its body.
            edit = parsed.opening_blank_lines(end)
            if edit is not None:
                edits.append(edit)
                blank_gaps.add(end)
    for edit in parsed.compound_blank_lines():
        edits.append(edit)
        blank_gaps.add(edit[0])
    result = source
    for begin, finish, replacement in sorted(edits, reverse=True):
        result = result[:begin] + replacement + result[finish:]
    if edits:
        parsed.verify(result, blank_gaps)
    return result, {'changed_spans': len(edits), 'unbreakable_spans': unbreakable, 'parse_skipped': False}


def prepare_files(paths):
    prepared, report = [], []
    for path in paths:
        source = path.read_bytes()
        try:
            result, counts = preformat(source, Style.for_file(path))
        except UnicodeDecodeError:
            result, counts = source, {'changed_spans': 0, 'encoding_skipped': True}
        report.append({'path': str(path), **counts})
        if result != source:
            prepared.append((path, result))
    return prepared, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('files', type=Path, nargs='+')
    args = parser.parse_args()
    try:
        prepared, report = prepare_files(args.files)
        if not args.check:
            # All files have been parsed and verified before any file is changed.
            for path, result in prepared:
                path.write_bytes(result)
        print(json.dumps({'changed_files': len(prepared), 'files': report}))
        return int(args.check and bool(prepared))
    except (OSError, ValueError, editorconfig.EditorConfigError) as error:
        print(f'Java preformatting failed: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

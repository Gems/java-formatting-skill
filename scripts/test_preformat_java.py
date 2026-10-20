import tempfile
import time
from pathlib import Path
import unittest

from preformat_java import preformat, Source, Style


class PreformatTests(unittest.TestCase):
    def format(self, source, maximum=80, **settings):
        data = source.encode()
        result, report = preformat(data, Style(maximum=maximum, **settings))
        again, _ = preformat(result, Style(maximum=maximum, **settings))
        self.assertEqual(result, again, 'Preformatter must be idempotent')
        self.assertFalse(report['parse_skipped'])
        return result.decode(), report

    def test_method_signature_one_line_has_priority(self):
        result, _ = self.format('''class X {
  public String combine(
      String first,
      String second) {
    return first;
  }
}
''')
        self.assertIn('  public String combine(String first, String second) {', result)

    def test_method_signature_two_lines_before_one_argument_per_line(self):
        result, _ = self.format('''class X {
  public static String combine(String alpha, String beta) {
    return alpha;
  }
}
''', maximum=45)
        self.assertIn('  public static String combine(\n      String alpha, String beta) {', result)

    def test_method_signature_one_argument_per_line_when_two_lines_do_not_fit(self):
        result, _ = self.format('''class X {
  void invoke(FirstType first, SecondType second, ThirdType third) {
  }
}
''', maximum=40)
        self.assertIn('  void invoke(\n      FirstType first,\n      SecondType second,\n      ThirdType third) {', result)

    def test_statement_two_lines_before_one_argument_per_line(self):
        result, _ = self.format('''class X {
  void f() {
    veryLongReceiverName.call(first, second, third);
  }
}
''', maximum=40)
        self.assertIn('    veryLongReceiverName.call(\n        first, second, third);', result)

    def test_nested_arguments_recurse_and_include_closing_delimiters_in_budget(self):
        result, _ = self.format('''class X {
  void f() {
    outer(firstArgument, nested(firstNestedArgument, secondNestedArgument), finalArgument);
  }
}
''', maximum=55)
        self.assertIn('    outer(\n        firstArgument,\n        nested(\n            firstNestedArgument, secondNestedArgument),\n        finalArgument);', result)
        self.assertTrue(all(len(line) <= 55 for line in result.splitlines()))

    def test_recursive_split_of_nested_arguments(self):
        result, _ = self.format('''class X {
  void f() {
    outer(firstArgument, nested(firstNestedArgument, secondNestedArgument), finalArgument);
  }
}
''', maximum=42)
        self.assertIn('        nested(\n            firstNestedArgument,\n            secondNestedArgument),', result)
        self.assertTrue(all(len(line) <= 42 for line in result.splitlines()))

    def test_statements_use_137_character_line_and_two_line_161_character_call(self):
        source = '''class X {
  void f() {
      parameters.put(
          "filters", U.map(
              searchOptions.filter(),
              filter -> "%s %s %s".formatted(filter.field(), filter.operator(), escapedValue(filter, context))));
      parameters.put(
          "ordering", U.map(
              searchOptions.ordering(),
              ordering -> "%s %s".formatted(ordering.field(), ordering.direction())));
  }
}
'''
        result, _ = self.format(source, maximum=160)
        self.assertIn('      parameters.put(\n          "filters", U.map(searchOptions.filter(), filter -> "%s %s %s".formatted(filter.field(), filter.operator(), escapedValue(filter, context))));', result)
        ordering = next(line for line in result.splitlines() if '"ordering"' in line)
        self.assertEqual(137, len(ordering))
        self.assertTrue(all(len(line) <= 160 for line in result.splitlines()))

    def test_control_headers_are_preserved_but_body_statements_are_compacted(self):
        source = '''class X {
  void f() {
    if (check(
        first,
        second)) {
      run(
          first,
          second);
    }
    for (
        var x = make(
            first,
            second);
        test(x);
        advance(x)) {
      run(
          x);
    }
  }
}
'''
        result, _ = self.format(source)
        self.assertIn('if (check(\n        first,\n        second))', result)
        self.assertIn('var x = make(\n            first,\n            second)', result)
        self.assertIn('      run(first, second);', result)
        self.assertIn('      run(x);', result)

    def test_annotations_generics_varargs_and_throws_are_preserved(self):
        result, _ = self.format('''class X {
  @Override
  public <T> String f(
      @A(v = {"x,y", "z"}) final java.util.Map<String, T> x,
      String... y) throws Exception {
    return "escaped \\\" quote, (paren)";
  }
}
''', maximum=95)
        self.assertIn('  @Override\n  public <T> String f(', result)
        self.assertIn('@A(v = {"x,y", "z"}) final java.util.Map<String, T> x', result)
        self.assertIn('String... y) throws Exception {', result)
        self.assertIn('"escaped \\\" quote, (paren)"', result)

    def test_resource_headers_are_preserved_and_catch_bodies_are_eligible(self):
        source = '''class X {
  void f() {
    try (var resource = open(
        first,
        second)) {
      run(
          resource);
    } catch (Exception error) {
      report(
          error);
    } finally {
      finish(
          first);
    }
  }
}
'''
        result, _ = self.format(source)
        self.assertIn('open(\n        first,\n        second)', result)
        self.assertIn('      report(error);', result)
        self.assertIn('      finish(first);', result)

    def test_comments_and_text_blocks_are_protected(self):
        source = '''class X {
  void f() {
    call(
        first, // this comment must stay here
        second);
    call(first, /* multiline
        comment */ second);
    var text = """
        keep    these spaces
          and this indentation
        """;
  }
}
'''
        result, _ = self.format(source)
        self.assertEqual(source, result)

    def test_trailing_comments_do_not_get_pushed_past_the_margin(self):
        source = '''class X {
  void f() {
    call(
        first,
        second); // a long explanation that must be preserved
  }
}
'''
        result, _ = self.format(source, maximum=60)
        self.assertEqual(source, result)

    def test_tab_indentation_uses_visual_columns(self):
        result, _ = self.format('class X {\n\tvoid f() {\n\t\tlongReceiverName.call(first, second, third);\n\t}\n}\n',
            maximum=42, tabs=True, tab_width=4)
        self.assertIn('\t\tlongReceiverName.call(\n\t\t\tfirst, second, third);', result)

    def test_formatter_off_regions_are_preserved(self):
        source = '''class X {
  void f() {
    // @formatter:off
    call(
        first,
        second);
    // @formatter:on
    call(
        first,
        second);
  }
}
'''
        result, _ = self.format(source)
        self.assertIn('// @formatter:off\n    call(\n        first,\n        second);', result)
        self.assertIn('// @formatter:on\n    call(first, second);', result)

    def test_unbreakable_literal_is_not_split_or_rewritten(self):
        source = 'class X {\n  void f() {\n    call("a very very very very very very long immutable literal");\n  }\n}\n'
        result, report = self.format(source, maximum=30)
        self.assertEqual(source, result)
        self.assertGreater(report['unbreakable_spans'], 0)

    def test_crlf_and_unicode_literals_remain_unchanged(self):
        source = 'class X {\r\n  void f() {\r\n    call(\r\n        "Привет, 🌍",\r\n        second);\r\n  }\r\n}\r\n'
        result, _ = self.format(source)
        self.assertIn('    call("Привет, 🌍", second);\r\n', result)
        self.assertEqual(result.count('\n'), result.count('\r\n'))
        self.assertEqual(4, Style().width('🌍ab'))

    def test_constructors_and_return_expressions(self):
        result, _ = self.format('''class X {
  X(
      String first,
      String second) {
    this(first);
  }
  Object f() {
    return outer(
        first,
        inner(second, third));
  }
}
''')
        self.assertIn('  X(String first, String second) {', result)
        self.assertIn('    return outer(first, inner(second, third));', result)

    def test_block_lambdas_keep_their_body_layout(self):
        source = '''class X {
  void f() {
    call(first, value -> {
      if (check(
          value)) {
        run(
            value);
      }
    });
  }
}
'''
        result, _ = self.format(source)
        self.assertIn('call(first, value -> {', result)
        self.assertIn('if (check(\n          value))', result)
        self.assertIn('        run(value);', result)

    def test_incomplete_java_is_not_rewritten(self):
        source = b'class X { void f( { broken( x, y; }'
        result, report = preformat(source, Style())
        self.assertEqual(source, result)
        self.assertTrue(report['parse_skipped'])

    def test_long_fluent_chains_have_bounded_layout_work(self):
        calls = ''.join('.then(firstLongArgument, secondLongArgument)' for _ in range(45))
        source = 'class X {\n  void f() {\n    receiver' + calls + ';\n  }\n}\n'
        started = time.monotonic()
        self.format(source, maximum=50)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_editorconfig_inheritance_and_java_overrides(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / '.editorconfig').write_text('root = true\n[*]\nmax_line_length = 160\nindent_size = 2\n[*.java]\nij_continuation_indent_size = 6\n')
            nested = root / 'nested'
            nested.mkdir()
            (nested / '.editorconfig').write_text('[*.java]\nmax_line_length = 80\n')
            style = Style.for_file(nested / 'X.java')
            self.assertEqual(80, style.maximum)
            self.assertEqual(6, style.continuation)

    def test_signature_with_one_parameter_per_line_collapses_when_it_fits(self):
        result, _ = self.format('''class X {
  static void put(
      @NonNull OrderReference orderReference,
      @NonNull SearchOptions searchOptions,
      @NonNull ResultPage response) {
  }
}
''', maximum=160)
        self.assertIn('  static void put(@NonNull OrderReference orderReference, @NonNull SearchOptions searchOptions, '
            '@NonNull ResultPage response) {\n', result)

    def test_one_line_signature_drops_blank_lines_before_body(self):
        result, report = self.format('''class X {
  @Override
  @SneakyThrows
  protected void doFilterInternal(
      @NonNull HttpServletRequest request, @NonNull HttpServletResponse response, @NonNull FilterChain filterChain) {

    ScopedCache.open();
  }

  X(String first) {
    \t
    run(first);
  }
}
''', maximum=160)
        self.assertIn('''  @Override
  @SneakyThrows
  protected void doFilterInternal(@NonNull HttpServletRequest request, @NonNull HttpServletResponse response, @NonNull FilterChain filterChain) {
    ScopedCache.open();
  }

  X(String first) {
    run(first);
  }''', result)
        self.assertEqual(3, report['changed_spans'])
        crlf = 'class X {\r\n  void f(\r\n      String first) {\r\n\r\n    run(first);\r\n  }\r\n}\r\n'
        self.assertEqual('class X {\r\n  void f(String first) {\r\n    run(first);\r\n  }\r\n}\r\n', self.format(crlf)[0])

    def test_control_statement_after_a_statement_gets_a_blank_line(self):
        result, _ = self.format('''class X {
  @Override
  @SneakyThrows
  protected void doFilterInternal(
      @NonNull HttpServletRequest request, @NonNull HttpServletResponse response, @NonNull FilterChain filterChain) {

    ScopedCache.open();
    try {
      filterChain.doFilter(request, response);
    } finally {
      ScopedCache.close();
    }
  }
}
''', maximum=160)
        self.assertEqual('''class X {
  @Override
  @SneakyThrows
  protected void doFilterInternal(@NonNull HttpServletRequest request, @NonNull HttpServletResponse response, @NonNull FilterChain filterChain) {
    ScopedCache.open();

    try {
      filterChain.doFilter(request, response);
    } finally {
      ScopedCache.close();
    }
  }
}
''', result)

    def test_control_statement_blank_lines_respect_block_starts_labels_comments_and_tags(self):
        source = '''class X {
  void f() {
    if (first) {
      run();
    }
    for (var item : items)
      run(item);
    run(); // trailing note
    // explains the loop
    while (check()) {
      if (nested) run();
      else if (other) run();
    }

    do {
      run();
    } while (check());
    switch (value) {
      case 1:
        if (x) run();
        break;
      default:
        run();
        synchronized (this) {
          run();
        }
    }
    run(); if (x) run();
    outer:
    for (;;) {
      break outer;
    }
    // @formatter:off
    run();
    if (x) run();
    // @formatter:on
  }
}
'''
        result, report = self.format(source)
        self.assertEqual(source
            .replace('    }\n    for (var item', '    }\n\n    for (var item')
            .replace('// trailing note\n', '// trailing note\n\n')
            .replace('while (check());\n    switch', 'while (check());\n\n    switch')
            .replace('        run();\n        synchronized', '        run();\n\n        synchronized')
            .replace('run();\n    outer:', 'run();\n\n    outer:'), result)
        self.assertEqual(5, report['changed_spans'])
        crlf = 'class X {\r\n  void f() {\r\n    run();\r\n    if (x) run();\r\n  }\r\n}\r\n'
        self.assertEqual(crlf.replace('run();\r\n    if', 'run();\r\n\r\n    if'), self.format(crlf)[0])

    def test_wrapped_signatures_and_empty_bodies_keep_blank_lines(self):
        source = '''class X {
  void invoke(
      FirstType first,
      SecondType second) {

    run();
  }

  void empty() {

  }
}
'''
        self.assertEqual(source, self.format(source, maximum=40)[0])

    def test_wrapped_method_chains_keep_their_breaks(self):
        source = '''class X {
  Object f() {
    val builder = MockMvcRequestBuilders.request(request.method(), request.uri())
        .contentType(MediaType.APPLICATION_JSON)
        .accept(MediaType.APPLICATION_JSON);
    return stream
        .filter(item -> matches(
            item,
            other))
        .findFirst()
        .map(Result::of)
        .orElseThrow(() -> new NotFoundException("Item not found"));
  }
}
'''
        result, _ = self.format(source, maximum=160)
        self.assertEqual(source.replace('matches(\n            item,\n            other))', 'matches(item, other))'), result)

    def test_wrapped_ternary_keeps_its_breaks(self):
        source = '''class X {
  void f() {
    val itemId = Objects.equals(sourceRequest.method(), HttpMethod.POST)
        ? ResponseTestSupport.readJsonPath(sourceResult, "$.data.id", String.class)
        : sourceRequest.identifier();
  }
}
'''
        self.assertEqual(source, self.format(source, maximum=160)[0])
        result, _ = self.format(source, maximum=80)
        self.assertEqual(source.replace('readJsonPath(sourceResult',
            'readJsonPath(\n            sourceResult'), result)

    def test_wrapped_concatenation_keeps_its_breaks(self):
        source = '''class X {
  void f() {
      throw new IllegalArgumentException(
          "Order identifier could not be resolved for order (method='%s', parent='%s', item='%s', bulk='%s')."
              .formatted(order.method(), order.parent(), order.item(), order.isBulk())
              + "\\nNo identifier was found in the order or in the shared test state, so the test cannot continue.");
  }
}
'''
        self.assertEqual(source, self.format(source, maximum=160)[0])

    def test_lists_followed_by_chains_ternaries_or_concatenation_do_not_wrap(self):
        source = '''class X {
  void f() {
    val result = Objects.equals(first, second) ? compute(alpha, beta) : other(gamma, delta);
    throw new IllegalStateException("Missing %s for %s".formatted(first, second) + " in the test context");
  }
}
'''
        result, _ = self.format(source, maximum=85)
        self.assertIn('    val result = Objects.equals(first, second) ? compute(alpha, beta) : other(\n        gamma, delta);', result)
        self.assertIn('    throw new IllegalStateException(\n        "Missing %s for %s".formatted(first, second) + " in the test context");', result)
        result, report = self.format(source, maximum=70)
        self.assertEqual(source, result)
        self.assertEqual(2, report['unbreakable_spans'])

    def test_map_of_key_value_rows_stay_intact(self):
        source = '''class X {
  private static final Map<String, List<Item>> SAMPLE_ITEMS = Map.of(
      BOOK_CATEGORY, generateItems(BOOK_CATEGORY, 10),
      MUSIC_CATEGORY, generateItems(
          MUSIC_CATEGORY,
          10));
}
'''
        result, _ = self.format(source, maximum=160)
        self.assertEqual(source.replace('generateItems(\n          MUSIC_CATEGORY,\n          10)',
            'generateItems(MUSIC_CATEGORY, 10)'), result)

    def test_lambda_arrow_break_is_kept_while_its_call_collapses(self):
        source = '''class X {
  void f() {
    ifPresent(
        request.delegate(),
        delegate ->
            builder.header(
                HttpHeaders.AUTHORIZATION,
                TokenAuthenticationScheme.DELEGATED_TOKEN_PREFIX + " " + delegate));
  }
}
'''
        result, _ = self.format(source, maximum=160)
        self.assertEqual(source.replace('builder.header(\n                HttpHeaders.AUTHORIZATION,\n                ',
            'builder.header(HttpHeaders.AUTHORIZATION, '), result)

    def test_list_with_multiline_argument_keeps_its_own_breaks(self):
        source = '''class X {
  void f() {
    ifPresent(
        request.page(), page -> builder
            .param(PageParameters.SIZE, String.valueOf(page.getPageSize()))
            .param(PageParameters.NUMBER, String.valueOf(
                page.getPageNumber())));
  }
}
'''
        result, _ = self.format(source, maximum=160)
        self.assertEqual(source.replace('String.valueOf(\n                page', 'String.valueOf(page'), result)

    def test_verification_rejects_whitespace_changes_outside_lists(self):
        source = b'class X {\n  void f() {\n    call(first)\n        .then(second);\n  }\n}\n'
        parsed = Source(source, Style())
        parsed.verify(source.replace(b'call(first)', b'call( first )'))
        with self.assertRaisesRegex(ValueError, 'outside argument or parameter lists'):
            parsed.verify(source.replace(b')\n        .then', b').then'))


if __name__ == '__main__':
    unittest.main()

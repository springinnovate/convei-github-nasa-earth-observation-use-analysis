"""Check structural evidence selection against bounded, offline source fixtures."""

import unittest
from unittest.mock import patch

from nasa_eo_search.source_context import (
    MAX_SELECTED_LINES,
    select_enclosing_source_lines,
)


class PythonSourceContextTests(unittest.TestCase):
    """Preserve original Python coordinates and show how matched data is used."""

    def test_function_header_decorators_and_multiline_arguments_are_selected(self) -> None:
        """Keep decorators and multiline function headers beside a body match."""
        source_lines = [
            "# Original line one",
            "@cached_result(timeout=60)",
            "def load_product(",
            "    product_name,",
            "    endpoint,",
            "):",
            "    result = download('ATL03', endpoint)",
            "    return result",
        ]
        selection = select_enclosing_source_lines("module.py", source_lines, [7])
        self.assertEqual(selection["line_numbers"][:5], [3, 2, 4, 5, 6])
        self.assertIn(8, selection["line_numbers"])
        self.assertEqual(source_lines[selection["line_numbers"][0] - 1], "def load_product(")

    def test_nested_function_prioritizes_nearest_definition(self) -> None:
        """Put the inner definition before its containing function header."""
        source_lines = [
            "def run_analysis():",
            "    def load_photons():",
            "        return download('ATL03')",
            "    return load_photons()",
        ]
        selection = select_enclosing_source_lines("analysis.py", source_lines, [3])
        self.assertEqual(selection["line_numbers"][:2], [2, 1])
        self.assertEqual(set(selection["line_numbers"]), {1, 2, 3, 4})

    def test_match_in_decorator_includes_the_decorated_definition(self) -> None:
        """Associate product-specific decorators with the function they wrap."""
        source_lines = ["@register_product('ATL03')", "def read_photons():", "    return read_hdf5()"]
        selection = select_enclosing_source_lines("reader.py", source_lines, [1])
        self.assertEqual(selection["line_numbers"][:2], [2, 1])
        self.assertIn(3, selection["line_numbers"])

    def test_large_class_attribute_links_only_its_consuming_method(self) -> None:
        """Show a class, its long identifier mapping, and the method reading it."""
        source_lines = ["class RadarProduct(BaseProduct):", "    collection_ids = {"]
        source_lines.extend(f"        'C{index}-PROVIDER'," for index in range(180))
        source_lines.extend([
            "    }",
            "",
            "    def unrelated(self):",
            "        return 'unrelated body'",
            "",
            "    @staticmethod",
            "    def recognizes(item):",
            "        concept_id = item['collection']",
            "        return concept_id in RadarProduct.collection_ids",
        ])
        consumer_line = source_lines.index("    def recognizes(item):") + 1
        selection = select_enclosing_source_lines("Products/RadarProduct.py", source_lines, [120])
        self.assertIn(1, selection["line_numbers"][:10])
        self.assertIn(2, selection["line_numbers"][:10])
        self.assertIn(consumer_line, selection["line_numbers"][:10])
        self.assertIn(consumer_line - 1, selection["line_numbers"][:10])
        self.assertIn(consumer_line + 2, selection["line_numbers"])
        self.assertNotIn(source_lines.index("        return 'unrelated body'") + 1, selection["line_numbers"])
        self.assertTrue(any("middle is omitted" in note for note in selection["notes"]))

    def test_module_mapping_consumer_excludes_same_named_parameter(self) -> None:
        """Use a global mapping's reader without following shadowed parameters."""
        source_lines = [
            "collections = {'ATL03': 'C123-PROVIDER'}",
            *["# separation"] * 30,
            "def resolve_product(name):",
            "    return collections[name]",
            "",
            "def unrelated(collections):",
            "    return collections[0]",
        ]
        selection = select_enclosing_source_lines("mapping.py", source_lines, [1])
        self.assertIn(32, selection["line_numbers"][:5])
        self.assertIn(33, selection["line_numbers"])
        self.assertNotIn(35, selection["line_numbers"])

    def test_module_is_parsed_without_execution(self) -> None:
        """Treat executable-looking source exclusively as untrusted text."""
        source_lines = ["raise RuntimeError('source was executed')", "dataset = 'ATL03'"]
        selection = select_enclosing_source_lines("untrusted.py", source_lines, [2])
        self.assertIn(2, selection["line_numbers"])
        self.assertTrue(any("does not establish a connection" in note for note in selection["notes"]))

    def test_short_top_level_script_keeps_distant_consuming_statements(self) -> None:
        """Retain later parsing, retrieval and output without executing a short script."""
        source_lines = [
            "# Reusable download command.",
            "raise RuntimeError('must not execute')",
            "options.add_argument('--dataset', default='EXAMPLE_PRODUCT')",
            *["# other setup"] * 35,
            "settings = options.parse_args()",
            *["# validate user settings"] * 35,
            "response = download(settings.dataset)",
            "save_response(response, settings.output)",
        ]
        selection = select_enclosing_source_lines("download.py", source_lines, [3])
        self.assertEqual(set(selection["line_numbers"]), set(range(1, len(source_lines) + 1)))
        self.assertIn(source_lines.index("settings = options.parse_args()") + 1, selection["line_numbers"])
        self.assertIn(len(source_lines), selection["line_numbers"])

    def test_malformed_top_level_script_retains_code_and_syntax_limitation(self) -> None:
        """Show distant operations in a small broken script without guessing execution."""
        source_lines = [
            "# Download a user-selected dataset.",
            "arguments.add_argument('--product', default='EXAMPLE_PRODUCT')",
            "arguments.add_argument('--region', help='region'",
            *["# intervening argument setup"] * 35,
            "selection = arguments.parse_args()",
            *["# intervening validation"] * 35,
            "result = fetch(selection.product)",
            "write_result(result)",
        ]
        selection = select_enclosing_source_lines("broken_download.py", source_lines, [2])
        self.assertIn(len(source_lines) - 1, selection["line_numbers"])
        self.assertIn(len(source_lines), selection["line_numbers"])
        self.assertIn("SyntaxError", selection["notes"][0])
        self.assertIn("syntax and data flow are unverified", selection["notes"][0])
        self.assertTrue(any("may include unrelated operations" in note for note in selection["notes"]))

    def test_static_method_parameter_is_not_treated_as_class_instance(self) -> None:
        """Exclude another object's same-named attribute inside static methods."""
        source_lines = ["class Product:", "    identifiers = {'ATL03'}", *["    # separation"] * 30]
        source_lines.extend([
            "    @staticmethod",
            "    def different_object(other):",
            "        return other.identifiers",
            "    @staticmethod",
            "    def actual_consumer(item):",
            "        return item in Product.identifiers",
        ])
        selection = select_enclosing_source_lines("products.py", source_lines, [2])
        self.assertNotIn(34, selection["line_numbers"])
        self.assertNotIn(35, selection["line_numbers"])
        self.assertIn(37, selection["line_numbers"])
        self.assertIn(38, selection["line_numbers"])

    def test_import_bindings_shadow_same_named_module_mapping(self) -> None:
        """Exclude direct, aliased, and from-import bindings of a mapping name."""
        for import_statement in (
            "import identifiers",
            "import another_package as identifiers",
            "from another_package import identifiers",
            "from another_package import something as identifiers",
        ):
            with self.subTest(import_statement=import_statement):
                source_lines = ["identifiers = {'ATL03'}", *["# separation"] * 30]
                source_lines.extend(["def unrelated():", f"    {import_statement}", "    return identifiers.resolve()"])
                selection = select_enclosing_source_lines("mapping.py", source_lines, [1])
                self.assertNotIn(32, selection["line_numbers"])
                self.assertNotIn(34, selection["line_numbers"])

    def test_long_consumer_preserves_actual_read_before_truncated_body(self) -> None:
        """Keep a distant mapping read even when its function exceeds the budget."""
        source_lines = ["collections = {'ATL03': 'C123-PROVIDER'}", "def resolve_product(name):"]
        source_lines.extend("    intermediate = 1" for _ in range(200))
        source_lines.append("    return collections[name]")
        selection = select_enclosing_source_lines("mapping.py", source_lines, [1])
        self.assertIn(len(source_lines), selection["line_numbers"][:8])
        self.assertLess(len(selection["line_numbers"]), 120)


class ConfigurationSourceContextTests(unittest.TestCase):
    """Recover bounded configuration mappings and their explicit alias readers."""

    def test_yaml_anchor_links_only_matching_alias_test_block(self) -> None:
        """Connect an opaque identifier list to a distant alias-consuming test."""
        source_lines = ["collections: &collection_ids ["]
        source_lines.extend(f'  "C{index}-PROVIDER",' for index in range(180))
        source_lines.extend([
            "]",
            "",
            "tests:",
            "  - unrelated-test:",
            "      params:",
            "        dataset: OtherProduct",
            "      expected: []",
            "  - test-search NamedProduct:",
            "      params:",
            "        processingLevel:",
            "          - L0B",
            "        dataset:",
            "          - NamedProduct",
            "      expected: [{collections: *collection_ids}]",
            "  - different-test:",
            "      params: ignored",
        ])
        consumer_line = source_lines.index("  - test-search NamedProduct:") + 1
        selection = select_enclosing_source_lines("test_search.yml", source_lines, [160])
        self.assertIn(1, selection["line_numbers"][:5])
        self.assertIn(consumer_line, selection["line_numbers"][:5])
        for line_number in range(consumer_line, consumer_line + 7):
            self.assertIn(line_number, selection["line_numbers"])
        self.assertNotIn(source_lines.index("        dataset: OtherProduct") + 1, selection["line_numbers"])
        self.assertNotIn(source_lines.index("      params: ignored") + 1, selection["line_numbers"])

    def test_yaml_block_anchor_preserves_mapping_and_consumer(self) -> None:
        """Support indented anchor lists as well as flow-style square brackets."""
        source_lines = [
            "products: &product_ids",
            "  - ATL03",
            "  - ATL06",
            *["# separation"] * 20,
            "tests:",
            "  - check-products:",
            "      expected: *product_ids",
            "  - other:",
            "      expected: []",
        ]
        selection = select_enclosing_source_lines("config.yaml", source_lines, [2])
        self.assertIn(1, selection["line_numbers"])
        self.assertIn(25, selection["line_numbers"])
        self.assertIn(26, selection["line_numbers"])
        self.assertNotIn(28, selection["line_numbers"])

    def test_json_quoted_brackets_do_not_change_source_coordinates(self) -> None:
        """Ignore literal bracket characters inside JSON string values."""
        source_lines = [
            "{",
            '  "description": "literal } and [ are not structure",',
            '  "products": [',
            '    {"name": "ATL03", "reader": "photon"}',
            "  ]",
            "}",
        ]
        selection = select_enclosing_source_lines("products.json", source_lines, [4])
        self.assertEqual(selection["line_numbers"][0], 4)
        self.assertEqual(selection["notes"], [])

    def test_quoted_anchor_lookalikes_are_not_followed(self) -> None:
        """Do not treat quoted scalar values as YAML anchor declarations."""
        source_lines = ['name: "&collection_ids ATL03"', *["# separation"] * 20, 'note: "*collection_ids"']
        selection = select_enclosing_source_lines("notes.yml", source_lines, [1])
        self.assertNotIn(22, selection["line_numbers"])

    def test_redeclared_yaml_anchor_only_links_alias_to_latest_definition(self) -> None:
        """Do not connect aliases of a replacement anchor to the original list."""
        source_lines = ["products: &ids [ATL03]", *["# separation"] * 30]
        source_lines.extend([
            "other_products: &ids [unrelated]",
            "tests:",
            "  - unrelated-test:",
            "      expected: *ids",
        ])
        first_selection = select_enclosing_source_lines("mapping.yaml", source_lines, [1])
        self.assertNotIn(34, first_selection["line_numbers"])
        self.assertNotIn(35, first_selection["line_numbers"])
        latest_selection = select_enclosing_source_lines("mapping.yaml", source_lines, [32])
        self.assertIn(34, latest_selection["line_numbers"])
        self.assertIn(35, latest_selection["line_numbers"])


class SourceContextFallbackTests(unittest.TestCase):
    """Keep unavailable structural context visible and all selections bounded."""

    def test_malformed_python_returns_nearby_original_lines_with_note(self) -> None:
        """Keep a syntax failure useful without treating it as parsed context."""
        source_lines = ["# original"] * 20 + ["def broken(:", "    dataset = 'ATL03'"] + ["# end"] * 20
        selection = select_enclosing_source_lines("broken.py", source_lines, [22])
        self.assertEqual(selection["line_numbers"][0], 22)
        self.assertIn("SyntaxError", selection["notes"][0])
        self.assertIn("nearby lines only", selection["notes"][0])
        self.assertEqual(min(selection["line_numbers"]), 12)
        self.assertEqual(max(selection["line_numbers"]), 32)

    def test_oversized_source_is_not_sent_to_parser(self) -> None:
        """Avoid parsing oversized files while preserving real matching lines."""
        source_lines = ["dataset = 'ATL03'", "# " + "x" * 2_000_000]
        with patch("nasa_eo_search.source_context.ast.parse") as parse_source:
            selection = select_enclosing_source_lines("oversized.py", source_lines, [1])
        parse_source.assert_not_called()
        self.assertEqual(selection["line_numbers"], [1, 2])
        self.assertIn("parsing limit", selection["notes"][0])

    def test_unsupported_notebook_uses_explicit_fallback(self) -> None:
        """Avoid interpreting notebook JSON as executable Python line numbers."""
        selection = select_enclosing_source_lines("analysis.ipynb", ['{"cells": ["ATL03"]}'], [1])
        self.assertEqual(selection["line_numbers"], [1])
        self.assertIn("unsupported for .ipynb", selection["notes"][0])

    def test_small_javascript_example_keeps_purpose_and_later_visualization(self) -> None:
        """Include a short demonstration's purpose comment and its final operation."""
        source_lines = ["// Simple image collection demonstration.", *["// setup"] * 11]
        source_lines.extend([
            "const images = dataset('EXAMPLE_PRODUCT');",
            *["// adjust preview"] * 25,
            "const preview = animatedThumbnail(images);",
            "display(preview);",
        ])
        selection = select_enclosing_source_lines("example.js", source_lines, [13])
        self.assertEqual(set(selection["line_numbers"]), set(range(1, len(source_lines) + 1)))
        self.assertLess(selection["line_numbers"].index(1), 12)
        self.assertIn("complete small-script text", selection["notes"][0])

    def test_large_script_keeps_header_but_does_not_expand_to_unrelated_distant_code(self) -> None:
        """Bound script-wide selection by both line count and character count."""
        for source_lines in (
            ["// Worked demonstration.", *["// comment"] * 200],
            ["// Worked demonstration.", *["// " + "x" * 100] * 100],
        ):
            with self.subTest(line_count=len(source_lines)):
                source_lines[49] = "const images = dataset('EXAMPLE_PRODUCT');"
                source_lines[-1] = "unrelated_operation();"
                selection = select_enclosing_source_lines("large.js", source_lines, [50])
                self.assertIn(1, selection["line_numbers"])
                self.assertNotIn(len(source_lines), selection["line_numbers"])
                self.assertLessEqual(len(selection["line_numbers"]), MAX_SELECTED_LINES)
                self.assertIn("bounded file header", selection["notes"][0])

    def test_malformed_configuration_returns_explicit_fallback(self) -> None:
        """Reject mismatched flow-container brackets without guessing a block."""
        selection = select_enclosing_source_lines("bad.yaml", ["products: [", "  ATL03", "}"], [2])
        self.assertIn("ValueError", selection["notes"][0])
        self.assertIn(2, selection["line_numbers"])

    def test_invalid_matching_lines_are_ignored_and_line_limit_is_enforced(self) -> None:
        """Keep every emitted coordinate valid, unique, and within the limit."""
        source_lines = [f"line {index}" for index in range(1000)]
        selection = select_enclosing_source_lines("notes.txt", source_lines, [0, -1, True, 1001, *range(1, 1001)])
        selected_lines = selection["line_numbers"]
        self.assertEqual(len(selected_lines), MAX_SELECTED_LINES)
        self.assertEqual(len(set(selected_lines)), len(selected_lines))
        self.assertTrue(all(1 <= line_number <= len(source_lines) for line_number in selected_lines))
        self.assertTrue(any("limited to 400" in note for note in selection["notes"]))

    def test_many_matches_bound_structural_work_and_report_that_limit(self) -> None:
        """Cap repeated structural walks through a file with many search matches."""
        source_lines = [f"dataset_{index} = 'ATL03'" for index in range(1000)]
        selection = select_enclosing_source_lines("many_matches.py", source_lines, list(range(1, 1001)))
        self.assertLessEqual(len(selection["line_numbers"]), MAX_SELECTED_LINES)
        self.assertTrue(any("first 64 matching lines" in note for note in selection["notes"]))


if __name__ == "__main__":
    unittest.main()

"""Check document-purpose selection, section isolation, and pinned citations."""

from copy import deepcopy
import unittest

from nasa_eo_search.classification_context import add_enclosing_classification_context
from nasa_eo_search.document_context import read_document_structure
from nasa_eo_search.evidence_context import build_evidence_packets
from nasa_eo_search.evidence_inputs import serialize_evidence
from nasa_eo_search.evidence_reports import validate_prepared_blocks
from nasa_eo_search.snapshot_evidence import build_snapshot_excerpt_citation
from nasa_eo_search.source_context import MAX_SELECTED_LINES, select_enclosing_source_lines
from test_evidence_packaging import make_evidence_group


class DocumentPurposeContextTests(unittest.TestCase):
    """Keep worked-example framing visible without borrowing another section."""

    def test_rmarkdown_title_and_intro_survive_distant_product_code(self) -> None:
        """Preserve a vignette's stated purpose before setup and long download code."""
        lines = [
            "---", 'title: "Full worked use case"', "output:", "  html_document:",
            "    toc: true", "---", "```{r setup}", "# Fake application heading", "setup()", "```",
            "## Introduction", "", "This vignette showcases the package with a worked example.", "",
            "## Configure the analysis", *["configuration <- TRUE"] * 35,
            "## Download observations", "The following calls demonstrate data access.", "",
            "```{r}", 'urls <- find_data("ATL03")', "download(urls)", "```",
        ]
        match = lines.index('urls <- find_data("ATL03")') + 1
        selection = select_enclosing_source_lines("use_case.Rmd", lines, [match])
        selected = selection["line_numbers"]
        for number in (2, 11, 13, lines.index("## Download observations") + 1):
            self.assertIn(number, selected)
            self.assertLess(selected.index(number), selected.index(match))
        self.assertNotIn(8, selected)
        self.assertNotIn(15, selected)

    def test_ancestor_example_headings_do_not_pull_sibling_purpose(self) -> None:
        """Retain distant example headings while excluding a neighboring dataset."""
        lines = ["# Package", "## Worked demonstration", "Use this example to learn the API.",
                 "### Python", "", "```python", *["# setup"] * 25,
                 'values = read_data("ATL03")', "```", "",
                 "## Different dataset application", "An unrelated scientific conclusion."]
        match = lines.index('values = read_data("ATL03")') + 1
        selection = select_enclosing_source_lines("README.md", lines, [match])
        self.assertTrue({1, 2, 3, 4}.issubset(selection["line_numbers"]))
        self.assertNotIn(len(lines), selection["line_numbers"])
        self.assertNotIn(len(lines) - 1, selection["line_numbers"])

    def test_intro_under_a_different_root_section_is_not_document_purpose(self) -> None:
        """Do not reuse the introduction of a preceding dataset's section."""
        lines = ["# Other dataset", "## Introduction", "This is only a tutorial for another product.",
                 *[""] * 15, "# Target application", "The target application has its own result.",
                 *[""] * 15, 'read("ATL03")']
        selection = select_enclosing_source_lines("notes.md", lines, [len(lines)])
        self.assertNotIn(1, selection["line_numbers"])
        self.assertNotIn(2, selection["line_numbers"])
        self.assertNotIn(3, selection["line_numbers"])
        self.assertIn(19, selection["line_numbers"])

    def test_previous_peer_example_does_not_explain_a_later_application(self) -> None:
        """Keep a common title without borrowing a previous sibling's tutorial label."""
        lines = ["# Dataset workflows", "General documentation for these workflows.",
                 "## Worked example for another dataset", "This example teaches another API.",
                 *[""] * 15, "## Target application", "This application answers its own question.",
                 *[""] * 15, 'read("ATL03")']
        selection = select_enclosing_source_lines("README.md", lines, [len(lines)])
        self.assertTrue({1, 2, 20, 21}.issubset(selection["line_numbers"]))
        self.assertNotIn(3, selection["line_numbers"])
        self.assertNotIn(4, selection["line_numbers"])

    def test_setext_headings_ignore_fences_front_matter_and_indented_code(self) -> None:
        """Recognize rendered headings rather than similarly shaped source code."""
        lines = ["---", "title: 'Example guide'", "metadata: |", "  # Not a heading", "---",
                 "Worked example", "==============", "Purpose prose.",
                 "~~~~python", "# Not a heading", "Wrong title", "----------", "~~~~",
                 "    # Also not a heading", "", "Source section", "--------------",
                 "```python", "# Still code", "```", 'read("ATL03")']
        headings, title_lines, _, _ = read_document_structure(lines)
        self.assertEqual([(heading["line_number"], heading["end_line"], heading["level"])
                          for heading in headings], [(6, 7, 1), (16, 17, 2)])
        self.assertEqual(title_lines, [2])
        selection = select_enclosing_source_lines("README.markdown", lines, [21])
        self.assertTrue({2, 6, 7, 8, 16, 17}.issubset(selection["line_numbers"]))

    def test_unclosed_fence_or_front_matter_has_explicit_limits(self) -> None:
        """Keep raw matches without claiming to recognize unavailable headings."""
        for lines, expected_note in [
            (["# Guide", "```python", "# Not a real heading", 'read("ATL03")'], "unclosed code fence"),
            (["---", "title: Pending", "# Not a real heading", 'read("ATL03")'], "front matter"),
        ]:
            with self.subTest(expected_note=expected_note):
                selection = select_enclosing_source_lines("README.md", lines, [4])
                self.assertIn(4, selection["line_numbers"])
                self.assertTrue(any(expected_note in note for note in selection["notes"]))

    def test_document_selection_and_introductions_are_bounded(self) -> None:
        """Limit repeated structural work and disclose discarded purpose text."""
        lines = ["# Guide", "## Worked demonstration", "Purpose " + "x" * 2600,
                 "A short useful introduction.", *["setup"] * 25]
        lines.extend(f'read("ATL03_{number}")' for number in range(700))
        selection = select_enclosing_source_lines("README.md", lines, list(range(30, len(lines) + 1)))
        self.assertLessEqual(len(selection["line_numbers"]), MAX_SELECTED_LINES)
        self.assertNotIn(3, selection["line_numbers"])
        self.assertIn(4, selection["line_numbers"])
        self.assertTrue(any("2,400" in note for note in selection["notes"]))
        self.assertTrue(any("first 64" in note for note in selection["notes"]))

    def test_headings_keep_source_coordinates_and_packet_budgets(self) -> None:
        """Carry purpose text into model input without changing original matches."""
        lines = ["# Guide", "## Worked example", "This is a reusable API demonstration.",
                 "", "```python", *["# setup"] * 25, 'read("ATL03")', "```"]
        match = lines.index('read("ATL03")') + 1
        group = make_evidence_group("README.md")
        group["evidence_gaps"] = []
        group["files"][0]["matching_lines"] = [{"line_number": match, "text": lines[match - 1]}]
        group["evidence"] = [build_snapshot_excerpt_citation(
            group["repository"], "a" * 40, "README.md", match, 0, lines[match - 1], "matching_line")]
        original_evidence = deepcopy(group["evidence"])
        original_files = deepcopy(group["files"])
        add_enclosing_classification_context(group, lines)
        group["packets"] = build_evidence_packets(group, 4096)
        packet_text = "\n".join(block["text"] for packet in group["packets"] for block in packet["evidence"])
        self.assertIn("## Worked example", packet_text)
        self.assertIn("This is a reusable API demonstration.", packet_text)
        self.assertEqual(group["evidence"][:len(original_evidence)], original_evidence)
        self.assertEqual(group["files"], original_files)
        for citation in group["evidence"]:
            self.assertEqual(citation["commit"], "a" * 40)
            self.assertEqual(citation["text"], lines[citation["line_number"] - 1])
        self.assertLessEqual(sum(len(item["text"]) for item in group["evidence"]), 12000)
        self.assertTrue(all(len(serialize_evidence(packet).encode("utf-8")) <= 4096 for packet in group["packets"]))
        validate_prepared_blocks(group)


if __name__ == "__main__":
    unittest.main()

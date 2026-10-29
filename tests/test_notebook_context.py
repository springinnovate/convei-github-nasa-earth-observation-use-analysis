"""Check bounded notebook source context, exact citations and output isolation."""

from copy import deepcopy
import json
import unittest

from nasa_eo_search.classification_context import add_enclosing_classification_context
from nasa_eo_search.evidence_context import build_evidence_packets
from nasa_eo_search.evidence_inputs import serialize_evidence
from nasa_eo_search.evidence_reports import validate_prepared_blocks
from nasa_eo_search.snapshot_evidence import build_snapshot_excerpt_citation
from test_evidence_packaging import make_evidence_group


def make_notebook_context_fixture(cells: list[dict], *, compact: bool = False) -> tuple[dict, str]:
    """Create pinned source evidence for the first literal ATL03 notebook hit.

    Args:
        cells: Synthetic notebook cells, including optional stored outputs.
        compact: Write a one-line notebook to exercise mixed raw fields.

    Returns:
        Independent evidence group and complete notebook JSON source.
    """
    contents = json.dumps({"cells": cells, "metadata": {}, "nbformat": 4}, indent=None if compact else 2)
    source_lines = contents.splitlines()
    line_number = next(number for number, line in enumerate(source_lines, 1) if "ATL03" in line)
    matching_line = source_lines[line_number - 1]
    offset = max(0, matching_line.index("ATL03") - 80)
    excerpt = matching_line[offset:offset + 600]
    group = make_evidence_group("analysis.ipynb")
    group["files"][0]["matching_lines"] = [{"line_number": line_number, "text": excerpt, "character_offset": offset}]
    group["files"][0]["sources"] = [{"term": "ATL03"}]
    group["evidence"] = [build_snapshot_excerpt_citation(
        group["repository"], "a" * 40, "analysis.ipynb", line_number, offset, excerpt, "matching_line")]
    group["evidence_gaps"] = []
    group["notebook_cells"] = []
    group["notebook_decoded_lines"] = []
    return group, contents


class NotebookClassificationContextTests(unittest.TestCase):
    """Keep source-cell relationships visible without executing or importing code."""

    def test_markdown_identifier_keeps_later_analysis_without_stored_outputs(self) -> None:
        """Bridge output-heavy raw JSON gaps while retaining source-only citations."""
        cells = [
            {"cell_type": "markdown", "source": ["# Calculate photon-height anomalies for this study\n"]},
            {"cell_type": "markdown", "source": ["## Data source\n", "The input is ATL03.\n"]},
            {"cell_type": "code", "source": ["measurements = load_photon_data()\n"],
             "outputs": [{"text": ["OUTPUT_SENTINEL " + "x" * 2000 + "\n"] * 400}]},
            {"cell_type": "code", "source": ["anomaly = measurements - measurements.mean()\n"]},
            {"cell_type": "code", "source": ["anomaly.plot()\n"]},
        ]
        group, contents = make_notebook_context_fixture(cells)
        original_evidence = deepcopy(group["evidence"])
        original_rows = deepcopy(group["files"])
        add_enclosing_classification_context(group, contents.splitlines())
        packets = build_evidence_packets(group, 24000)
        group["packets"] = packets
        packet_text = "\n".join(block["text"] for packet in packets for block in packet["evidence"])
        self.assertIn("Calculate photon-height anomalies", packet_text)
        self.assertIn("measurements = load_photon_data()", packet_text)
        self.assertIn("anomaly = measurements - measurements.mean()", packet_text)
        self.assertNotIn("OUTPUT_SENTINEL", packet_text)
        self.assertEqual(group["evidence"][:len(original_evidence)], original_evidence)
        self.assertEqual(group["files"], original_rows)
        self.assertLessEqual(sum(len(item["text"]) for item in group["evidence"]), 12000)
        source_lines = contents.splitlines()
        for citation in group["evidence"]:
            self.assertEqual(citation["commit"], "a" * 40)
            self.assertEqual(citation["file"], "analysis.ipynb")
            offset = citation["character_offset"]
            self.assertEqual(source_lines[citation["line_number"] - 1][offset:offset + len(citation["text"])], citation["text"])
        for block in group["notebook_cells"]:
            if block["cell_type"] == "code":
                self.assertEqual(block["kind"], "enclosing_code")
        self.assertTrue(all(len(serialize_evidence(packet).encode("utf-8")) <= 24000 for packet in packets))
        validate_prepared_blocks(group)

    def test_next_dataset_section_does_not_supply_application_context(self) -> None:
        """Stop at the next peer heading rather than attributing another product's code."""
        group, contents = make_notebook_context_fixture([
            {"cell_type": "markdown", "source": ["# Dataset examples\n"]},
            {"cell_type": "markdown", "source": ["## ATL03 data\n"]},
            {"cell_type": "code", "source": ["photon_data = get_photons()\n"]},
            {"cell_type": "markdown", "source": ["## Different precipitation product\n"]},
            {"cell_type": "code", "source": ["rainfall = unrelated_product_analysis()\n"]},
        ])
        add_enclosing_classification_context(group, contents.splitlines())
        packets = build_evidence_packets(group, 24000)
        packet_text = "\n".join(block["text"] for packet in packets for block in packet["evidence"])
        self.assertIn("photon_data = get_photons()", packet_text)
        self.assertNotIn("unrelated_product_analysis", packet_text)

    def test_output_and_metadata_hits_cannot_anchor_unrelated_source_in_any_layout(self) -> None:
        """Keep original non-source hits without adding source as usage evidence."""
        for compact in (False, True):
            for matching_field in ("outputs", "metadata"):
                with self.subTest(compact=compact, matching_field=matching_field):
                    match_value = ([{"text": "ATL03 appears in an old output"}]
                                   if matching_field == "outputs" else {"identifier": "ATL03"})
                    group, contents = make_notebook_context_fixture([
                        {"cell_type": "code", "source": ["process_another_dataset()\n"],
                         matching_field: match_value},
                    ], compact=compact)
                    original_evidence = deepcopy(group["evidence"])
                    add_enclosing_classification_context(group, contents.splitlines())
                    self.assertEqual(group["evidence"], original_evidence)
                    self.assertEqual(group["notebook_cells"], [])
                    self.assertTrue(any("anchored" in gap for gap in group["evidence_gaps"]))

    def test_compact_source_anchor_respects_longer_dataset_identity(self) -> None:
        """Do not turn another product's source into context for a base-name output hit."""
        group, contents = make_notebook_context_fixture([
            {"cell_type": "code", "source": ['load("ATL03_ENHANCED")\n'],
             "outputs": [{"text": "ATL03 appears in cached output"}]},
        ], compact=True)
        group["identifier_identity_rules"] = {"ATL03": [{"term": "ATL03_ENHANCED", "same_product": False}]}
        original_evidence = deepcopy(group["evidence"])
        add_enclosing_classification_context(group, contents.splitlines())
        self.assertFalse(group["notebook_cells"])
        self.assertEqual(group["evidence"], original_evidence)

    def test_compact_source_and_string_source_have_precise_cell_citations(self) -> None:
        """Decode literal source matches while retaining mixed raw JSON for audit."""
        for compact in (False, True):
            with self.subTest(compact=compact):
                group, contents = make_notebook_context_fixture([
                    {"cell_type": "code", "source": 'product = "ATL03"\nread(product)\n',
                     "outputs": [{"text": "UNUSED_OUTPUT"}]},
                ], compact=compact)
                original_evidence = deepcopy(group["evidence"])
                add_enclosing_classification_context(group, contents.splitlines())
                cells = group["notebook_cells"]
                self.assertEqual(len(cells), 1)
                self.assertEqual(cells[0]["text"], 'product = "ATL03"\nread(product)\n')
                self.assertEqual(cells[0]["kind"], "matching_line")
                self.assertEqual(cells[0]["cell_index"], 0)
                self.assertEqual(group["evidence"][:len(original_evidence)], original_evidence)
                added_text = "".join(item["text"] for item in group["evidence"][len(original_evidence):])
                self.assertNotIn("UNUSED_OUTPUT", added_text)
                group["packets"] = build_evidence_packets(group, 4096)
                validate_prepared_blocks(group)

    def test_large_cell_omissions_are_explicit_and_both_text_budgets_hold(self) -> None:
        """Bound added raw/decoded text and leave oversized original matches intact."""
        group, contents = make_notebook_context_fixture([
            {"cell_type": "markdown", "source": ["# ATL03 analysis\n"]},
            {"cell_type": "code", "source": ["# large documentation " * 1000 + "\n"]},
            *[{"cell_type": "code", "source": [f"measure_{number} = data.mean()  # " + "x" * 450 + "\n"]}
              for number in range(30)],
        ])
        original_size = sum(len(item["text"]) for item in group["evidence"])
        add_enclosing_classification_context(group, contents.splitlines())
        self.assertLessEqual(sum(len(item["text"]) for item in group["evidence"]) - original_size, 6000)
        self.assertLessEqual(sum(len(cell["text"]) for cell in group["notebook_cells"]), 6000)
        self.assertTrue(any("omitted" in gap and "cells" in gap for gap in group["evidence_gaps"]))
        packets = build_evidence_packets(group, 4096)
        group["packets"] = packets
        self.assertTrue(all(len(serialize_evidence(packet).encode("utf-8")) <= 4096 for packet in packets))
        validate_prepared_blocks(group)

    def test_compact_context_fragments_are_not_promoted_to_product_matches(self) -> None:
        """Keep separate cells as context even when their raw JSON shares a hit line."""
        group, contents = make_notebook_context_fixture([
            {"cell_type": "markdown", "source": ["# ATL03 analysis\n"]},
            {"cell_type": "code", "source": ["values = read_data()\n", "# " + "x" * 4500 + "\n"]},
        ], compact=True)
        add_enclosing_classification_context(group, contents.splitlines())
        group["packets"] = build_evidence_packets(group, 4096)
        context_blocks = [block for block in group["evidence_blocks"] if block.get("cell_index") == 1]
        self.assertGreater(len(context_blocks), 1)
        self.assertTrue(all(block["kind"] == "enclosing_code" for block in context_blocks))
        validate_prepared_blocks(group)

    def test_fragments_within_a_matched_cell_keep_precise_matching_status(self) -> None:
        """Do not spread one matching raw-line ID across later parts of the same cell."""
        for source_parts in ([ 'product = "ATL03"\n', '# ' + 'x' * 4500 + '\n'],
                             'product = "ATL03"\n# ' + 'x' * 4500 + '\n'):
            with self.subTest(source_layout=type(source_parts).__name__):
                group, contents = make_notebook_context_fixture([
                    {"cell_type": "code", "source": source_parts},
                ], compact=True)
                original_evidence = deepcopy(group["evidence"])
                add_enclosing_classification_context(group, contents.splitlines())
                group["packets"] = build_evidence_packets(group, 4096)
                cell_fragments = [block for block in group["evidence_blocks"] if block.get("cell_index") == 0]
                self.assertGreater(len(cell_fragments), 1)
                for fragment in cell_fragments:
                    self.assertEqual(fragment["kind"], "matching_line" if "ATL03" in fragment["text"] else "enclosing_code")
                self.assertEqual(group["evidence"][:len(original_evidence)], original_evidence)
                validate_prepared_blocks(group)

    def test_malformed_notebook_keeps_original_hits_and_reports_missing_context(self) -> None:
        """Do not fabricate source cells when JSON cannot be decoded safely."""
        group, _ = make_notebook_context_fixture([{ "cell_type": "markdown", "source": ["ATL03"]}])
        contents = '{"cells": ["ATL03", broken]}'
        group["files"][0]["matching_lines"] = [{"line_number": 1, "text": contents}]
        group["evidence"] = [build_snapshot_excerpt_citation(
            group["repository"], "a" * 40, "analysis.ipynb", 1, 0, contents, "matching_line")]
        original_evidence = deepcopy(group["evidence"])
        add_enclosing_classification_context(group, contents.splitlines())
        self.assertEqual(group["evidence"], original_evidence)
        self.assertFalse(group["notebook_cells"])
        self.assertTrue(any("could not be decoded" in gap for gap in group["evidence_gaps"]))


if __name__ == "__main__":
    unittest.main()

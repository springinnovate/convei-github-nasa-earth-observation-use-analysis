"""Test compact evidence inputs, raw provenance, and notebook safety offline."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from nasa_eo_search.evidence_context import build_evidence_packets, enrich_evidence_group, make_line_evidence
from nasa_eo_search.evidence_inputs import serialize_evidence
from nasa_eo_search.evidence_notebooks import decode_notebook_source_cells
from nasa_eo_search.evidence_packaging import reassemble_evidence_lines


def make_evidence_group(file_name: str = "example.py") -> dict:
    """Create a small repository/product group without reading user files.

    Args:
        file_name: Repository-relative file to hold fixture matches.

    Returns:
        In-memory group ready for context enrichment or direct evidence setup.
    """
    return {
        "repository": "github.com/example/project", "warnings": [], "search_complete": True,
        "product": {"provider": "NSIDC", "short_name": "ATL03", "collections": [
            {"title": "ICESat-2 photon data", "summary": "Photon heights.", "version": "007"},
        ]},
        "files": [{"repository": "github.com/example/project", "file": file_name,
                   "commit": "a" * 40, "matching_lines": [{"line_number": 2, "text": 'open("ATL03_003.h5")'}]}],
    }


def add_fixture_lines(group: dict, lines: list[tuple[int, str]], kind: str, file_name: str | None = None) -> None:
    """Append raw line fixtures with actual production citation identities.

    Args:
        group: Test-owned repository/product group to update.
        lines: One-based line numbers and original source text.
        kind: Matching, surrounding, or README evidence category.
        file_name: Optional different source path for duplicates or README text.
    """
    file_record = {**group["files"][0]}
    if file_name is not None:
        file_record["file"] = file_name
    group.setdefault("evidence", []).extend(make_line_evidence(
        file_record, [{"line_number": number, "text": text} for number, text in lines], kind,
    ))


class EvidencePackagingTests(unittest.TestCase):
    """Verify that smaller inputs preserve relevant evidence and audit locations."""

    def test_snapshot_excerpts_are_retained_without_false_whole_line_conflicts(self) -> None:
        """Keep excerpt text and IDs while excluding it from whole-line checks."""
        group = make_evidence_group()
        add_fixture_lines(group, [(2, "ATL03")], "matching_line")
        first_excerpt = {**group["evidence"][0], "character_offset": 0}
        second_excerpt = {**first_excerpt, "text": " tail", "character_offset": 5,
                          "evidence_id": "second-excerpt"}
        excerpts = [first_excerpt, second_excerpt]
        original_excerpts = deepcopy(excerpts)
        for marker in ("excerpt", "character_offset"):
            with self.subTest(marker=marker):
                matching_lines = [
                    {"line_number": 2, "text": excerpt["text"],
                     marker: True if marker == "excerpt" else excerpt["character_offset"]}
                    for excerpt in excerpts
                ]
                matching_files = [{**group["files"][0], "matching_lines": matching_lines}]
                assembled_lines = reassemble_evidence_lines(excerpts, matching_files)
                self.assertEqual(len(assembled_lines), 1)
                self.assertEqual(assembled_lines[0]["text"], "ATL03 tail")
                self.assertEqual(assembled_lines[0]["character_offset"], 0)
                self.assertEqual(assembled_lines[0]["source_ids"],
                                 [first_excerpt["evidence_id"], "second-excerpt"])
                self.assertNotIn("conflicting_fragments", assembled_lines[0])
                self.assertEqual(excerpts, original_excerpts)

    def test_contiguous_lines_form_one_readable_block_with_raw_audit(self) -> None:
        """Keep the operation and its neighboring lines together with short IDs."""
        group = make_evidence_group()
        add_fixture_lines(group, [(1, "def load():"), (3, "    return data")], "surrounding_code")
        add_fixture_lines(group, [(2, '    data = open("ATL03_003.h5")')], "matching_line")
        original = deepcopy(group["evidence"])
        packets = build_evidence_packets(group, 4096)
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0]["format_version"], 2)
        self.assertEqual(packets[0]["evidence"][0]["text"], 'def load():\n    data = open("ATL03_003.h5")\n    return data')
        self.assertEqual(packets[0]["evidence"][0]["evidence_id"], "E1")
        self.assertEqual(group["evidence"], original)
        self.assertEqual(set(group["evidence_blocks"][0]["source_ids"]), {item["evidence_id"] for item in original})
        self.assertNotIn(original[0]["evidence_id"], serialize_evidence(packets))

    def test_readme_is_background_not_an_independent_packet(self) -> None:
        """Bound repeated background while retaining original README lines."""
        group = make_evidence_group()
        add_fixture_lines(group, [(2, 'open("ATL03.h5")')], "matching_line")
        add_fixture_lines(group, [(number, "Background " * 30) for number in range(1, 81)], "readme_context", "README.md")
        packets = build_evidence_packets(group, 4096)
        self.assertEqual(len(packets), 1)
        self.assertEqual(len(packets[0]["repository_context"][0]["text"]), 1000)
        self.assertTrue(any("README" in note for note in group["packaging_notes"]))
        self.assertTrue(all(item["kind"] == "matching_line" for item in packets[0]["evidence"]))
        self.assertEqual(len(group["evidence"]), 81)
        self.assertFalse(group["evidence_gaps"])

    def test_exact_duplicate_blocks_keep_all_source_occurrences(self) -> None:
        """Send identical text once without hiding different source filenames."""
        group = make_evidence_group()
        add_fixture_lines(group, [(2, 'open("ATL03.h5")')], "matching_line", "app.py")
        add_fixture_lines(group, [(8, 'open("ATL03.h5")')], "matching_line", "tests/test_app.py")
        packets = build_evidence_packets(group, 4096)
        self.assertEqual(len(group["evidence_blocks"]), 1)
        self.assertEqual(len(group["evidence_blocks"][0]["locations"]), 2)
        locations = packets[0]["evidence"][0]["locations"]
        self.assertEqual([location["file"] for location in locations], ["app.py", "tests/test_app.py"])
        self.assertNotIn("source_ids", locations[0])

    def test_split_unicode_blocks_are_lossless_and_deterministic(self) -> None:
        """Enforce byte limits without dropping Unicode or changing repeated output."""
        group = make_evidence_group()
        text = "NASA🌎\\\"\n" * 4000
        add_fixture_lines(group, [(2, text)], "matching_line")
        packets = build_evidence_packets(group, 4096)
        original_blocks = deepcopy(group["evidence_blocks"])
        self.assertGreater(len(packets), 1)
        self.assertTrue(all(len(serialize_evidence(packet).encode("utf-8")) <= 4096 for packet in packets))
        self.assertEqual("".join(block["text"] for packet in packets for block in packet["evidence"]), text)
        self.assertEqual(build_evidence_packets(group, 4096), packets)
        self.assertEqual(group["evidence_blocks"], original_blocks)
        self.assertTrue(all(block["source_ids"] for block in group["evidence_blocks"]))

    def test_product_summary_is_bounded_but_catalog_metadata_is_preserved(self) -> None:
        """Use family scope and expose catalog versions without copying huge prose."""
        group = make_evidence_group()
        group["product"]["collections"][0]["summary"] = "Long NASA description. " * 3000
        group["product"]["collections"].append({"title": "Earlier photon data", "version": "003"})
        original = deepcopy(group["product"])
        add_fixture_lines(group, [(2, 'open("ATL03_002.h5")')], "matching_line")
        packets = build_evidence_packets(group, 4096)
        self.assertEqual(group["product"], original)
        self.assertEqual(packets[0]["product"]["catalog_versions"], ["003", "007"])
        self.assertTrue(packets[0]["product"]["description_truncated"])
        self.assertIn("across versions", packets[0]["product"]["classification_scope"])
        self.assertTrue(group["packaging_notes"])

    def test_oversized_context_pieces_are_not_marked_as_matching_code(self) -> None:
        """Keep overflow context neutral instead of creating false ambiguous hits."""
        group = make_evidence_group()
        preceding_context = "# introductory context " * 1200
        following_context = "# later context " * 1200
        match_text = 'open("ATL03.h5")'
        add_fixture_lines(group, [(1, preceding_context), (3, following_context)], "surrounding_code")
        add_fixture_lines(group, [(2, match_text)], "matching_line")
        packets = build_evidence_packets(group, 4096)
        evidence_blocks = [block for packet in packets for block in packet["evidence"]]
        self.assertEqual("".join(block["text"] for block in evidence_blocks), f"{preceding_context}\n{match_text}\n{following_context}")
        self.assertTrue(any(block["kind"] == "surrounding_code" for block in evidence_blocks))
        matching_blocks = [block for block in evidence_blocks if block["kind"] == "matching_line"]
        self.assertTrue(matching_blocks)
        self.assertTrue(all(block["line_number"] <= 2 <= block["end_line"] for block in matching_blocks))
        for block in group["evidence_blocks"]:
            source_items = [item for item in group["evidence"] if item["evidence_id"] in block["source_ids"]]
            self.assertEqual(any(item["kind"] == "matching_line" for item in source_items), block["kind"] == "matching_line")

    def test_oversized_notebook_cells_keep_context_mapping_outside_hit_window(self) -> None:
        """Retain complete decoded cells and raw citations for context-only chunks."""
        group = make_evidence_group("large.ipynb")
        cell_source = ["# introductory context " * 500 + "\n", 'open("ATL03.h5")\n', "# later context " * 500 + "\n"]
        contents = json.dumps({"cells": [{"cell_type": "code", "source": cell_source, "outputs": []}]}, indent=2)
        lines = contents.splitlines()
        number = next(index for index, line in enumerate(lines, 1) if "ATL03" in line)
        group["files"][0]["matching_lines"] = [{"line_number": number, "text": lines[number - 1]}]
        with patch("nasa_eo_search.evidence_context.fetch_public_file_text", side_effect=[{"text": contents}, {"text": "Notebook project"}]):
            enrich_evidence_group(group, {}, False, 0, None)
        packets = build_evidence_packets(group, 4096)
        blocks = [block for packet in packets for block in packet["evidence"]]
        self.assertEqual("".join(block["text"] for block in blocks), "".join(cell_source))
        self.assertTrue(any(block["kind"] == "surrounding_code" for block in blocks))
        self.assertTrue(all(block["source_ids"] for block in group["evidence_blocks"]))
        self.assertTrue(all(len(serialize_evidence(packet).encode("utf-8")) <= 4096 for packet in packets))
        self.assertFalse(group["evidence_gaps"])

    def test_oversized_required_identity_blocks_instead_of_losing_evidence(self) -> None:
        """Reject an impossible packet budget while preserving all source text."""
        group = make_evidence_group()
        group["product"]["short_name"] = "A" * 5000
        add_fixture_lines(group, [(2, "Important matching text")], "matching_line")
        original = deepcopy(group["evidence"])
        with self.assertRaisesRegex(ValueError, "packet-bytes"):
            build_evidence_packets(group, 4096)
        self.assertEqual(group["evidence"], original)

    def test_saved_notebook_strings_decode_without_network_and_keep_outputs(self) -> None:
        """Decode archived escaped strings and preserve output matches with a gap."""
        group = make_evidence_group("example.ipynb")
        add_fixture_lines(group, [(12, '    "dataset = open(\\"ATL03.h5\\")\\n",'),
                                  (13, '    "print(dataset)\\n"'),
                                  (21, '    "Downloaded ATL03_003.h5\\n",')], "matching_line")
        with patch("nasa_eo_search.evidence_context.read_bounded_https") as network:
            packets = build_evidence_packets(group, 4096)
        network.assert_not_called()
        texts = [block["text"] for packet in packets for block in packet["evidence"]]
        self.assertIn('dataset = open("ATL03.h5")\nprint(dataset)\n', texts)
        self.assertIn("Downloaded ATL03_003.h5\n", texts)
        self.assertTrue(group["evidence_gaps"])
        self.assertEqual(len(group["evidence"]), 3)

    def test_full_notebook_cells_decode_and_keep_precise_raw_source_mapping(self) -> None:
        """Decode a complete source cell even when it extends beyond hit windows."""
        group = make_evidence_group("example.ipynb")
        notebook = {"cells": [{"cell_type": "code", "metadata": {}, "outputs": [],
                              "source": ['dataset = open("ATL03.h5")\n', "print(dataset)\n"]}],
                    "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
        contents = json.dumps(notebook, indent=2)
        lines = contents.splitlines()
        number = next(index for index, line in enumerate(lines, 1) if "ATL03" in line)
        group["files"][0]["matching_lines"] = [{"line_number": number, "text": lines[number - 1]}]
        with patch("nasa_eo_search.evidence_context.fetch_public_file_text", side_effect=[{"text": contents}, {"text": "Notebook project"}]):
            enrich_evidence_group(group, {}, False, 0, None)
        packets = build_evidence_packets(group, 4096)
        self.assertEqual(len(packets[0]["evidence"]), 1)
        cell = packets[0]["evidence"][0]
        self.assertEqual(cell["text"], 'dataset = open("ATL03.h5")\nprint(dataset)\n')
        self.assertEqual(cell["cell_type"], "code")
        self.assertEqual(cell["cell_index"], 0)
        self.assertFalse(group["evidence_gaps"])
        matching_ids = {item["evidence_id"] for item in group["evidence"] if item["kind"] == "matching_line"}
        self.assertTrue(matching_ids.issubset(set(group["evidence_blocks"][0]["source_ids"])))

    def test_notebook_output_match_survives_complete_source_decoding(self) -> None:
        """Retain output-only hits instead of mistaking them for executable source."""
        group = make_evidence_group("example.ipynb")
        notebook = {"cells": [{"cell_type": "code", "source": ["print(name)\n"],
                              "outputs": [{"text": ["ATL03 found\n"], "output_type": "stream"}]}]}
        contents = json.dumps(notebook, indent=2)
        matching_line = next((number, line) for number, line in enumerate(contents.splitlines(), 1) if "ATL03" in line)
        add_fixture_lines(group, [matching_line], "matching_line")
        cells, represented, gaps = decode_notebook_source_cells(contents, group["files"][0], group["evidence"])
        self.assertEqual(cells, [])
        self.assertEqual(represented, set())
        self.assertTrue(gaps)
        packets = build_evidence_packets(group, 4096)
        self.assertIn("ATL03 found", packets[0]["evidence"][0]["text"])

    def test_compact_notebook_line_keeps_raw_matches_outside_source(self) -> None:
        """Keep a whole-line JSON match when source and outputs share that line."""
        group = make_evidence_group("example.ipynb")
        contents = json.dumps({"cells": [{"cell_type": "code", "source": ["print(name)"], "outputs": [{"text": "ATL03 output"}]}]})
        add_fixture_lines(group, [(1, contents)], "matching_line")
        cells, represented, gaps = decode_notebook_source_cells(contents, group["files"][0], group["evidence"])
        self.assertEqual(cells, [])
        self.assertEqual(represented, set())
        self.assertTrue(gaps)
        group["notebook_cells"] = cells
        packets = build_evidence_packets(group, 4096)
        self.assertTrue(any(contents == block["text"] for packet in packets for block in packet["evidence"]))

    def test_invalid_notebook_layout_has_explicit_gap_and_original_matches(self) -> None:
        """Leave malformed and unsupported notebooks available for human review."""
        group = make_evidence_group("example.ipynb")
        add_fixture_lines(group, [(1, "ATL03 broken JSON")], "matching_line")
        for contents in ("{bad JSON", '{"worksheets": []}', '{"cells":[null]}'):
            with self.subTest(contents=contents):
                cells, represented, gaps = decode_notebook_source_cells(contents, group["files"][0], group["evidence"])
                self.assertEqual(cells, [])
                self.assertFalse(represented)
                self.assertTrue(gaps)

    def test_missing_line_fragments_are_not_joined_across_character_gaps(self) -> None:
        """Preserve separate fragments rather than inventing missing characters."""
        group = make_evidence_group()
        add_fixture_lines(group, [(2, "ATL03")], "matching_line")
        fragment = {**group["evidence"][0], "text": "tail", "character_offset": 100, "evidence_id": "another-id"}
        group["evidence"].append(fragment)
        packets = build_evidence_packets(group, 4096)
        self.assertEqual([block["text"] for block in packets[0]["evidence"]], ["ATL03", "tail"])


if __name__ == "__main__":
    unittest.main()

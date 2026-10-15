"""Prevent contradictory saved search lines from forming fabricated code text."""

from copy import deepcopy
import unittest

from nasa_eo_search.evidence_context import build_evidence_packets, make_line_evidence
from nasa_eo_search.evidence_packaging import reassemble_evidence_lines


def make_conflicting_evidence_group(original_lines: list[str]) -> dict:
    """Create raw production-format fragments for alternatives at one source line.

    Args:
        original_lines: Complete saved search snippets sharing a file and line.

    Returns:
        Test-owned group with raw citations and full original snippet provenance.
    """
    file_record = {
        "repository": "github.com/example/project", "file": "example.py", "commit": "a" * 40,
        "matching_lines": [{"line_number": 3, "text": text} for text in original_lines],
    }
    evidence = make_line_evidence(file_record, file_record["matching_lines"], "matching_line")
    # Enrichment deduplicates identical raw fragments and sorts by source offset.
    evidence = sorted({item["evidence_id"]: item for item in evidence}.values(),
                      key=lambda item: item["character_offset"])
    return {
        "repository": file_record["repository"], "files": [file_record], "evidence": evidence,
        "warnings": [], "search_complete": True,
        "product": {"provider": "NSIDC", "short_name": "ATL03", "collections": []},
    }


class ConflictingEvidenceFragmentTests(unittest.TestCase):
    """Preserve original alternatives while avoiding guessed fragment associations."""

    def test_conflicting_long_lines_retain_both_originals_without_hybrid_text(self) -> None:
        """Keep complete original snippets instead of mixing their offset fragments."""
        originals = ["A" * 600, "B" * 600]
        group = make_conflicting_evidence_group(originals)
        raw_before = deepcopy(group["evidence"])
        packets = build_evidence_packets(group, 4096)
        self.assertEqual([block["text"] for packet in packets for block in packet["evidence"]], originals)
        self.assertEqual(group["evidence"], raw_before)
        self.assertTrue(any("saved matches disagree" in gap for gap in group["evidence_gaps"]))

    def test_shared_prefix_can_belong_to_both_original_variants(self) -> None:
        """Reuse a deduplicated raw prefix citation without dropping either suffix."""
        originals = ["A" * 500 + "B" * 100, "A" * 500 + "C" * 100]
        group = make_conflicting_evidence_group(originals)
        packets = build_evidence_packets(group, 4096)
        blocks = [block for packet in packets for block in packet["evidence"]]
        self.assertEqual([block["text"] for block in blocks], originals)
        prefix_id = next(item["evidence_id"] for item in group["evidence"] if item["character_offset"] == 0)
        self.assertTrue(all(prefix_id in block["source_ids"] for block in group["evidence_blocks"]))

    def test_missing_variant_provenance_keeps_ambiguous_fragments_separate(self) -> None:
        """Never guess which legacy prefix belongs with a conflicting suffix."""
        group = make_conflicting_evidence_group(["A" * 600, "B" * 600])
        lines = reassemble_evidence_lines(group["evidence"])
        self.assertEqual(sorted(line["text"] for line in lines), sorted(item["text"] for item in group["evidence"]))
        self.assertTrue(all(line["conflicting_fragments"] for line in lines))
        self.assertEqual({source_id for line in lines for source_id in line["source_ids"]},
                         {item["evidence_id"] for item in group["evidence"]})

    def test_variant_ending_at_fragment_boundary_is_not_absorbed_by_longer_line(self) -> None:
        """Keep a complete short alternative when its raw fragments are a prefix."""
        originals = ["A" * 500, "A" * 600]
        group = make_conflicting_evidence_group(originals)
        packets = build_evidence_packets(group, 4096)
        self.assertEqual([block["text"] for packet in packets for block in packet["evidence"]], originals)

    def test_original_provenance_does_not_fill_missing_saved_fragments(self) -> None:
        """Use full snippets to associate evidence, never to recreate missing bytes."""
        group = make_conflicting_evidence_group(["A" * 600, "B" * 600])
        group["evidence"] = [item for item in group["evidence"] if item["text"] != "A" * 100]
        lines = reassemble_evidence_lines(group["evidence"], group["files"])
        self.assertEqual([line["text"] for line in lines], ["A" * 500, "B" * 600])

    def test_nonconflicting_saved_fragments_keep_existing_representation(self) -> None:
        """Leave ordinary legacy line reassembly and citation ordering unchanged."""
        group = make_conflicting_evidence_group(["ATL03 " + "x" * 1200])
        legacy_lines = reassemble_evidence_lines(group["evidence"])
        lines = reassemble_evidence_lines(group["evidence"], group["files"])
        self.assertEqual(lines, legacy_lines)
        self.assertEqual(lines[0]["text"], group["files"][0]["matching_lines"][0]["text"])
        self.assertNotIn("conflicting_fragments", lines[0])


if __name__ == "__main__":
    unittest.main()

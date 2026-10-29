"""Verify catalog identity mappings and disconnected code context offline."""

from copy import deepcopy
import unittest

from nasa_eo_search.evidence_context import make_line_evidence
from nasa_eo_search.evidence_packaging import measure_packet_bytes, package_evidence_blocks


def make_mapping_group(short_name: str = "ASF_ECMWF_SMST", concept_id: str = "C3653529349-ASF") -> dict:
    """Create saved catalog and matching code for one product without services.

    Args:
        short_name: Product family identified by the fixture collection.
        concept_id: Common Metadata Repository collection identifier in code.

    Returns:
        Group with a catalog-backed identifier and one raw matching-line citation.
    """
    file_record = {
        "repository": "github.com/example/project", "file": "datasets.py", "commit": "a" * 40,
        "matching_lines": [{"line_number": 20, "text": f'collection = "{concept_id}"'}],
        "sources": [{"term": concept_id}],
    }
    product = {
        "provider": "ASF", "short_name": short_name,
        "collections": [{"concept_id": concept_id, "entry_id": f"{short_name}_1", "version": "1",
                         "title": "Example NASA dataset", "summary": "An example catalog description."}],
        "candidate_signatures": [
            {"term": term, "sources": [{"concept_id": concept_id, "source_field": source_field}]}
            for term, source_field in ((concept_id, "id"), (short_name, "short_name"), (f"{short_name}_1", "entry_id"))
        ],
    }
    return {
        "repository": file_record["repository"], "product": product, "files": [file_record],
        "search_complete": True, "warnings": [],
        "evidence": make_line_evidence(file_record, file_record["matching_lines"], "matching_line"),
    }


def replace_matching_text(group: dict, text: str) -> None:
    """Replace a fixture match consistently in source and raw evidence records.

    Args:
        group: Test-owned product group to modify.
        text: Complete replacement source line.
    """
    file_record = group["files"][0]
    file_record["matching_lines"] = [{"line_number": 20, "text": text}]
    group["evidence"] = make_line_evidence(file_record, file_record["matching_lines"], "matching_line")


class DatasetMappingTests(unittest.TestCase):
    """Keep catalog identity distinct from citable repository behavior."""

    def test_concept_id_only_code_receives_its_saved_product_mapping(self) -> None:
        """Explain a collection ID even when its product name is absent in code."""
        group = make_mapping_group()
        original_product = deepcopy(group["product"])
        original_evidence = deepcopy(group["evidence"])
        packet = package_evidence_blocks(group, 4096)[0]
        self.assertEqual(packet["product"]["matched_identifiers"], [{
            "term": "C3653529349-ASF", "source_field": "id", "concept_id": "C3653529349-ASF",
            "catalog_version": "1",
            "catalog_url": "https://cmr.earthdata.nasa.gov/search/concepts/C3653529349-ASF.json",
        }])
        self.assertEqual(group["product"], original_product)
        self.assertEqual(group["evidence"], original_evidence)
        self.assertIn("not code use", packet["product"]["identifier_relationship_note"])
        self.assertNotIn("evidence_id", packet["product"]["matched_identifiers"][0])
        self.assertEqual([block["evidence_id"] for block in packet["evidence"]], ["E1"])

    def test_shared_term_keeps_provenance_with_its_own_product(self) -> None:
        """Keep an ambiguous entry name from borrowing another product's source."""
        first_group = make_mapping_group("FIRST_DATASET", "C100-ASF")
        second_group = make_mapping_group("SECOND_DATASET", "C200-ASF")
        for group in (first_group, second_group):
            collection = group["product"]["collections"][0]
            collection["entry_id"] = "SHARED_ENTRY"
            group["product"]["candidate_signatures"] = [{
                "term": "SHARED_ENTRY",
                "sources": [{"concept_id": collection["concept_id"], "source_field": "entry_id"}],
            }]
            replace_matching_text(group, 'fetch("SHARED_ENTRY")')
        first_packet = package_evidence_blocks(first_group, 4096)[0]
        second_packet = package_evidence_blocks(second_group, 4096)[0]
        self.assertEqual(first_packet["product"]["short_name"], "FIRST_DATASET")
        self.assertEqual(second_packet["product"]["short_name"], "SECOND_DATASET")
        self.assertEqual(first_packet["product"]["matched_identifiers"][0]["concept_id"], "C100-ASF")
        self.assertEqual(second_packet["product"]["matched_identifiers"][0]["concept_id"], "C200-ASF")

    def test_substrings_and_unmatched_catalog_identifiers_are_excluded(self) -> None:
        """Require a complete identifier rather than metadata or substring overlap."""
        group = make_mapping_group("ATL03", "C100-ASF")
        replace_matching_text(group, 'name = "MATL03"')
        packet = package_evidence_blocks(group, 4096)[0]
        self.assertEqual(packet["product"]["matched_identifiers"], [])
        replace_matching_text(group, 'open("atl03_007.h5")')
        packet = package_evidence_blocks(group, 4096)[0]
        self.assertEqual([mapping["term"] for mapping in packet["product"]["matched_identifiers"]], ["ATL03"])

    def test_unverified_associations_are_not_invented_from_candidate_text(self) -> None:
        """Reject provenance outside this product or inconsistent source fields."""
        group = make_mapping_group()
        signature = group["product"]["candidate_signatures"][0]
        signature["sources"] = [
            {"concept_id": "C999-OTHER", "source_field": "id"},
            {"concept_id": "C3653529349-ASF", "source_field": "entry_id"},
        ]
        packet = package_evidence_blocks(group, 4096)[0]
        self.assertEqual(packet["product"]["matched_identifiers"], [])

    def test_mappings_are_unique_sorted_and_extra_collection_sources_disclosed(self) -> None:
        """Limit repetitive provenance while preserving an explicit omission count."""
        group = make_mapping_group("ATL03", "C100-ASF")
        replace_matching_text(group, 'open("ATL03_007.h5")')
        signature = group["product"]["candidate_signatures"][1]
        for collection_number in range(101, 110):
            concept_id = f"C{collection_number}-ASF"
            group["product"]["collections"].append({"concept_id": concept_id, "version": "1"})
            signature["sources"].append({"concept_id": concept_id, "source_field": "short_name"})
        signature["sources"] = list(reversed(signature["sources"] * 2))
        packet = package_evidence_blocks(group, 8192)[0]
        mappings = packet["product"]["matched_identifiers"]
        self.assertEqual(len(mappings), 8)
        self.assertEqual([mapping["concept_id"] for mapping in mappings], [f"C{number}-ASF" for number in range(100, 108)])
        self.assertEqual(packet["product"]["identifier_sources_omitted"], {"ATL03": 2})
        self.assertTrue(any("2 additional sources" in note for note in group["packaging_notes"]))

    def test_fragmented_packets_keep_whole_identifiers_and_only_local_mappings(self) -> None:
        """Preserve a complete mapped identifier across lossless packet splitting."""
        group = make_mapping_group()
        text = "prefix " * 300 + '"C3653529349-ASF"' + " suffix" * 300
        replace_matching_text(group, text)
        packets = package_evidence_blocks(group, 2048)
        self.assertGreater(len(packets), 1)
        self.assertTrue(all(measure_packet_bytes(packet) <= 2048 for packet in packets))
        self.assertEqual("".join(block["text"] for packet in packets for block in packet["evidence"]), text)
        mapped_packets = [packet for packet in packets if packet["product"]["matched_identifiers"]]
        self.assertEqual(len(mapped_packets), 1)
        self.assertTrue(any("C3653529349-ASF" in block["text"] for block in mapped_packets[0]["evidence"]))
        self.assertTrue(any(not packet["product"]["matched_identifiers"] for packet in packets))

    def test_required_mapping_too_large_for_packet_raises_without_shortening(self) -> None:
        """Reject an impossible byte cap instead of dropping catalog identity."""
        group = make_mapping_group(concept_id="C" + "1" * 5000 + "-ASF")
        original = deepcopy(group["evidence"])
        with self.assertRaisesRegex(ValueError, "packet-bytes"):
            package_evidence_blocks(group, 4096)
        self.assertEqual(group["evidence"], original)

    def test_excess_distinct_matched_identifiers_fail_without_silent_omissions(self) -> None:
        """Disclose the mapping count limit instead of omitting a matched term."""
        group = make_mapping_group()
        product = group["product"]
        product["collections"] = []
        product["candidate_signatures"] = []
        for collection_number in range(33):
            concept_id = f"C{collection_number}-ASF"
            product["collections"].append({"concept_id": concept_id, "version": "1"})
            product["candidate_signatures"].append({
                "term": concept_id, "sources": [{"concept_id": concept_id, "source_field": "id"}],
            })
        replace_matching_text(group, " ".join(signature["term"] for signature in product["candidate_signatures"]))
        original = deepcopy(group["evidence"])
        with self.assertRaisesRegex(ValueError, "32 matched catalog identifiers"):
            package_evidence_blocks(group, 65536)
        self.assertEqual(group["evidence"], original)

    def test_disconnected_enclosing_code_is_retained_with_its_own_kind(self) -> None:
        """Send class declarations and consumer code without inventing search hits."""
        group = make_mapping_group()
        group["evidence"].extend(make_line_evidence(group["files"][0], [
            {"line_number": 1, "text": "class DatasetReader:"},
            {"line_number": 70, "text": "    return metadata['collection'] in collection_ids"},
        ], "enclosing_code"))
        packet = package_evidence_blocks(group, 4096)[0]
        self.assertEqual([block["kind"] for block in packet["evidence"]],
                         ["enclosing_code", "matching_line", "enclosing_code"])
        self.assertEqual([block["line_number"] for block in packet["evidence"]], [1, 20, 70])

    def test_enclosing_code_fragments_remain_context_and_have_no_foreign_mapping(self) -> None:
        """Keep long context distinct after splitting and remove absent identities."""
        group = make_mapping_group()
        group["evidence"].extend(make_line_evidence(group["files"][0], [
            {"line_number": 70, "text": "# consumer details " * 400},
        ], "enclosing_code"))
        packets = package_evidence_blocks(group, 2048)
        context_packets = [packet for packet in packets if all(block["kind"] == "enclosing_code"
                                                              for block in packet["evidence"])]
        self.assertTrue(context_packets)
        self.assertTrue(all(not packet["product"]["matched_identifiers"] for packet in context_packets))
        self.assertTrue(all(measure_packet_bytes(packet) <= 2048 for packet in packets))


if __name__ == "__main__":
    unittest.main()

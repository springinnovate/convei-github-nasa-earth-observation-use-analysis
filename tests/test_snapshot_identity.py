"""Keep distinct catalog products separate in saved-snapshot report evidence."""

from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.evidence_inputs import identify_evidence, read_evidence_json, serialize_evidence
from nasa_eo_search.evidence_model import build_classification_request
from nasa_eo_search.evidence_reports import load_evidence_plan, save_evidence_json
from nasa_eo_search.file_classify import (
    build_file_classification_plan, classify_file_matches, identify_file_classification_source,
)
from nasa_eo_search.repository_scan import scan_repository_zip
from nasa_eo_search.snapshot_evidence import (
    build_snapshot_file_excerpts, hash_snapshot_file, read_snapshot_evidence_groups, snapshot_match_rows,
)
from test_pipeline_catalog import make_pipeline_product, save_pipeline_catalog
from test_file_classify import create_file_classification_arguments
from test_repository_scan import save_repository_zip
from test_snapshot_evidence import replace_snapshot_scan


def create_identity_snapshot_inputs(root: Path, products: list[dict], contents: str) -> Path:
    """Save a broad catalog scan and pinned source for identity integration tests.

    Args:
        root: Existing test-owned temporary directory.
        products: Complete catalog products, including overlapping identifiers.
        contents: UTF-8 source saved at src/read.py inside the repository ZIP.

    Returns:
        Search directory with catalog, immutable ZIP, raw scan, and checksums.
    """
    catalog_directory = save_pipeline_catalog(root, products)
    catalog_summary = read_evidence_json(catalog_directory / "summary.json")
    output_directory = root / "batch"
    output_directory.mkdir()
    (output_directory / "snapshots").mkdir()
    catalog_path = output_directory / "catalog.json"
    catalog_path.write_text(json.dumps({"summary": catalog_summary, "products": products}), encoding="utf-8")
    zip_path = save_repository_zip(output_directory / "snapshots", [("project-commit/src/read.py", contents)])
    terms = [candidate["term"] for product in products for candidate in product["candidate_signatures"]]
    scan = scan_repository_zip(zip_path, terms)
    scan_path = output_directory / "snapshots" / "snapshot.scan.json"
    scan_path.write_text(json.dumps(scan), encoding="utf-8")
    manifest = {"schema_version": 1, "repositories": [{
        "repository": "github.com/example/project", "commit": "a" * 40, "status": scan["status"],
        "archive_relative_path": "snapshots/snapshot.zip", "archive_sha256": hash_snapshot_file(zip_path),
        "scan_relative_path": "snapshots/snapshot.scan.json", "scan_sha256": hash_snapshot_file(scan_path),
        "discovered_by": ["github:SPL3SMP"], "error": "", "metrics": {},
    }]}
    (output_directory / "snapshots.json").write_text(json.dumps(manifest), encoding="utf-8")
    plan = {"schema_version": 1, "backend": "github", "report_unit": "file",
            "catalog_directory": str(catalog_directory), "catalog_summary": catalog_summary,
            "catalog_sha256": hash_snapshot_file(catalog_path)}
    (output_directory / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (output_directory / "summary.json").write_text('{"status":"complete"}', encoding="utf-8")
    return output_directory


class SnapshotIdentityTests(unittest.TestCase):
    """Filter incorrect product associations without changing saved repository text."""

    def test_enhanced_only_source_does_not_become_a_base_product_row(self) -> None:
        """Report enhanced SMAP while preserving the broad scan and legacy evidence."""
        products = [make_pipeline_product("SPL3SMP"), make_pipeline_product("SPL3SMP_E")]
        with TemporaryDirectory() as temporary_directory:
            directory = create_identity_snapshot_inputs(
                Path(temporary_directory), products, "load('NASA/SMAP/SPL3SMP_E/006')\n",
            )
            original_bytes = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
            source = read_snapshot_evidence_groups(directory)
            legacy_source = read_snapshot_evidence_groups(directory, apply_catalog_identity=False)
            self.assertEqual([row["short_name"] for row in snapshot_match_rows(source)], ["SPL3SMP_E"])
            self.assertEqual({row["short_name"] for row in snapshot_match_rows(legacy_source)},
                             {"SPL3SMP", "SPL3SMP_E"})
            self.assertNotEqual(identify_file_classification_source(source),
                                identify_file_classification_source(legacy_source))
            self.assertEqual(original_bytes, {path: path.read_bytes() for path in original_bytes})

    def test_separate_base_mentions_survive_on_a_mixed_source_line(self) -> None:
        """Keep both products but remove enhanced-only lines from the base match."""
        products = [make_pipeline_product("SPL3SMP"), make_pipeline_product("SPL3SMP_E")]
        contents = "load('SPL3SMP_E')\ncompare('SPL3SMP', 'SPL3SMP_E')\n"
        with TemporaryDirectory() as temporary_directory:
            directory = create_identity_snapshot_inputs(Path(temporary_directory), products, contents)
            source = read_snapshot_evidence_groups(directory)
            records = {group["product"]["short_name"]: group["files"][0] for group in source["groups"]}
            self.assertEqual(set(records), {"SPL3SMP", "SPL3SMP_E"})
            self.assertEqual({line["line_number"] for line in records["SPL3SMP"]["matching_lines"]}, {2})
            self.assertEqual({line["line_number"] for line in records["SPL3SMP_E"]["matching_lines"]}, {1, 2})

    def test_rejected_prefixes_cannot_exhaust_the_excerpt_window_limit(self) -> None:
        """Center the base excerpt on its distant real occurrence after many siblings."""
        products = [make_pipeline_product("SPL3SMP"), make_pipeline_product("SPL3SMP_E")]
        contents = "冰🙂 " + ("SPL3SMP_E " + "x" * 1000 + " ") * 20 + " load('SPL3SMP')"
        with TemporaryDirectory() as temporary_directory:
            directory = create_identity_snapshot_inputs(Path(temporary_directory), products, contents)
            source = read_snapshot_evidence_groups(directory)
            base_group = next(group for group in source["groups"] if group["product"]["short_name"] == "SPL3SMP")
            excerpts = base_group["files"][0]["matching_lines"]
            self.assertEqual(len(excerpts), 1)
            final_sibling_end = contents.rindex("SPL3SMP_E") + len("SPL3SMP_E")
            self.assertGreater(excerpts[0]["character_offset"], final_sibling_end)
            self.assertIn("load('SPL3SMP')", excerpts[0]["text"])
            self.assertNotIn("SPL3SMP_E", excerpts[0]["text"])
            offset = excerpts[0]["character_offset"]
            self.assertEqual(excerpts[0]["text"], contents[offset:offset + len(excerpts[0]["text"])])

    def test_case_insensitive_identity_preserves_original_source_characters(self) -> None:
        """Recognize lowercase enhanced identifiers without changing Unicode offsets."""
        products = [make_pipeline_product("SPL3SMP"), make_pipeline_product("SPL3SMP_E")]
        contents = "冰🙂 load('nasa/smap/spl3smp_e/006')"
        with TemporaryDirectory() as temporary_directory:
            directory = create_identity_snapshot_inputs(Path(temporary_directory), products, contents)
            source = read_snapshot_evidence_groups(directory)
            self.assertEqual([group["product"]["short_name"] for group in source["groups"]], ["SPL3SMP_E"])
            excerpt = source["groups"][0]["files"][0]["matching_lines"][0]
            self.assertEqual(excerpt["text"], contents)
            self.assertEqual(excerpt["character_offset"], 0)

    def test_filtered_associations_still_validate_every_saved_source_line(self) -> None:
        """Reject invalid raw lines before an identity filter could discard them."""
        products = [make_pipeline_product("SPL3SMP"), make_pipeline_product("SPL3SMP_E")]
        for invalid_line in ({"line_number": 1, "text": "invented SPL3SMP_E"},
                             {"line_number": 999, "text": "load('SPL3SMP_E')"}):
            with self.subTest(invalid_line=invalid_line), TemporaryDirectory() as temporary_directory:
                directory = create_identity_snapshot_inputs(
                    Path(temporary_directory), products, "load('SPL3SMP_E')\n",
                )
                scan = read_evidence_json(directory / "snapshots" / "snapshot.scan.json")
                base_match = next(match for match in scan["matches"] if match["term"] == "SPL3SMP")
                base_match["matching_lines"] = [invalid_line]
                replace_snapshot_scan(directory, scan)
                with self.assertRaisesRegex(ValueError, "differs from its pinned source"):
                    read_snapshot_evidence_groups(directory)

    def test_same_product_versions_and_unknown_granule_suffixes_remain_matches(self) -> None:
        """Keep catalog entry aliases and old granule filenames for their own product."""
        products = [make_pipeline_product("ATL03"), make_pipeline_product("ATL03_E")]
        contents = "read('ATL03_007')\nread('ATL03_20181014001049_02350102_003_01.h5')\n"
        with TemporaryDirectory() as temporary_directory:
            directory = create_identity_snapshot_inputs(Path(temporary_directory), products, contents)
            source = read_snapshot_evidence_groups(directory)
            rows = snapshot_match_rows(source)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["short_name"], "ATL03")
            self.assertEqual(rows[0]["line_numbers"], "1; 2")
            self.assertIn("ATL03_007", rows[0]["search_terms"])

    def test_shared_catalog_aliases_keep_every_recorded_product_owner(self) -> None:
        """Preserve ambiguous shared entry IDs instead of arbitrarily selecting an owner."""
        products = [make_pipeline_product("PRODUCT_A"), make_pipeline_product("PRODUCT_B")]
        for product in products:
            product["collections"][0]["entry_id"] = "SHARED_ALIAS"
            product["candidate_signatures"][1]["term"] = "SHARED_ALIAS"
        with TemporaryDirectory() as temporary_directory:
            directory = create_identity_snapshot_inputs(Path(temporary_directory), products, "load('SHARED_ALIAS')")
            source = read_snapshot_evidence_groups(directory)
            self.assertEqual({row["short_name"] for row in snapshot_match_rows(source)},
                             {"PRODUCT_A", "PRODUCT_B"})

    def test_unaffected_matches_keep_identical_rows_and_model_requests(self) -> None:
        """Reuse exact paid-request identities when source contains no foreign identifier."""
        products = [make_pipeline_product("SPL3SMP"), make_pipeline_product("SPL3SMP_E")]
        with TemporaryDirectory() as temporary_directory:
            directory = create_identity_snapshot_inputs(Path(temporary_directory), products, "load('SPL3SMP')\n")
            current_source = read_snapshot_evidence_groups(directory)
            legacy_source = read_snapshot_evidence_groups(directory, apply_catalog_identity=False)
            self.assertEqual(snapshot_match_rows(current_source), snapshot_match_rows(legacy_source))
            with redirect_stderr(StringIO()):
                current_plan = build_file_classification_plan(current_source, True, 10, 48_000, None)
                legacy_plan = build_file_classification_plan(legacy_source, True, 10, 48_000, None)
            self.assertEqual(len(current_plan["groups"]), 1)
            self.assertEqual(current_plan["groups"][0]["packets"], legacy_plan["groups"][0]["packets"])
            current_request = build_classification_request(current_plan["groups"][0]["packets"][0],
                                                           "test-model", 8192, current_plan["prompt_version"])
            legacy_request = build_classification_request(legacy_plan["groups"][0]["packets"][0],
                                                          "test-model", 8192, legacy_plan["prompt_version"])
            self.assertEqual(serialize_evidence(current_request), serialize_evidence(legacy_request))

    def test_legacy_overlapping_terms_keep_original_nonoverlapping_windows(self) -> None:
        """Preserve archived excerpt selection when overlapping long terms share text."""
        first_term = "A" * 400 + "_" + "B" * 400
        second_term = "B" * 400 + "_" + "C" * 400
        source_line = "A" * 400 + "_" + "B" * 400 + "_" + "C" * 400
        matches = {term: [{"line_number": 1, "text": source_line}]
                   for term in (first_term, second_term)}
        for identity_rules in (None, {}):
            with self.subTest(identity_rules=identity_rules):
                excerpts = build_snapshot_file_excerpts(
                    "github.com/example/project", "a" * 40, "source.py", [source_line],
                    matches, identity_rules,
                )
                self.assertEqual(excerpts["matching_windows"], 1)
                self.assertEqual(excerpts["matching_lines"], [{
                    "line_number": 1, "character_offset": 0, "text": first_term, "excerpt": True,
                }])

    def test_legacy_resume_preserves_original_plan_and_new_output_uses_identity_filter(self) -> None:
        """Resume frozen old associations while applying filtering to a new preparation."""
        products = [make_pipeline_product("SPL3SMP"), make_pipeline_product("SPL3SMP_E")]
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = create_identity_snapshot_inputs(root, products, "load('SPL3SMP_E')\n")
            legacy_source = read_snapshot_evidence_groups(directory, apply_catalog_identity=False)
            with redirect_stderr(StringIO()):
                legacy_plan = build_file_classification_plan(legacy_source, True, 10, 48_000, None)
            self.assertNotIn("dataset_identity_policy", legacy_plan)
            arguments = create_file_classification_arguments(root, packet_bytes=48_000)
            arguments.output.mkdir()
            plan_path = arguments.output / "plan.json"
            save_evidence_json(plan_path, {
                "schema_version": 3, "plan_id": identify_evidence(legacy_plan), "data": legacy_plan,
            })
            original_plan_bytes = plan_path.read_bytes()
            with patch("nasa_eo_search.file_classify.run_prepared_classifications", return_value=0) as runner, \
                    patch("nasa_eo_search.evidence_classify.load_openai_api_key") as key_loader, \
                    patch("nasa_eo_search.evidence_classify.request_evidence_classification") as model_request, \
                    redirect_stderr(StringIO()) as diagnostics:
                self.assertEqual(classify_file_matches(arguments), 0)
                self.assertEqual(plan_path.read_bytes(), original_plan_bytes)
                self.assertIn("Resuming original identifier associations", diagnostics.getvalue())
                new_arguments = create_file_classification_arguments(root, output=root / "classified-new",
                                                                    packet_bytes=48_000)
                self.assertEqual(classify_file_matches(new_arguments), 0)
                self.assertEqual(runner.call_count, 2)
                key_loader.assert_not_called()
                model_request.assert_not_called()
            new_plan = load_evidence_plan(new_arguments.output)
            self.assertTrue(new_plan["dataset_identity_policy"])
            self.assertEqual([group["product"]["short_name"] for group in new_plan["groups"]], ["SPL3SMP_E"])
            self.assertEqual({group["product"]["short_name"] for group in legacy_plan["groups"]},
                             {"SPL3SMP", "SPL3SMP_E"})
            self.assertNotEqual(new_plan["source_evidence_id"], legacy_plan["source_evidence_id"])
            self.assertEqual(plan_path.read_bytes(), original_plan_bytes)


if __name__ == "__main__":
    unittest.main()

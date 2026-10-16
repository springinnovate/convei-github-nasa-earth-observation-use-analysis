"""Verify bounded local snapshot excerpts and their immutable source provenance."""

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from nasa_eo_search.evidence_inputs import read_evidence_json
from nasa_eo_search.evidence_packaging import reassemble_evidence_lines
from nasa_eo_search.file_classify import identify_file_classification_source
from nasa_eo_search.snapshot_evidence import (
    MAX_EVIDENCE_CHARACTERS, SNAPSHOT_MATCH_COLUMNS, bound_snapshot_file_evidence,
    hash_snapshot_file, read_snapshot_evidence_groups, snapshot_match_rows,
)
from nasa_eo_search.trial_scan import scan_repository_archive
from test_pipeline_catalog import make_pipeline_product, save_pipeline_catalog
from test_trial_scan import save_repository_archive


def create_snapshot_inputs(root: Path, contents: str = 'import h5py\nload("ATL03")\nload("ATL06")\n') -> tuple[Path, Path]:
    """Save a complete two-product snapshot search with verified artifact hashes.

    Args:
        root: Existing test-owned temporary directory.
        contents: Original text to place in the pinned repository file.

    Returns:
        Search output and original catalog directories.
    """
    products = [make_pipeline_product("ATL03"), make_pipeline_product("ATL06")]
    catalog_directory = save_pipeline_catalog(root, products)
    catalog_summary = read_evidence_json(catalog_directory / "summary.json")
    output_directory = root / "batch"
    output_directory.mkdir()
    (output_directory / "snapshots").mkdir()
    catalog_path = output_directory / "catalog.json"
    catalog_path.write_text(json.dumps({"summary": catalog_summary, "products": products}), encoding="utf-8")
    archive_path = save_repository_archive(output_directory / "snapshots", [("project-commit/src/read.py", contents)])
    terms = [candidate["term"] for product in products for candidate in product["candidate_signatures"]]
    scan = scan_repository_archive(archive_path, terms)
    scan_path = output_directory / "snapshots" / "snapshot.scan.json"
    scan_path.write_text(json.dumps(scan), encoding="utf-8")
    manifest = {"schema_version": 1, "repositories": [{
        "repository": "github.com/example/project", "commit": "a" * 40, "status": scan["status"],
        "archive_relative_path": "snapshots/snapshot.zip", "archive_sha256": hash_snapshot_file(archive_path),
        "scan_relative_path": "snapshots/snapshot.scan.json", "scan_sha256": hash_snapshot_file(scan_path),
        "discovered_by": ["github:ATL03"], "error": "", "metrics": {},
    }]}
    (output_directory / "snapshots.json").write_text(json.dumps(manifest), encoding="utf-8")
    plan = {"schema_version": 1, "backend": "github", "report_unit": "file",
            "catalog_directory": str(catalog_directory), "catalog_summary": catalog_summary,
            "catalog_sha256": hash_snapshot_file(catalog_path)}
    (output_directory / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (output_directory / "summary.json").write_text('{"status":"complete"}', encoding="utf-8")
    return output_directory, catalog_directory


def replace_snapshot_scan(directory: Path, scan: dict) -> None:
    """Update a test scan and its recorded hash to exercise semantic validation.

    Args:
        directory: Test-owned snapshot output.
        scan: Deliberately modified raw scan object.
    """
    scan_path = directory / "snapshots" / "snapshot.scan.json"
    scan_path.write_text(json.dumps(scan), encoding="utf-8")
    manifest = read_evidence_json(directory / "snapshots.json")
    manifest["repositories"][0]["scan_sha256"] = hash_snapshot_file(scan_path)
    (directory / "snapshots.json").write_text(json.dumps(manifest), encoding="utf-8")


class SnapshotEvidenceTests(unittest.TestCase):
    """Keep long-line matches useful without losing offsets or source guarantees."""

    def test_far_long_line_hit_is_centered_and_bounded_with_true_offset(self) -> None:
        """Show an actual hit near a megabyte into a line rather than its prefix."""
        line = "x" * 900_000 + " ATL03 " + "z" * 50_000
        with TemporaryDirectory() as temporary:
            directory, _ = create_snapshot_inputs(Path(temporary), line)
            original_scan = (directory / "snapshots" / "snapshot.scan.json").read_bytes()
            source = read_snapshot_evidence_groups(directory)
            self.assertEqual(len(source["groups"]), 1)
            record = source["groups"][0]["files"][0]
            excerpt = record["matching_lines"][0]
            self.assertGreater(excerpt["character_offset"], 899_000)
            self.assertIn(" ATL03 ", excerpt["text"])
            self.assertEqual(line[excerpt["character_offset"]:excerpt["character_offset"] + len(excerpt["text"])], excerpt["text"])
            self.assertTrue(record["snapshot_evidence_truncated"])
            row = snapshot_match_rows(source)[0]
            self.assertLess(len(row["snippet"]), 12_000)
            self.assertIn("[chars ", row["snippet"])
            self.assertIn("Evidence truncated: yes", row["warnings"])
            self.assertEqual(set(row), set(SNAPSHOT_MATCH_COLUMNS))
            self.assertEqual((directory / "snapshots" / "snapshot.scan.json").read_bytes(), original_scan)

    def test_matching_and_local_context_share_a_fixed_total_budget(self) -> None:
        """Bound repeated matches and neighboring text with explicit omissions."""
        lines = [f"context {number} " + "x" * 900 for number in range(300)]
        for number in range(5, 300, 10):
            lines[number] = 'read("ATL03")'
        matching_lines = [{"line_number": number + 1, "text": text}
                          for number, text in enumerate(lines) if "ATL03" in text]
        bounded = bound_snapshot_file_evidence("github.com/example/project", "a" * 40,
                                              "source.py", lines, {"ATL03": matching_lines})
        self.assertLessEqual(bounded["matching_windows"], 12)
        self.assertLessEqual(sum(len(item["text"]) for item in bounded["evidence"]), MAX_EVIDENCE_CHARACTERS)
        self.assertTrue(bounded["truncated"])
        self.assertTrue(any(item["kind"] == "surrounding_code" for item in bounded["evidence"]))
        self.assertEqual(bounded["matching_line_count"], 30)

    def test_two_windows_on_one_line_are_not_conflicting_complete_source_lines(self) -> None:
        """Keep explicit excerpt offsets out of legacy whole-line conflict checks."""
        line = "ATL03 " + "x" * 2000 + " ATL03"
        bounded = bound_snapshot_file_evidence("github.com/example/project", "a" * 40,
                                              "source.py", [line], {"ATL03": [{"line_number": 1, "text": line}]})
        record = {"file": "source.py", "commit": "a" * 40, "matching_lines": bounded["matching_lines"]}
        reassembled = reassemble_evidence_lines(bounded["evidence"], [record])
        self.assertEqual(len(reassembled), 2)
        self.assertFalse(any(item.get("conflicting_fragments") for item in reassembled))
        legacy = copy.deepcopy(record)
        legacy["matching_lines"] = [{"line_number": 1, "text": "ATL03"}, {"line_number": 1, "text": "different"}]
        self.assertTrue(all(item.get("conflicting_fragments") for item in reassemble_evidence_lines(bounded["evidence"], [legacy])))

    def test_generated_reports_and_summary_timestamps_do_not_change_source_identity(self) -> None:
        """Allow harmless report regeneration without invalidating paid receipts."""
        with TemporaryDirectory() as temporary:
            directory, _ = create_snapshot_inputs(Path(temporary))
            original = read_snapshot_evidence_groups(directory)
            (directory / "matches.csv").write_text("regenerated report", encoding="utf-8")
            (directory / "summary.json").write_text('{"status":"complete","elapsed_seconds":99,"finished_at":"later"}', encoding="utf-8")
            refreshed = read_snapshot_evidence_groups(directory)
            self.assertEqual(identify_file_classification_source(original), identify_file_classification_source(refreshed))
            self.assertEqual(snapshot_match_rows(original), snapshot_match_rows(refreshed))

    def test_changed_artifact_bytes_are_rejected_before_source_reading(self) -> None:
        """Reject changed catalog, archive, or raw scan even when JSON stays valid."""
        for relative_path in ("catalog.json", "snapshots/snapshot.zip", "snapshots/snapshot.scan.json"):
            with self.subTest(artifact=relative_path), TemporaryDirectory() as temporary:
                directory, _ = create_snapshot_inputs(Path(temporary))
                with (directory / relative_path).open("ab") as artifact:
                    artifact.write(b" ")
                with self.assertRaisesRegex(ValueError, "hash differs"):
                    read_snapshot_evidence_groups(directory)

    def test_scan_lines_must_match_actual_archive_even_after_receipt_rehash(self) -> None:
        """Do not accept invented snippet text when a scan digest was also updated."""
        with TemporaryDirectory() as temporary:
            directory, _ = create_snapshot_inputs(Path(temporary))
            scan = read_evidence_json(directory / "snapshots" / "snapshot.scan.json")
            scan["matches"][0]["matching_lines"][0]["text"] = "invented ATL03 use"
            replace_snapshot_scan(directory, scan)
            with self.assertRaisesRegex(ValueError, "differs from its pinned source"):
                read_snapshot_evidence_groups(directory)

    def test_unsafe_manifest_paths_and_unknown_scan_terms_are_rejected(self) -> None:
        """Refuse escaped artifact paths and unsupported term-to-product claims."""
        with TemporaryDirectory() as temporary:
            directory, _ = create_snapshot_inputs(Path(temporary))
            manifest = read_evidence_json(directory / "snapshots.json")
            manifest["repositories"][0]["archive_relative_path"] = "../outside.zip"
            (directory / "snapshots.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsafe artifact"):
                read_snapshot_evidence_groups(directory)
        with TemporaryDirectory() as temporary:
            directory, _ = create_snapshot_inputs(Path(temporary))
            scan = read_evidence_json(directory / "snapshots" / "snapshot.scan.json")
            scan["matches"][0]["term"] = "UNKNOWN"
            replace_snapshot_scan(directory, scan)
            with self.assertRaisesRegex(ValueError, "invalid file, term"):
                read_snapshot_evidence_groups(directory)

    def test_partial_downloaded_record_is_valid_but_not_a_match(self) -> None:
        """Keep resume-safe archive receipts without pretending a scan occurred."""
        with TemporaryDirectory() as temporary:
            directory, _ = create_snapshot_inputs(Path(temporary))
            manifest = read_evidence_json(directory / "snapshots.json")
            manifest["repositories"][0].update(status="downloaded", scan_relative_path="", scan_sha256="")
            (directory / "snapshots.json").write_text(json.dumps(manifest), encoding="utf-8")
            source = read_snapshot_evidence_groups(directory)
            self.assertEqual(source["groups"], [])
            self.assertEqual(source["source_product_queries"][0]["status"], "downloaded")

    def test_invalid_zip_error_receipt_publishes_without_reopening_bad_archive(self) -> None:
        """Retain an invalid-download audit while exposing no fabricated file hits."""
        with TemporaryDirectory() as temporary:
            directory, _ = create_snapshot_inputs(Path(temporary))
            archive_path = directory / "snapshots" / "snapshot.zip"
            archive_path.write_bytes(b"not a ZIP download")
            catalog = read_evidence_json(directory / "catalog.json")
            terms = [candidate["term"] for product in catalog["products"] for candidate in product["candidate_signatures"]]
            scan = scan_repository_archive(archive_path, terms)
            replace_snapshot_scan(directory, scan)
            manifest = read_evidence_json(directory / "snapshots.json")
            manifest["repositories"][0].update(status="error", archive_sha256=hash_snapshot_file(archive_path), error="Invalid archive")
            (directory / "snapshots.json").write_text(json.dumps(manifest), encoding="utf-8")
            source = read_snapshot_evidence_groups(directory)
            self.assertEqual(source["groups"], [])
            self.assertEqual(source["source_product_queries"][0]["status"], "error")

    def test_active_collection_is_only_allowed_for_report_publication(self) -> None:
        """Refuse classification of changing sources while allowing internal reports."""
        with TemporaryDirectory() as temporary:
            directory, _ = create_snapshot_inputs(Path(temporary))
            (directory / "summary.json").write_text('{"status":"running"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Finish or stop"):
                read_snapshot_evidence_groups(directory)
            self.assertEqual(len(read_snapshot_evidence_groups(directory, allow_running=True)["groups"]), 2)

    def test_catalog_override_must_agree_but_original_catalog_is_optional(self) -> None:
        """Use frozen metadata after relocation and reject an incompatible override."""
        with TemporaryDirectory() as temporary:
            directory, catalog = create_snapshot_inputs(Path(temporary))
            self.assertEqual(len(read_snapshot_evidence_groups(directory, catalog)["groups"]), 2)
            products_path = catalog / "products.jsonl"
            products_path.write_text(products_path.read_text(encoding="utf-8").replace("NASA ATL03 photon data", "Changed product title"), encoding="utf-8")
            self.assertEqual(len(read_snapshot_evidence_groups(directory)["groups"]), 2)
            with self.assertRaisesRegex(ValueError, "override differs"):
                read_snapshot_evidence_groups(directory, catalog)


if __name__ == "__main__":
    unittest.main()

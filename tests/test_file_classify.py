"""Exercise the automatic, independently scoped file-classification workflow offline."""

import argparse
import copy
from contextlib import redirect_stderr
import csv
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.evidence_classify import main as evidence_main
from nasa_eo_search.evidence_inputs import identify_evidence, read_evidence_json, read_product_evidence_groups
from nasa_eo_search.evidence_reports import load_evidence_plan, write_evidence_reports
from nasa_eo_search.file_classify import classify_file_matches, identify_file_classification_source
from nasa_eo_search.pipeline_tables import FILE_MATCH_COLUMNS, build_file_match_row
from test_evidence_classify import create_saved_evidence_inputs, make_model_response


def create_file_classification_arguments(root: Path, **changes) -> argparse.Namespace:
    """Create command options whose network and credential calls are mocked by tests.

    Args:
        root: Test-owned directory containing saved batch and catalog inputs.
        **changes: Explicit overrides for particular workflow scenarios.

    Returns:
        Complete namespace consumed by the file-classification command.
    """
    return argparse.Namespace(**{
        "batch_directory": root / "batch", "catalog": None, "output": root / "classified",
        "model": "test-model", "offline": True, "context_lines": 10, "packet_bytes": 24000,
        "max_requests": None, "max_output_tokens": 2048, "ca_bundle": None,
        "api_key_file": None, "dry_run": False, "retry_failed": False, **changes,
    })


def replace_test_batch_files(batch_directory: Path, file_specs: list[tuple[str, str]]) -> None:
    """Give every selected product several distinct saved files or revisions.

    Args:
        batch_directory: Test-owned completed search directory.
        file_specs: Relative file paths and immutable commit identifiers.
    """
    for matches_path in (batch_directory / "searches").glob("*/attempt-*/matches.jsonl"):
        original = json.loads(matches_path.read_text(encoding="utf-8"))
        records = []
        for file_path, commit in file_specs:
            record = {**original, "match": {**original["match"], "path": file_path, "commit": commit}}
            records.append(json.dumps(record))
        matches_path.write_text("\n".join(records) + "\n", encoding="utf-8")
        summary_path = matches_path.parent / "summary.json"
        summary = read_evidence_json(summary_path)
        summary["match_records_saved"] = len(file_specs)
        summary_path.write_text(json.dumps(summary), encoding="utf-8")


def read_classified_file_rows(output_directory: Path) -> list[dict[str, str]]:
    """Read generated classification rows without normalizing their original search fields.

    Args:
        output_directory: Test-owned file-classification output directory.

    Returns:
        Rows from the spreadsheet-safe classified-matches table.
    """
    with (output_directory / "classified-matches.csv").open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def snapshot_test_files(directory: Path) -> dict[str, bytes]:
    """Capture local test output so rejected resumes can be checked for mutations.

    Args:
        directory: Test-owned directory to inspect recursively.

    Returns:
        Relative file paths mapped to their exact saved bytes.
    """
    return {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


class FileClassificationWorkflowTests(unittest.TestCase):
    """Reuse existing receipts while keeping file judgments and legacy runs separate."""

    def setUp(self) -> None:
        """Prevent every workflow test from reading actual credentials or contacting HTTPS."""
        key_patch = patch("nasa_eo_search.evidence_classify.load_openai_api_key", return_value="synthetic-test-key")
        self.key_loader = key_patch.start()
        self.addCleanup(key_patch.stop)
        transport_patch = patch("nasa_eo_search.evidence_context.read_bounded_https", side_effect=AssertionError("No network in tests"))
        self.context_transport = transport_patch.start()
        self.addCleanup(transport_patch.stop)
        tls_patch = patch("nasa_eo_search.evidence_classify.build_tls_context")
        tls_patch.start()
        self.addCleanup(tls_patch.stop)

    def test_each_dataset_file_revision_keeps_its_own_category_and_original_row(self) -> None:
        """Keep relevant and unrelated files in one repository from influencing one another."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            batch, catalog = create_saved_evidence_inputs(root)
            replace_test_batch_files(batch, [("analysis.py", "a" * 40), ("sites.py", "a" * 40), ("analysis.py", "b" * 40)])
            original_plan = read_product_evidence_groups(batch, catalog)
            original_rows = {row["match_id"]: row for group in original_plan["groups"] for record in group["files"]
                             for row in [build_file_match_row(group, record)]}

            def classify_one_file(request_body, api_key, ca_bundle_path):
                """Return different supported categories for each independently scoped file.

                Args:
                    request_body: Actual serialized request constructed by the runner.
                    api_key: Mocked credential unused by this responder.
                    ca_bundle_path: Mocked transport option unused by this responder.

                Returns:
                    A valid synthetic response whose category depends on this file alone.
                """
                packet = json.loads(request_body["input"])
                self.assertEqual(packet["assessment_unit"], "file")
                self.assertTrue(all(item["file"] == packet["assessment_file"] for item in packet["evidence"]))
                response = make_model_response(request_body)
                judgment = json.loads(response["output"][0]["content"][0]["text"])
                judgment["category"] = "unrelated" if packet["assessment_file"] == "sites.py" else (
                    "direct_use" if packet["assessment_commit"] == "a" * 40 else "reference_only"
                )
                response["output"][0]["content"][0]["text"] = json.dumps(judgment)
                return response

            arguments = create_file_classification_arguments(root)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=classify_one_file) as request, redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 0)
                self.assertEqual(request.call_count, 6)
            rows = read_classified_file_rows(arguments.output)
            self.assertEqual(len(rows), 6)
            for row in rows:
                self.assertEqual({field: row[field] for field in FILE_MATCH_COLUMNS}, original_rows[row["match_id"]])
                self.assertEqual(row["status"], "complete")
                self.assertEqual(row["gpt_model"], "test-model")
                expected = "unrelated" if row["file"] == "sites.py" else ("direct_use" if row["commit"] == "a" * 40 else "reference_only")
                self.assertEqual(row["category"], expected)
            plan = load_evidence_plan(arguments.output)
            self.assertEqual(plan["assessment_unit"], "file")
            self.assertTrue(all(len(group["files"]) == 1 for group in plan["groups"]))
            self.assertEqual(read_evidence_json(arguments.output / "plan.json")["schema_version"], 3)
            self.assertFalse((arguments.output / "review.csv").exists())
            summary = read_evidence_json(arguments.output / "summary.json")
            self.assertEqual(summary["file_matches"], 6)
            self.assertNotIn("repository_product_groups", summary)

    def test_default_processes_all_eligible_calls_beyond_the_legacy_twenty_call_limit(self) -> None:
        """Finish every eligible file by default instead of silently stopping at twenty calls."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            batch, _ = create_saved_evidence_inputs(root)
            replace_test_batch_files(batch, [(f"file-{number}.py", "a" * 40) for number in range(11)])
            arguments = create_file_classification_arguments(root)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response) as request, patch(
                "nasa_eo_search.evidence_classify.write_evidence_reports", wraps=write_evidence_reports,
            ) as reports, redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 0)
                self.assertEqual(request.call_count, 22)
                self.assertEqual(reports.call_count, 2)
            self.assertTrue(all(row["status"] == "complete" for row in read_classified_file_rows(arguments.output)))

    def test_optional_request_limit_resumes_and_reuses_success_without_reading_a_key(self) -> None:
        """Resume saved file receipts without refetching context or repaying completed requests."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            create_saved_evidence_inputs(root)
            arguments = create_file_classification_arguments(root, max_requests=1)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response) as request, redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 4)
                self.assertEqual(request.call_count, 1)
                arguments.max_requests = None
                arguments.offline = False
                self.assertEqual(classify_file_matches(arguments), 0)
                self.assertEqual(request.call_count, 2)
                self.key_loader.reset_mock()
                self.assertEqual(classify_file_matches(arguments), 0)
                self.assertEqual(request.call_count, 2)
                self.key_loader.assert_not_called()
                self.context_transport.assert_not_called()

    def test_new_and_resumed_dry_runs_make_no_writes_context_calls_or_key_reads(self) -> None:
        """Estimate new work offline and inspect resumed plans without changing either source."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            create_saved_evidence_inputs(root)
            arguments = create_file_classification_arguments(root, dry_run=True, offline=False)
            original_files = snapshot_test_files(root)
            output = StringIO()
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification") as request, redirect_stderr(output):
                self.assertEqual(classify_file_matches(arguments), 0)
                request.assert_not_called()
                self.key_loader.assert_not_called()
                self.context_transport.assert_not_called()
            self.assertEqual(snapshot_test_files(root), original_files)
            self.assertFalse(arguments.output.exists())
            self.assertIn("saved matching lines only", output.getvalue())
            arguments.dry_run = False
            arguments.offline = True
            arguments.max_requests = 1
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response), redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 4)
            self.key_loader.reset_mock()
            original_files = snapshot_test_files(root)
            arguments.dry_run = True
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification") as request, redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 0)
                request.assert_not_called()
                self.key_loader.assert_not_called()
            self.assertEqual(snapshot_test_files(root), original_files)

    def test_changed_source_is_rejected_before_key_reads_calls_or_output_writes(self) -> None:
        """Require a new output when search results or their product metadata change."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, catalog = create_saved_evidence_inputs(root)
            arguments = create_file_classification_arguments(root, max_requests=1)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response), redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 4)
            original_files = snapshot_test_files(arguments.output)
            products_path = catalog / "products.jsonl"
            products = [json.loads(line) for line in products_path.read_text(encoding="utf-8").splitlines()]
            products[0]["collections"][0]["title"] = "Changed catalog title"
            products_path.write_text("\n".join(json.dumps(product) for product in products), encoding="utf-8")
            self.key_loader.reset_mock()
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification") as request, self.assertRaisesRegex(ValueError, "differs"):
                classify_file_matches(arguments)
            request.assert_not_called()
            self.key_loader.assert_not_called()
            self.assertEqual(snapshot_test_files(arguments.output), original_files)

    def test_repository_level_output_is_not_reinterpreted_as_file_judgments(self) -> None:
        """Leave legacy repository plans and reports untouched when the new command resumes."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            batch, catalog = create_saved_evidence_inputs(root)
            arguments = create_file_classification_arguments(root)
            with redirect_stderr(StringIO()):
                self.assertEqual(evidence_main(["prepare", str(batch), "--catalog", str(catalog), "--output", str(arguments.output), "--offline"]), 0)
            original_files = snapshot_test_files(arguments.output)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification") as request, self.assertRaisesRegex(ValueError, "repository-level"):
                classify_file_matches(arguments)
            request.assert_not_called()
            self.key_loader.assert_not_called()
            self.assertEqual(snapshot_test_files(arguments.output), original_files)

    def test_failed_or_uncertain_receipts_require_an_explicit_retry(self) -> None:
        """Do not automatically repeat requests whose prior charge may already exist."""
        for failure in ("incomplete", "interrupted"):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                root = Path(directory)
                create_saved_evidence_inputs(root)
                arguments = create_file_classification_arguments(root, max_requests=1)
                response_options = {"return_value": {"status": "incomplete"}} if failure == "incomplete" else {"side_effect": KeyboardInterrupt()}
                with patch("nasa_eo_search.evidence_classify.request_evidence_classification", **response_options), redirect_stderr(StringIO()):
                    self.assertEqual(classify_file_matches(arguments), 1 if failure == "incomplete" else 130)
                arguments.max_requests = None
                with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response) as request, redirect_stderr(StringIO()):
                    self.assertEqual(classify_file_matches(arguments), 1)
                    self.assertEqual(request.call_count, 1)
                    arguments.retry_failed = True
                    self.assertEqual(classify_file_matches(arguments), 0)
                    self.assertEqual(request.call_count, 2)

    def test_loader_rejects_another_file_in_a_rehashed_file_plan(self) -> None:
        """Validate file scope independently of the outer plan integrity hash."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            create_saved_evidence_inputs(root)
            arguments = create_file_classification_arguments(root, max_requests=1)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response), redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 4)
            plan_path = arguments.output / "plan.json"
            envelope = read_evidence_json(plan_path)
            envelope["data"]["groups"][0]["evidence_blocks"][0]["file"] = "another-file.py"
            envelope["plan_id"] = identify_evidence(envelope["data"])
            plan_path.write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "another file"):
                load_evidence_plan(arguments.output)

    def test_catalog_path_can_be_overridden_when_saved_path_is_unavailable(self) -> None:
        """Allow users to supply the same saved catalog after relocating their files."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            batch, catalog = create_saved_evidence_inputs(root)
            source_plan = read_evidence_json(batch / "plan.json")
            source_plan["catalog_directory"] = str(root / "unavailable-catalog")
            (batch / "plan.json").write_text(json.dumps(source_plan), encoding="utf-8")
            arguments = create_file_classification_arguments(root, catalog=catalog)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response), redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 0)

    def test_real_classification_does_not_create_locks_or_modify_saved_search_inputs(self) -> None:
        """Keep the search and catalog read-only while classification owns a separate output."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            batch, catalog = create_saved_evidence_inputs(root)
            original_batch = snapshot_test_files(batch)
            original_catalog = snapshot_test_files(catalog)
            arguments = create_file_classification_arguments(root)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response), redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 0)
            self.assertEqual(snapshot_test_files(batch), original_batch)
            self.assertEqual(snapshot_test_files(catalog), original_catalog)
            self.assertFalse((batch / "batch.lock").exists())

    def test_large_file_packets_are_combined_only_within_the_same_file(self) -> None:
        """Keep split-packet contradictions conservative without collapsing other file rows."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            batch, _ = create_saved_evidence_inputs(root)
            for matches_path in (batch / "searches").glob("*/attempt-*/matches.jsonl"):
                record = json.loads(matches_path.read_text(encoding="utf-8"))
                record["match"]["lineMatches"][0]["line"] = "ATL03 ATL06 product evidence " * 400
                matches_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            arguments = create_file_classification_arguments(root, packet_bytes=4096)
            calls_by_product = {}

            def classify_split_file(request_body, api_key, ca_bundle_path):
                """Return conflicting mock judgments to exercise per-file aggregation.

                Args:
                    request_body: Actual bounded request containing one file fragment.
                    api_key: Mock credential unused by the responder.
                    ca_bundle_path: Mock transport configuration.

                Returns:
                    A valid synthetic response with a deterministic conflicting category.
                """
                packet = json.loads(request_body["input"])
                self.assertLessEqual(len(request_body["input"].encode("utf-8")), 4096)
                product = packet["product"]["short_name"]
                calls_by_product[product] = calls_by_product.get(product, 0) + 1
                response = make_model_response(request_body)
                judgment = json.loads(response["output"][0]["content"][0]["text"])
                judgment["category"] = "direct_use" if calls_by_product[product] == 1 else "unrelated"
                response["output"][0]["content"][0]["text"] = json.dumps(judgment)
                return response

            with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=classify_split_file), redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 0)
            rows = read_classified_file_rows(arguments.output)
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(count > 1 for count in calls_by_product.values()))
            self.assertTrue(all(row["category"] == "unclear" for row in rows))
            self.assertTrue(all("conflicting" in row["explanation"] for row in rows))

    def test_regenerated_summary_timestamps_do_not_invalidate_completed_receipts(self) -> None:
        """Reuse paid results after a no-op search report or catalog-summary refresh."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            batch, catalog = create_saved_evidence_inputs(root)
            arguments = create_file_classification_arguments(root)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response) as request, redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 0)
                self.assertEqual(request.call_count, 2)
            for summary_path in (batch / "summary.json", catalog / "summary.json"):
                summary = read_evidence_json(summary_path)
                summary.update(updated_at="later-report-time", started_at="later-start", finished_at="later-end", elapsed_seconds=123)
                summary_path.write_text(json.dumps(summary), encoding="utf-8")
            self.key_loader.reset_mock()
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification") as request, redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 0)
                request.assert_not_called()
                self.key_loader.assert_not_called()

    def test_relocated_unchanged_catalog_can_resume_with_an_explicit_path(self) -> None:
        """Treat local paths as provenance, not evidence or paid-cache identity."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, catalog = create_saved_evidence_inputs(root)
            arguments = create_file_classification_arguments(root, max_requests=1)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response) as request, redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 4)
                relocated_catalog = root / "relocated-catalog"
                catalog.rename(relocated_catalog)
                arguments.catalog = relocated_catalog
                arguments.max_requests = None
                self.assertEqual(classify_file_matches(arguments), 0)
                self.assertEqual(request.call_count, 2)
            self.assertEqual(load_evidence_plan(arguments.output)["catalog_directory"], str(catalog.resolve()))

    def test_semantic_source_hash_still_detects_snippet_revision_and_coverage_changes(self) -> None:
        """Exclude presentation metadata without weakening evidence or search-status checks."""
        with TemporaryDirectory() as directory:
            batch, catalog = create_saved_evidence_inputs(Path(directory))
            source_plan = read_product_evidence_groups(batch, catalog)
            original_id = identify_file_classification_source(source_plan)
            relocated_plan = copy.deepcopy(source_plan)
            relocated_plan.update(batch_directory="moved-search", catalog_directory="moved-catalog")
            relocated_plan["source_batch_summary"]["updated_at"] = "new-time"
            self.assertEqual(identify_file_classification_source(relocated_plan), original_id)
            for changed_field in ("snippet", "revision", "coverage", "catalog_status"):
                changed_plan = copy.deepcopy(source_plan)
                if changed_field == "snippet":
                    changed_plan["groups"][0]["files"][0]["matching_lines"][0]["text"] = "Changed source code"
                elif changed_field == "revision":
                    changed_plan["groups"][0]["files"][0]["commit"] = "b" * 40
                elif changed_field == "coverage":
                    changed_plan["source_product_queries"][0]["status"] = "incomplete"
                else:
                    changed_plan["catalog_status"] = "incomplete"
                with self.subTest(changed_field=changed_field):
                    self.assertNotEqual(identify_file_classification_source(changed_plan), original_id)


if __name__ == "__main__":
    unittest.main()

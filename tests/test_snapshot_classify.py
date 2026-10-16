"""Run snapshot file classification with fake model responses and no network."""

from contextlib import redirect_stderr
import csv
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.batch_reports import write_protected_csv_rows
from nasa_eo_search.evidence_reports import load_evidence_plan
from nasa_eo_search.file_classify import classify_file_matches
from nasa_eo_search.pipeline_tables import FILE_MATCH_COLUMNS
from nasa_eo_search.snapshot_evidence import read_snapshot_evidence_groups, snapshot_match_rows
from test_evidence_classify import make_model_response
from test_file_classify import create_file_classification_arguments, read_classified_file_rows, snapshot_test_files
from test_snapshot_evidence import create_snapshot_inputs


class SnapshotClassificationTests(unittest.TestCase):
    """Preserve original bounded rows through dry runs, classification, and resume."""

    def setUp(self) -> None:
        """Prevent tests from reading real keys or fetching any public context."""
        key_patch = patch("nasa_eo_search.evidence_classify.load_openai_api_key", return_value="synthetic-key")
        self.key_loader = key_patch.start()
        self.addCleanup(key_patch.stop)
        context_patch = patch("nasa_eo_search.file_classify.enrich_evidence_group", side_effect=AssertionError("No public context"))
        self.context_loader = context_patch.start()
        self.addCleanup(context_patch.stop)
        tls_patch = patch("nasa_eo_search.file_classify.build_tls_context", side_effect=AssertionError("No context transport"))
        self.context_tls = tls_patch.start()
        self.addCleanup(tls_patch.stop)
        model_tls_patch = patch("nasa_eo_search.evidence_classify.build_tls_context")
        model_tls_patch.start()
        self.addCleanup(model_tls_patch.stop)

    def test_fake_model_end_to_end_preserves_original_rows_and_uses_local_context(self) -> None:
        """Reuse the exact search row while classifying each file/product separately."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory, _ = create_snapshot_inputs(root)
            source = read_snapshot_evidence_groups(directory)
            original_rows = snapshot_match_rows(source)
            write_protected_csv_rows(directory / "matches.csv", FILE_MATCH_COLUMNS, original_rows)
            with (directory / "matches.csv").open(encoding="utf-8-sig", newline="") as saved:
                original_csv_rows = list(csv.DictReader(saved))
            arguments = create_file_classification_arguments(root, offline=False)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response) as model, redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 0)
            self.assertEqual(model.call_count, 2)
            classified = read_classified_file_rows(arguments.output)
            self.assertEqual([{column: row[column] for column in FILE_MATCH_COLUMNS} for row in classified], original_csv_rows)
            plan = load_evidence_plan(arguments.output)
            self.assertTrue(all(any(item["kind"] == "surrounding_code" for item in group["evidence"]) for group in plan["groups"]))
            self.context_loader.assert_not_called()
            self.context_tls.assert_not_called()

    def test_long_line_classification_is_bounded_and_retains_real_offsets(self) -> None:
        """Never send an entire megabyte line to classify one far-offset match."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_snapshot_inputs(root, "x" * 900_000 + " ATL03 " + "y" * 9000)
            arguments = create_file_classification_arguments(root, offline=False)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response) as model, redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 0)
            self.assertEqual(model.call_count, 1)
            request = model.call_args.args[0]
            self.assertLess(len(request["input"]), 24_000)
            packet = json.loads(request["input"])
            self.assertIn("ATL03", packet["evidence"][0]["text"])
            plan = load_evidence_plan(arguments.output)
            self.assertGreater(plan["groups"][0]["evidence"][0]["character_offset"], 899_000)
            self.assertLess(len(read_classified_file_rows(arguments.output)[0]["snippet"]), 12_000)

    def test_dry_run_has_no_writes_network_or_credential_reads(self) -> None:
        """Estimate locally from pinned context without producing paid side effects."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_snapshot_inputs(root)
            before = snapshot_test_files(root)
            arguments = create_file_classification_arguments(root, dry_run=True, offline=False)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification") as model, redirect_stderr(StringIO()) as output:
                self.assertEqual(classify_file_matches(arguments), 0)
            self.assertEqual(snapshot_test_files(root), before)
            self.assertIn("saved pinned snapshots", output.getvalue())
            self.key_loader.assert_not_called()
            model.assert_not_called()
            self.context_loader.assert_not_called()

    def test_resume_reuses_receipts_after_search_reports_are_regenerated(self) -> None:
        """Finish only pending model calls and ignore harmless summary timestamps."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory, _ = create_snapshot_inputs(root)
            arguments = create_file_classification_arguments(root, max_requests=1)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response) as model, redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 4)
                (directory / "summary.json").write_text('{"status":"complete","elapsed_seconds":42}', encoding="utf-8")
                arguments.max_requests = None
                self.assertEqual(classify_file_matches(arguments), 0)
                self.assertEqual(model.call_count, 2)
                self.key_loader.reset_mock()
                self.assertEqual(classify_file_matches(arguments), 0)
                self.assertEqual(model.call_count, 2)
                self.key_loader.assert_not_called()

    def test_changed_archive_is_rejected_before_key_reads_or_model_calls(self) -> None:
        """Protect frozen assessments from changed source bytes on resume."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory, _ = create_snapshot_inputs(root)
            arguments = create_file_classification_arguments(root)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response), redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 0)
            with (directory / "snapshots" / "snapshot.zip").open("ab") as archive:
                archive.write(b"changed")
            self.key_loader.reset_mock()
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification") as model, self.assertRaisesRegex(ValueError, "hash differs"):
                classify_file_matches(arguments)
            self.key_loader.assert_not_called()
            model.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""Check saved-match selection, paid-call scope, and restart safety offline."""

from contextlib import redirect_stderr
import csv
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.evidence_inputs import identify_evidence, read_product_evidence_groups
from nasa_eo_search.evidence_reports import load_evidence_plan
from nasa_eo_search.file_classify import classify_file_matches, split_repository_groups_into_files
from nasa_eo_search.file_selection import read_selected_match_ids
from nasa_eo_search.pipeline import main
from nasa_eo_search.pipeline_tables import FILE_MATCH_COLUMNS
from nasa_eo_search.snapshot_evidence import read_snapshot_evidence_groups
from test_evidence_classify import create_saved_evidence_inputs, make_model_response
from test_file_classify import create_file_classification_arguments, read_classified_file_rows, snapshot_test_files
from test_snapshot_evidence import create_snapshot_inputs


def create_selection_test_inputs(root: Path, backend: str) -> list[dict]:
    """Create two saved matches and return their original independently scoped rows.

    Args:
        root: Existing test-owned directory for the catalog and saved search.
        backend: GitHub snapshot or Sourcegraph saved-match fixture.

    Returns:
        The two original dataset/file rows, with stable match identifiers.
    """
    if backend == "github":
        batch_directory, _ = create_snapshot_inputs(root)
        source_plan = read_snapshot_evidence_groups(batch_directory)
    else:
        batch_directory, catalog_directory = create_saved_evidence_inputs(root)
        source_plan = read_product_evidence_groups(batch_directory, catalog_directory)
    return [group["match_row"] for group in split_repository_groups_into_files(source_plan)]


def write_match_selection(path: Path, match_ids: list[str]) -> None:
    """Write an editable selection CSV with a BOM and an ignored descriptive column.

    Args:
        path: Test-owned destination file.
        match_ids: Saved row identifiers to retain, including deliberate duplicates.
    """
    with path.open("w", encoding="utf-8-sig", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=["match_id", "snippet"])
        writer.writeheader()
        writer.writerows({"match_id": match_id, "snippet": "Edited text must not become model evidence"}
                         for match_id in match_ids)


class FileSelectionWorkflowTests(unittest.TestCase):
    """Select real saved evidence without broadening paid work or altering receipts."""

    def setUp(self) -> None:
        """Block external context and use a synthetic key for mocked classifications."""
        key_patch = patch("nasa_eo_search.evidence_classify.load_openai_api_key", return_value="synthetic-key")
        self.key_loader = key_patch.start()
        self.addCleanup(key_patch.stop)
        context_patch = patch("nasa_eo_search.evidence_context.read_bounded_https",
                              side_effect=AssertionError("Tests must not download context"))
        self.context_loader = context_patch.start()
        self.addCleanup(context_patch.stop)
        tls_patch = patch("nasa_eo_search.evidence_classify.build_tls_context")
        tls_patch.start()
        self.addCleanup(tls_patch.stop)

    def test_csv_selects_original_evidence_for_both_backends_and_ignores_extra_columns(self) -> None:
        """Copying or editing report columns selects IDs without replacing verified evidence."""
        for backend in ("github", "sourcegraph"):
            with self.subTest(backend=backend), TemporaryDirectory() as directory:
                root = Path(directory)
                original_rows = create_selection_test_inputs(root, backend)
                selected_row = next(row for row in original_rows if row["short_name"] == "ATL06")
                selection_path = root / "chosen.csv"
                write_match_selection(selection_path, [selected_row["match_id"], selected_row["match_id"]])
                original_source_files = snapshot_test_files(root / "batch")
                arguments = create_file_classification_arguments(root, selection=selection_path)
                with patch("nasa_eo_search.evidence_classify.request_evidence_classification",
                           side_effect=make_model_response) as model, redirect_stderr(StringIO()):
                    self.assertEqual(classify_file_matches(arguments), 0)
                self.assertEqual(model.call_count, 1)
                packet = json.loads(model.call_args.args[0]["input"])
                self.assertNotIn("Edited text must not become model evidence", json.dumps(packet))
                classified_rows = read_classified_file_rows(arguments.output)
                self.assertEqual(len(classified_rows), 1)
                self.assertEqual({field: classified_rows[0][field] for field in FILE_MATCH_COLUMNS}, selected_row)
                self.assertEqual(snapshot_test_files(root / "batch"), original_source_files)
                plan = load_evidence_plan(arguments.output)
                self.assertEqual(plan["file_selection"]["selected_match_ids"], [selected_row["match_id"]])
                self.assertEqual(plan["file_selection"]["available_matches"], 2)
                self.assertEqual(plan["file_selection"]["selected_matches"], 1)

    def test_repeated_short_names_and_csv_intersect_without_duplicate_classifications(self) -> None:
        """The public command applies both selectors and classifies each resulting ID once."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original_rows = create_selection_test_inputs(root, "github")
            selection_path = root / "chosen.csv"
            write_match_selection(selection_path, [row["match_id"] for row in original_rows])
            arguments = ["classify", str(root / "batch"), "--model", "test-model", "--output",
                         str(root / "classified"), "--short-name", "ATL06", "--short-name", "ATL06",
                         "--selection", str(selection_path)]
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification",
                       side_effect=make_model_response) as model, redirect_stderr(StringIO()):
                self.assertEqual(main(arguments), 0)
            self.assertEqual(model.call_count, 1)
            self.assertEqual([row["short_name"] for row in read_classified_file_rows(root / "classified")], ["ATL06"])
            plan = load_evidence_plan(root / "classified")
            self.assertEqual(plan["file_selection"]["short_names"], ["ATL06"])

    def test_copied_selection_accepts_large_ignored_snippets_and_preserves_original_evidence(self) -> None:
        """Long report snippets must not prevent selecting their saved match identifiers."""
        original_limit = csv.field_size_limit()
        self.addCleanup(csv.field_size_limit, original_limit)
        csv.field_size_limit(131_072)
        for backend in ("github", "sourcegraph"):
            with self.subTest(backend=backend), TemporaryDirectory() as directory:
                root = Path(directory)
                original_rows = create_selection_test_inputs(root, backend)
                selected_row = next(row for row in original_rows if row["short_name"] == "ATL06")
                selection_path = root / "chosen.csv"
                ignored_snippet = "edited-context-" * 10_000
                self.assertEqual(len(ignored_snippet), 150_000)
                with selection_path.open("w", encoding="utf-8-sig", newline="") as destination:
                    writer = csv.DictWriter(destination, fieldnames=["match_id", "snippet"])
                    writer.writeheader()
                    writer.writerow({"match_id": selected_row["match_id"], "snippet": ignored_snippet})
                arguments = create_file_classification_arguments(root, selection=selection_path)
                with patch("nasa_eo_search.evidence_classify.request_evidence_classification",
                           side_effect=make_model_response) as model, redirect_stderr(StringIO()):
                    self.assertEqual(classify_file_matches(arguments), 0)
                self.assertEqual(csv.field_size_limit(), 131_072)
                self.assertEqual(model.call_count, 1)
                self.assertNotIn("edited-context-", model.call_args.args[0]["input"])
                classified_rows = read_classified_file_rows(arguments.output)
                self.assertEqual([{field: row[field] for field in FILE_MATCH_COLUMNS} for row in classified_rows],
                                 [selected_row])

    def test_csv_parse_failure_restores_the_process_field_limit(self) -> None:
        """A malformed large selection must not change parsing limits for later commands."""
        original_limit = csv.field_size_limit()
        self.addCleanup(csv.field_size_limit, original_limit)
        csv.field_size_limit(131_072)
        with TemporaryDirectory() as directory:
            selection_path = Path(directory) / "broken.csv"
            selection_path.write_text('match_id,snippet\n' + "a" * 64 + ',"' + "x" * 140_000,
                                      encoding="utf-8")
            with self.assertRaisesRegex(csv.Error, "unexpected end of data"):
                read_selected_match_ids(selection_path)
            self.assertEqual(csv.field_size_limit(), 131_072)

    def test_multiple_dataset_names_select_their_union(self) -> None:
        """Repeating the dataset option includes each requested dataset's saved files."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            create_selection_test_inputs(root, "sourcegraph")
            arguments = create_file_classification_arguments(root, short_name=["ATL06", "ATL03", "ATL06"])
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification",
                       side_effect=make_model_response) as model, redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 0)
            self.assertEqual(model.call_count, 2)
            self.assertEqual({row["short_name"] for row in read_classified_file_rows(arguments.output)}, {"ATL03", "ATL06"})

    def test_invalid_selection_fails_before_context_keys_model_calls_or_writes(self) -> None:
        """Reject unknown identifiers and empty selections without starting paid work."""
        scenarios = ("unknown_name", "wrong_case", "unknown_id", "empty_csv", "blank_id",
                     "missing_id_column", "empty_intersection")
        for backend in ("github", "sourcegraph"):
            for scenario in scenarios:
                with self.subTest(backend=backend, scenario=scenario), TemporaryDirectory() as directory:
                    root = Path(directory)
                    original_rows = create_selection_test_inputs(root, backend)
                    selection_path = root / "chosen.csv"
                    options = {}
                    if scenario in ("unknown_name", "wrong_case"):
                        options["short_name"] = ["MISSING" if scenario == "unknown_name" else "atl03"]
                    else:
                        options["selection"] = selection_path
                        if scenario == "missing_id_column":
                            selection_path.write_text("short_name\nATL03\n", encoding="utf-8")
                        elif scenario == "empty_intersection":
                            match_id = next(row["match_id"] for row in original_rows if row["short_name"] == "ATL06")
                            write_match_selection(selection_path, [match_id])
                            options["short_name"] = ["ATL03"]
                        else:
                            match_ids = {"unknown_id": ["f" * 64], "empty_csv": [], "blank_id": [""]}[scenario]
                            write_match_selection(selection_path, match_ids)
                    arguments = create_file_classification_arguments(root, offline=False, **options)
                    before = snapshot_test_files(root)
                    self.key_loader.reset_mock()
                    with patch("nasa_eo_search.file_classify.build_file_classification_plan") as context, patch(
                        "nasa_eo_search.evidence_classify.request_evidence_classification",
                    ) as model, self.assertRaises(ValueError):
                        classify_file_matches(arguments)
                    context.assert_not_called()
                    model.assert_not_called()
                    self.key_loader.assert_not_called()
                    self.assertEqual(snapshot_test_files(root), before)

    def test_selected_dry_run_counts_only_selected_files_without_side_effects(self) -> None:
        """Preview the selected request scope while keeping all saved sources unchanged."""
        for backend in ("github", "sourcegraph"):
            with self.subTest(backend=backend), TemporaryDirectory() as directory:
                root = Path(directory)
                create_selection_test_inputs(root, backend)
                arguments = create_file_classification_arguments(root, short_name=["ATL03"], dry_run=True,
                                                                 offline=False)
                before = snapshot_test_files(root)
                self.key_loader.reset_mock()
                with patch("nasa_eo_search.evidence_classify.request_evidence_classification") as model, redirect_stderr(StringIO()) as terminal:
                    self.assertEqual(classify_file_matches(arguments), 0)
                self.assertIn("1 file matches", terminal.getvalue())
                self.assertIn("2", terminal.getvalue())
                model.assert_not_called()
                self.key_loader.assert_not_called()
                self.context_loader.assert_not_called()
                self.assertEqual(snapshot_test_files(root), before)

    def test_omitting_selectors_resumes_frozen_selection_after_csv_is_removed(self) -> None:
        """Finish pending calls from the saved selection without requiring its input CSV."""
        for backend in ("github", "sourcegraph"):
            with self.subTest(backend=backend), TemporaryDirectory() as directory:
                root = Path(directory)
                original_rows = create_selection_test_inputs(root, backend)
                selection_path = root / "chosen.csv"
                write_match_selection(selection_path, [row["match_id"] for row in original_rows])
                arguments = create_file_classification_arguments(root, selection=selection_path, max_requests=1)
                with patch("nasa_eo_search.evidence_classify.request_evidence_classification",
                           side_effect=make_model_response) as model, redirect_stderr(StringIO()):
                    self.assertEqual(classify_file_matches(arguments), 4)
                    self.assertEqual(model.call_count, 1)
                    selection_path.unlink()
                    arguments = create_file_classification_arguments(root)
                    self.assertEqual(classify_file_matches(arguments), 0)
                    self.assertEqual(model.call_count, 2)
                    self.key_loader.reset_mock()
                    self.assertEqual(classify_file_matches(arguments), 0)
                    self.assertEqual(model.call_count, 2)
                    self.key_loader.assert_not_called()
                self.assertEqual(len(read_classified_file_rows(arguments.output)), 2)

    def test_reordered_csv_and_duplicate_ids_reuse_successful_responses(self) -> None:
        """Selection identity depends on its identifiers rather than CSV order or path."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original_rows = create_selection_test_inputs(root, "github")
            selected_ids = [row["match_id"] for row in original_rows]
            selection_path = root / "chosen.csv"
            write_match_selection(selection_path, selected_ids)
            arguments = create_file_classification_arguments(root, selection=selection_path)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification",
                       side_effect=make_model_response) as model, redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 0)
                arguments.selection = root / "reordered.csv"
                write_match_selection(arguments.selection, [*reversed(selected_ids), selected_ids[0]])
                self.key_loader.reset_mock()
                self.assertEqual(classify_file_matches(arguments), 0)
                self.assertEqual(model.call_count, 2)
                self.key_loader.assert_not_called()

    def test_changed_explicit_selection_rejects_resume_and_dry_run_without_mutation(self) -> None:
        """A saved output cannot silently gain or replace assessed files on another run."""
        for backend in ("github", "sourcegraph"):
            with self.subTest(backend=backend), TemporaryDirectory() as directory:
                root = Path(directory)
                create_selection_test_inputs(root, backend)
                arguments = create_file_classification_arguments(root, short_name=["ATL03"])
                with patch("nasa_eo_search.evidence_classify.request_evidence_classification",
                           side_effect=make_model_response), redirect_stderr(StringIO()):
                    self.assertEqual(classify_file_matches(arguments), 0)
                before = snapshot_test_files(root)
                self.key_loader.reset_mock()
                for dry_run in (False, True):
                    arguments.short_name = ["ATL06"]
                    arguments.dry_run = dry_run
                    with patch("nasa_eo_search.evidence_classify.request_evidence_classification") as model, self.assertRaisesRegex(ValueError, "selection|Selection"):
                        classify_file_matches(arguments)
                    model.assert_not_called()
                    self.key_loader.assert_not_called()
                    self.assertEqual(snapshot_test_files(root), before)

    def test_legacy_unfiltered_plan_resumes_but_rejects_new_narrowing(self) -> None:
        """Existing full runs keep their saved scope when new selector options are introduced."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            create_selection_test_inputs(root, "github")
            arguments = create_file_classification_arguments(root)
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification",
                       side_effect=make_model_response) as model, redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 0)
                plan_path = arguments.output / "plan.json"
                envelope = json.loads(plan_path.read_text(encoding="utf-8"))
                envelope["data"].pop("file_selection", None)
                envelope["plan_id"] = identify_evidence(envelope["data"])
                plan_path.write_text(json.dumps(envelope), encoding="utf-8")
                self.key_loader.reset_mock()
                self.assertEqual(classify_file_matches(arguments), 0)
                self.assertEqual(model.call_count, 2)
                self.key_loader.assert_not_called()
            before = snapshot_test_files(root)
            arguments.short_name = ["ATL03"]
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification") as model, self.assertRaises(ValueError):
                classify_file_matches(arguments)
            model.assert_not_called()
            self.assertEqual(snapshot_test_files(root), before)

    def test_subset_resume_still_checks_changes_to_unselected_source_metadata(self) -> None:
        """Selecting one dataset does not weaken the full saved-source identity check."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            create_selection_test_inputs(root, "sourcegraph")
            arguments = create_file_classification_arguments(root, short_name=["ATL03"])
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification",
                       side_effect=make_model_response), redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 0)
            catalog_path = root / "catalog" / "products.jsonl"
            products = [json.loads(line) for line in catalog_path.read_text(encoding="utf-8").splitlines()]
            unselected_product = next(product for product in products if product["short_name"] == "ATL06")
            unselected_product["collections"][0]["title"] = "Changed unselected product"
            catalog_path.write_text("\n".join(json.dumps(product) for product in products), encoding="utf-8")
            before = snapshot_test_files(root)
            self.key_loader.reset_mock()
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification") as model, self.assertRaisesRegex(ValueError, "differs"):
                classify_file_matches(arguments)
            model.assert_not_called()
            self.key_loader.assert_not_called()
            self.assertEqual(snapshot_test_files(root), before)

    def test_rehashed_selection_metadata_cannot_redirect_saved_paid_scope(self) -> None:
        """Reject disagreement between saved selectors and their prepared evidence groups."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original_rows = create_selection_test_inputs(root, "github")
            arguments = create_file_classification_arguments(root, short_name=["ATL03"])
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification",
                       side_effect=make_model_response), redirect_stderr(StringIO()):
                self.assertEqual(classify_file_matches(arguments), 0)
            plan_path = arguments.output / "plan.json"
            envelope = json.loads(plan_path.read_text(encoding="utf-8"))
            replacement_id = next(row["match_id"] for row in original_rows if row["short_name"] == "ATL06")
            envelope["data"]["file_selection"]["short_names"] = ["ATL06"]
            envelope["data"]["file_selection"]["selected_match_ids"] = [replacement_id]
            envelope["plan_id"] = identify_evidence(envelope["data"])
            plan_path.write_text(json.dumps(envelope), encoding="utf-8")
            before = snapshot_test_files(root)
            arguments.short_name = None
            self.key_loader.reset_mock()
            with patch("nasa_eo_search.evidence_classify.request_evidence_classification") as model, self.assertRaisesRegex(ValueError, "selection"):
                classify_file_matches(arguments)
            model.assert_not_called()
            self.key_loader.assert_not_called()
            self.assertEqual(snapshot_test_files(root), before)


if __name__ == "__main__":
    unittest.main()

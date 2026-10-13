"""Exercise offline human reviews against real preparation and fake model receipts."""

from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.batch_reports import write_protected_csv_rows
from nasa_eo_search.evidence_classify import main as classify_main
from nasa_eo_search.evidence_inputs import identify_evidence, read_evidence_json
from nasa_eo_search.evidence_review import main, load_human_review
from nasa_eo_search.review_files import (
    KNOWN_EXAMPLE_COLUMNS, LABEL_COLUMNS, SELECTION_COLUMNS, identify_review_evidence,
    read_classification_snapshot, read_review_csv, read_review_snapshot, write_blind_review_page,
)
from nasa_eo_search.review_selection import build_review_selection
from test_evidence_classify import make_model_response, prepare_test_evidence


def snapshot_test_files(directory: Path) -> dict[str, bytes]:
    """Capture test-owned file bytes for assertions that source inputs never change.

    Args:
        directory: Temporary test directory containing no credentials.

    Returns:
        Relative paths mapped to their original byte content.
    """
    return {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


def create_completed_test_run(root: Path) -> Path:
    """Prepare and finish two classifications using a fake network response only.

    Args:
        root: Empty test-owned temporary directory.

    Returns:
        Completed format-2 classification directory with deterministic receipts.
    """
    source = prepare_test_evidence(root)
    with redirect_stderr(StringIO()), patch("nasa_eo_search.evidence_classify.load_openai_api_key", return_value="fake"), \
            patch("nasa_eo_search.evidence_classify.build_tls_context"), \
            patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response):
        result = classify_main(["run", str(source), "--model", "test-model", "--max-requests", "20"])
    if result != 0:
        raise AssertionError("Fake classification run failed.")
    return source


def label_test_review(directory: Path, category: str = "supporting_software") -> None:
    """Fill the test label sheet with a cited definitive human judgment.

    Args:
        directory: Test-owned human-review directory.
        category: Category to place on every row.
    """
    rows = read_review_csv(directory / "human-labels.csv", LABEL_COLUMNS)
    for row in rows:
        row.update(human_category=category, supporting_citation="E1", evidence_sufficiency="sufficient", review_minutes="2")
    write_protected_csv_rows(directory / "human-labels.csv", LABEL_COLUMNS, rows)


class HumanEvidenceReviewTests(unittest.TestCase):
    """Verify label persistence, blind exports, staleness, and repeatable metrics."""

    def test_prepare_and_compare_are_offline_blind_and_source_preserving(self) -> None:
        """Freeze baseline receipts and evaluate labels without touching source files."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = create_completed_test_run(root)
            before = snapshot_test_files(source)
            review = root / "human"
            with redirect_stderr(StringIO()), patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")):
                self.assertEqual(main(["prepare", str(source), "--output", str(review)]), 0)
                label_test_review(review)
                labels_before = (review / "human-labels.csv").read_bytes()
                self.assertEqual(main(["compare", str(review), "--output", str(root / "comparison")]), 0)
            self.assertEqual(snapshot_test_files(source), before)
            self.assertEqual((review / "human-labels.csv").read_bytes(), labels_before)
            html = (review / "evidence.html").read_text(encoding="utf-8")
            self.assertIn('load("ATL03")'.replace('"', '&quot;'), html)
            self.assertNotIn("Tests demonstrate product support.", html)
            self.assertNotIn("needs_review", html)
            self.assertIn("#L2", html)
            metrics = read_evidence_json(root / "comparison" / "metrics.json")
            self.assertEqual(metrics["metrics"]["category_agreement"]["numerator"], 2)
            self.assertEqual(metrics["metrics"]["category_agreement"]["denominator"], 2)
            self.assertEqual(metrics["usage"]["input_tokens"], 200)
            self.assertEqual(metrics["human_review_time"]["recorded_minutes"], 4)
            self.assertTrue((root / "comparison" / "metrics.html").is_file())
            snapshot = read_review_snapshot(review / "machine-snapshot.json")
            self.assertEqual(len(snapshot["groups"][0]["saved_attempts"]), 1)
            self.assertIn("response", snapshot["groups"][0]["saved_attempts"][0])

    def test_unchanged_evidence_across_runs_keeps_labels_current(self) -> None:
        """Exclude run timestamps, paths, and model responses from label freshness."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = create_completed_test_run(root)
            review = root / "human"
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["prepare", str(source), "--output", str(review)]), 0)
            label_test_review(review)
            envelope = read_evidence_json(source / "plan.json")
            envelope["data"]["created_at"] = "2099-01-01"
            envelope["plan_id"] = identify_evidence(envelope["data"])
            (source / "plan.json").write_text(json.dumps(envelope), encoding="utf-8")
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["compare", str(review), "--classifications", str(source),
                                       "--output", str(root / "comparison")]), 0)
            metrics = read_evidence_json(root / "comparison" / "metrics.json")
            self.assertEqual(metrics["reviewed_groups"], 2)
            self.assertEqual(metrics["groups_by_review_status"]["stale_snapshot"], 0)

    def test_changed_evidence_is_excluded_and_baseline_remains_reusable(self) -> None:
        """Make a changed packet stale while retaining the original pinned assessment."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = create_completed_test_run(root)
            review = root / "human"
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["prepare", str(source), "--output", str(review)]), 0)
            label_test_review(review)
            envelope = read_evidence_json(source / "plan.json")
            envelope["data"]["groups"][0]["packets"][0]["product"]["description"] = "Additional decisive context"
            envelope["plan_id"] = identify_evidence(envelope["data"])
            (source / "plan.json").write_text(json.dumps(envelope), encoding="utf-8")
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["compare", str(review), "--classifications", str(source),
                                       "--output", str(root / "changed")]), 0)
                self.assertEqual(main(["compare", str(review), "--output", str(root / "baseline")]), 0)
            changed = read_evidence_json(root / "changed" / "metrics.json")
            baseline = read_evidence_json(root / "baseline" / "metrics.json")
            self.assertEqual(changed["groups_by_review_status"]["stale_snapshot"], 1)
            self.assertEqual(changed["category_comparison_groups"], 1)
            self.assertEqual(baseline["category_comparison_groups"], 2)

    def test_overwrite_and_invalid_labels_fail_before_output(self) -> None:
        """Never replace a review directory or accept unsupported human judgments."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = create_completed_test_run(root)
            review = root / "human"
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["prepare", str(source), "--output", str(review)]), 0)
            original = snapshot_test_files(review)
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["prepare", str(source), "--output", str(review)]), 1)
            self.assertEqual(snapshot_test_files(review), original)
            label_test_review(review)
            rows = read_review_csv(review / "human-labels.csv", LABEL_COLUMNS)
            for field, value in (("human_category", "not_a_category"), ("supporting_citation", "E999"),
                                 ("review_minutes", "NaN"), ("evidence_sufficiency", "insufficient")):
                with self.subTest(field=field):
                    modified = [{**row} for row in rows]
                    modified[0][field] = value
                    write_protected_csv_rows(review / "human-labels.csv", LABEL_COLUMNS, modified)
                    output = root / field
                    with redirect_stderr(StringIO()):
                        self.assertEqual(main(["compare", str(review), "--output", str(output)]), 1)
                    self.assertFalse(output.exists())

    def test_pending_blank_and_unclear_remain_distinct(self) -> None:
        """Keep unfinished processing and absent human judgments outside accuracy counts."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = prepare_test_evidence(root)
            review = root / "human"
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["prepare", str(source), "--output", str(review)]), 0)
                self.assertEqual(main(["compare", str(review), "--output", str(root / "blank")]), 0)
            blank = read_evidence_json(root / "blank" / "metrics.json")
            self.assertEqual(blank["groups_by_review_status"]["unreviewed"], 2)
            self.assertIsNone(blank["metrics"]["category_agreement"]["value"])
            rows = read_review_csv(review / "human-labels.csv", LABEL_COLUMNS)
            rows[0].update(human_category="unclear", evidence_sufficiency="insufficient", missing_context_reason="Need the caller")
            write_protected_csv_rows(review / "human-labels.csv", LABEL_COLUMNS, rows)
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["compare", str(review), "--output", str(root / "partial")]), 0)
            partial = read_evidence_json(root / "partial" / "metrics.json")
            self.assertEqual(partial["classification_exclusions"]["machine_incomplete"], 1)
            self.assertEqual(partial["metrics"]["evidence_insufficiency"]["numerator"], 1)

    def test_snapshot_tampering_is_rejected(self) -> None:
        """Fail visibly if a frozen evidence artifact has been manually changed."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = prepare_test_evidence(root)
            review = root / "human"
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["prepare", str(source), "--output", str(review)]), 0)
            evidence = read_evidence_json(review / "evidence.json")
            evidence["data"]["created_at"] = "edited"
            (review / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Changed or invalid"):
                load_human_review(review)

    def test_html_escapes_untrusted_repository_content(self) -> None:
        """Render repository text as escaped code rather than executable page content."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = prepare_test_evidence(root)
            plan, snapshot = read_classification_snapshot(source)
            entries = build_review_selection(snapshot["groups"])
            plan["groups"][0]["evidence_blocks"][0]["text"] = '<script>alert("unsafe")</script>'
            plan["groups"][0]["evidence_blocks"][0].update(representation="notebook_cell", cell_type="markdown", cell_index=2)
            write_blind_review_page(root / "review.html", plan, entries)
            html = (root / "review.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>", html)
            self.assertIn("&lt;script&gt;", html)
            self.assertIn("cell_type: markdown", html)
            self.assertIn("representation: notebook_cell", html)

    def test_revision_tracks_rubric_inputs_not_only_repository_identity(self) -> None:
        """Detect missing context, changed scope, and changed block/source citations."""
        with TemporaryDirectory() as temporary:
            source = prepare_test_evidence(Path(temporary))
            plan, _ = read_classification_snapshot(source)
            group = plan["groups"][0]
            baseline = identify_review_evidence(group, plan)
            group["warnings"].append("More context unavailable")
            self.assertNotEqual(identify_review_evidence(group, plan), baseline)

    def test_custom_selection_freezes_membership_and_remains_comparable(self) -> None:
        """Freeze an edited worksheet without allowing it to change earlier reviews."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = create_completed_test_run(root)
            initial = root / "initial"
            selected = root / "selected"
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["prepare", str(source), "--output", str(initial)]), 0)
            selection = read_review_csv(initial / "selection.csv", SELECTION_COLUMNS)[:1]
            selection[0].update(split="test", sample_kind="challenge", family="example-family")
            write_protected_csv_rows(initial / "selection.csv", SELECTION_COLUMNS, selection)
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["prepare", str(source), "--selection", str(initial / "selection.csv"),
                                       "--output", str(selected)]), 0)
                self.assertEqual(main(["compare", str(selected), "--output", str(root / "comparison")]), 0)
            initial_manifest, _, _ = load_human_review(initial)
            selected_manifest, _, _ = load_human_review(selected)
            self.assertEqual(len(initial_manifest["entries"]), 2)
            self.assertEqual(len(selected_manifest["entries"]), 1)
            self.assertEqual(selected_manifest["entries"][0]["split"], "test")

    def test_retrieval_cli_preserves_sources_and_measures_independent_examples(self) -> None:
        """Keep missing independent examples distinct from unsearched products."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = prepare_test_evidence(root)
            before = snapshot_test_files(source)
            examples = [{"repository": repository, "provider": "NSIDC", "short_name": product,
                         "source_url": "https://example.org/documentation", "discovery_basis": "domain_knowledge",
                         "miss_reason": "", "notes": "Test-only example, not a real domain claim"}
                        for repository, product in (("github.com/example/project", "ATL03"),
                                                    ("github.com/example/missing", "ATL03"),
                                                    ("github.com/example/unsearched", "ATL99"))]
            write_protected_csv_rows(root / "known.csv", KNOWN_EXAMPLE_COLUMNS, examples)
            output = root / "retrieval"
            with redirect_stderr(StringIO()), patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")):
                self.assertEqual(main(["retrieval", str(source), "--known-examples", str(root / "known.csv"),
                                       "--output", str(output)]), 0)
            self.assertEqual(snapshot_test_files(source), before)
            summary = read_evidence_json(output / "retrieval-summary.json")
            self.assertEqual(summary["known_examples"], 3)
            self.assertEqual(summary["found"], 1)
            self.assertEqual(summary["status_counts"]["not_searched"], 1)

    def test_machine_report_refresh_does_not_erase_human_judgments(self) -> None:
        """Regenerate original classifier reports and recompare unchanged human labels."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = create_completed_test_run(root)
            review = root / "human"
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["prepare", str(source), "--output", str(review)]), 0)
            label_test_review(review)
            before = snapshot_test_files(review)
            with redirect_stderr(StringIO()), patch("nasa_eo_search.evidence_classify.request_evidence_classification") as network:
                self.assertEqual(classify_main(["run", str(source), "--model", "test-model"]), 0)
                self.assertEqual(main(["compare", str(review), "--output", str(root / "comparison")]), 0)
            network.assert_not_called()
            self.assertEqual(snapshot_test_files(review), before)


if __name__ == "__main__":
    unittest.main()

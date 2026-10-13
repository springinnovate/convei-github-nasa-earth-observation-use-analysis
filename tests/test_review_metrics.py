"""Check offline sample comparison denominators and evidence revision safety."""

import copy
from html import escape
import unittest

from nasa_eo_search.review_metrics import compare_review_labels, read_review_minutes, render_review_metrics


def create_review_inputs(category_pairs: list[tuple[str, str]]) -> tuple[dict, list[dict], list[dict]]:
    """Build current frozen selections and human labels for metric assertions.

    Args:
        category_pairs: Machine and human categories for completed example groups.

    Returns:
        Current snapshot, selected manifest entries, and independent human labels.
    """
    groups = []
    manifest_entries = []
    labels = []
    for number, (machine_category, human_category) in enumerate(category_pairs, start=1):
        group_id = f"group-{number}"
        evidence_revision = f"revision-{number}"
        groups.append({
            "group_id": group_id, "evidence_revision": evidence_revision,
            "repository": f"github.com/example/repository-{number}", "provider": "NSIDC", "short_name": "ATL03",
            "status": "complete", "category": machine_category, "needs_review": machine_category == "unclear",
            "request_count": 2, "completed_requests": 2, "attempt_count": 3,
            "input_tokens": 150, "output_tokens": 10, "attempts_without_usage": 0,
        })
        manifest_entries.append({
            "group_id": group_id, "evidence_revision": evidence_revision,
            "split": "test", "sample_kind": "ordinary", "family": f"family-{number}",
        })
        labels.append({
            "group_id": group_id, "evidence_revision": evidence_revision, "human_category": human_category,
            "supporting_citation": "E1", "evidence_sufficiency": "sufficient",
            "missing_context_reason": "", "notes": "Human review.", "review_minutes": "2.5",
        })
    return {"groups": groups}, manifest_entries, labels


class ReviewClassificationMetricsTests(unittest.TestCase):
    """Distinguish wrong definitive labels, unsupported claims, and abstentions."""

    def test_category_disagreements_and_use_support_errors_have_explicit_denominators(self) -> None:
        """Avoid treating support/application distinctions as unsupported NASA-use claims."""
        snapshot, selections, labels = create_review_inputs([
            ("direct_use", "direct_use"),
            ("supporting_software", "direct_use"),
            ("direct_use", "reference_only"),
            ("supporting_software", "unrelated"),
            ("reference_only", "direct_use"),
            ("unrelated", "supporting_software"),
            ("unclear", "direct_use"),
            ("reference_only", "reference_only"),
            ("direct_use", "unclear"),
        ])
        result = compare_review_labels(snapshot, selections, labels)
        metrics = result["summary"]["metrics"]
        expected_counts = {
            "category_agreement": (2, 7), "category_disagreement": (5, 7),
            "false_use_or_support": (2, 4), "missed_supported_findings": (2, 5),
            "supported_finding_abstentions": (1, 5), "model_abstentions": (1, 9), "human_unclear": (1, 9),
        }
        for name, (numerator, denominator) in expected_counts.items():
            with self.subTest(metric=name):
                self.assertEqual(metrics[name]["numerator"], numerator)
                self.assertEqual(metrics[name]["denominator"], denominator)
                self.assertAlmostEqual(metrics[name]["value"], numerator / denominator)
                self.assertTrue(metrics[name]["definition"])
        self.assertTrue(result["rows"][1]["category_disagreement"])
        self.assertFalse(result["rows"][1]["false_use_or_support"])
        self.assertIsNone(result["rows"][6]["category_disagreement"])
        self.assertFalse(result["rows"][6]["missed_supported_finding"])
        self.assertTrue(result["rows"][6]["supported_finding_abstention"])
        self.assertEqual(result["summary"]["classification_exclusions"]["model_abstention"], 1)
        self.assertEqual(result["summary"]["classification_exclusions"]["human_unclear"], 1)

    def test_processing_failures_are_excluded_even_with_a_human_category(self) -> None:
        """Exclude pending, failed, and blocked assessments from classification rates."""
        snapshot, selections, labels = create_review_inputs([("unrelated", "direct_use")] * 3)
        for group, status in zip(snapshot["groups"], ("pending", "failed", "blocked")):
            group["status"] = status
            group["completed_requests"] = 0
        result = compare_review_labels(snapshot, selections, labels)
        self.assertEqual(result["summary"]["reviewed_groups"], 3)
        self.assertEqual(result["summary"]["paired_complete_groups"], 0)
        self.assertEqual(result["summary"]["classification_exclusions"]["machine_incomplete"], 3)
        self.assertEqual(result["summary"]["metrics"]["missed_supported_findings"]["denominator"], 0)
        self.assertIsNone(result["summary"]["metrics"]["missed_supported_findings"]["value"])
        self.assertTrue(all(row["missed_supported_finding"] is None for row in result["rows"]))

    def test_empty_sample_uses_null_rates_instead_of_zero_accuracy(self) -> None:
        """Represent unavailable proportions explicitly across all empty strata."""
        result = compare_review_labels({"groups": []}, [], [])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["summary"]["selected_groups"], 0)
        for metric in result["summary"]["metrics"].values():
            self.assertEqual(metric["numerator"], 0)
            self.assertEqual(metric["denominator"], 0)
            self.assertIsNone(metric["value"])
        self.assertIsNone(result["summary"]["human_review_time"]["mean_recorded_minutes"])
        self.assertIsNone(result["summary"]["by_split"]["test"]["metrics"]["category_agreement"]["value"])

    def test_split_and_sample_kind_summaries_do_not_mix_challenges_with_ordinary_examples(self) -> None:
        """Retain ordinary/challenge counts separately within each reserved split."""
        snapshot, selections, labels = create_review_inputs([
            ("direct_use", "direct_use"), ("direct_use", "unrelated"),
            ("reference_only", "reference_only"), ("supporting_software", "supporting_software"),
        ])
        selections[0]["split"] = "development"
        selections[1]["sample_kind"] = "challenge"
        selections[3]["split"] = "regression"
        result = compare_review_labels(snapshot, selections, labels)
        summary = result["summary"]
        self.assertEqual(summary["by_split"]["test"]["selected_groups"], 2)
        self.assertEqual(summary["by_sample_kind"]["ordinary"]["selected_groups"], 3)
        self.assertEqual(summary["by_split_and_sample_kind"]["test"]["ordinary"]["metrics"]["category_agreement"]["value"], 1)
        self.assertEqual(summary["by_split_and_sample_kind"]["test"]["challenge"]["metrics"]["category_disagreement"]["value"], 1)
        self.assertEqual(summary["by_split_and_sample_kind"]["development"]["challenge"]["selected_groups"], 0)
        self.assertIn("not population accuracy or GitHub recall", summary["scope"])
        self.assertTrue(any("convenience samples" in limitation for limitation in summary["limitations"]))


class ReviewRevisionSafetyTests(unittest.TestCase):
    """Never silently apply a human judgment to different repository evidence."""

    def test_old_label_revision_is_flagged_and_never_applied(self) -> None:
        """Show an obsolete label as stale while leaving the current category unreviewed."""
        snapshot, selections, labels = create_review_inputs([("direct_use", "unrelated")])
        labels[0]["evidence_revision"] = "older-revision"
        result = compare_review_labels(snapshot, selections, labels)
        row = result["rows"][0]
        self.assertEqual(row["review_status"], "stale_label")
        self.assertEqual(row["human_category"], "")
        self.assertEqual(row["other_label_revisions"], ["older-revision"])
        self.assertIsNone(row["false_use_or_support"])
        self.assertEqual(result["summary"]["unused_label_rows"], 1)
        self.assertEqual(result["summary"]["classification_exclusions"]["stale_label"], 1)

    def test_matching_manifest_label_does_not_override_a_newer_snapshot(self) -> None:
        """Require exact revision agreement among selection, label, and snapshot."""
        snapshot, selections, labels = create_review_inputs([("direct_use", "unrelated")])
        snapshot["groups"][0]["evidence_revision"] = "newer-revision"
        result = compare_review_labels(snapshot, selections, labels)
        row = result["rows"][0]
        self.assertEqual(row["review_status"], "stale_snapshot")
        self.assertEqual(row["snapshot_evidence_revision"], "newer-revision")
        self.assertEqual(row["human_category"], "")
        self.assertEqual(row["notes"], "")
        self.assertIsNone(row["input_tokens"])
        self.assertEqual(result["summary"]["usage"]["groups_excluded_stale_or_missing"], 1)
        self.assertEqual(result["summary"]["usage"]["input_tokens"], 0)
        self.assertEqual(result["summary"]["unused_label_rows"], 1)

    def test_missing_group_blank_label_and_absent_label_are_explicit(self) -> None:
        """Keep missing snapshots distinct from selected but unreviewed examples."""
        snapshot, selections, labels = create_review_inputs([("direct_use", "direct_use")] * 3)
        snapshot["groups"].pop(0)
        labels[1]["human_category"] = ""
        labels.pop()
        result = compare_review_labels(snapshot, selections, labels)
        self.assertEqual([row["review_status"] for row in result["rows"]], ["missing_snapshot", "unreviewed", "unreviewed"])
        self.assertEqual(result["summary"]["groups_by_review_status"]["missing_snapshot"], 1)
        self.assertEqual(result["summary"]["groups_by_review_status"]["unreviewed"], 2)
        self.assertEqual(result["summary"]["category_comparison_groups"], 0)

    def test_missing_or_changed_group_retains_the_selected_repository_identity(self) -> None:
        """Keep stale and missing comparison rows identifiable from the sample manifest."""
        snapshot, selections, labels = create_review_inputs([("direct_use", "direct_use")] * 2)
        for group, selection in zip(snapshot["groups"], selections):
            selection.update({field: group[field] for field in ("repository", "provider", "short_name")})
        snapshot["groups"][0]["repository"] = "github.com/changed/name"
        snapshot["groups"][0]["evidence_revision"] = "different-evidence"
        snapshot["groups"].pop(1)
        result = compare_review_labels(snapshot, selections, labels)
        self.assertEqual(result["rows"][0]["repository"], selections[0]["repository"])
        for field in ("repository", "provider", "short_name"):
            self.assertEqual(result["rows"][1][field], selections[1][field])

    def test_distinct_older_revisions_can_coexist_but_duplicate_matching_labels_fail(self) -> None:
        """Allow label history without silently choosing among duplicate current judgments."""
        snapshot, selections, labels = create_review_inputs([("direct_use", "direct_use")])
        labels.extend([
            {**labels[0], "evidence_revision": "older-one", "human_category": "unrelated"},
            {**labels[0], "evidence_revision": "older-two", "human_category": "reference_only"},
        ])
        result = compare_review_labels(snapshot, selections, labels)
        self.assertEqual(result["rows"][0]["review_status"], "reviewed")
        self.assertEqual(result["rows"][0]["human_category"], "direct_use")
        self.assertEqual(result["summary"]["unused_label_rows"], 2)
        labels.append(dict(labels[0]))
        with self.assertRaisesRegex(ValueError, "duplicate matching"):
            compare_review_labels(snapshot, selections, labels)

    def test_duplicate_snapshot_or_manifest_selection_is_rejected(self) -> None:
        """Prevent repeated groups from inflating sample or usage counts."""
        snapshot, selections, labels = create_review_inputs([("direct_use", "direct_use")])
        with self.assertRaisesRegex(ValueError, "duplicate group IDs"):
            compare_review_labels({"groups": snapshot["groups"] * 2}, selections, labels)
        with self.assertRaisesRegex(ValueError, "duplicate group/revision"):
            compare_review_labels(snapshot, selections * 2, labels)

    def test_comparison_is_pure_and_does_not_change_labels_or_source_data(self) -> None:
        """Leave original selected evidence and human annotations untouched."""
        inputs = create_review_inputs([("direct_use", "direct_use"), ("unclear", "unclear")])
        before = copy.deepcopy(inputs)
        compare_review_labels(*inputs)
        self.assertEqual(inputs, before)


class ReviewCostAndEvidenceMetricsTests(unittest.TestCase):
    """Measure recorded work separately from category agreement and evidence quality."""

    def test_usage_includes_attempts_and_flags_missing_token_records(self) -> None:
        """Count recorded attempts and tokens without pretending unknown usage is free."""
        snapshot, selections, labels = create_review_inputs([("direct_use", "direct_use")] * 2)
        snapshot["groups"][1]["status"] = "pending"
        snapshot["groups"][1]["completed_requests"] = 1
        snapshot["groups"][1]["attempts_without_usage"] = 1
        result = compare_review_labels(snapshot, selections, labels)
        usage = result["summary"]["usage"]
        self.assertEqual(usage["request_count"], 4)
        self.assertEqual(usage["completed_requests"], 3)
        self.assertEqual(usage["attempt_count"], 6)
        self.assertEqual(usage["input_tokens"], 300)
        self.assertEqual(usage["output_tokens"], 20)
        self.assertEqual(usage["attempts_without_usage"], 1)
        self.assertFalse(usage["token_usage_complete_for_recorded_attempts"])

    def test_workload_and_evidence_gaps_are_separate_from_category_errors(self) -> None:
        """Report model flags and human collection concerns even when labels agree."""
        snapshot, selections, labels = create_review_inputs([("direct_use", "direct_use")] * 3)
        snapshot["groups"][0]["needs_review"] = True
        labels[0]["evidence_sufficiency"] = "insufficient"
        labels[0]["missing_context_reason"] = "The implementation was not collected."
        labels[1]["evidence_sufficiency"] = "uncertain"
        labels[2]["evidence_sufficiency"] = ""
        result = compare_review_labels(snapshot, selections, labels)
        metrics = result["summary"]["metrics"]
        self.assertEqual(metrics["category_disagreement"]["numerator"], 0)
        self.assertEqual(metrics["needs_review_workload"]["numerator"], 1)
        self.assertEqual(metrics["needs_review_workload"]["denominator"], 3)
        self.assertEqual(metrics["evidence_insufficiency"]["value"], 0.5)
        self.assertEqual(metrics["evidence_uncertainty"]["value"], 0.5)
        self.assertEqual(metrics["missing_context"]["numerator"], 1)
        self.assertEqual(metrics["missing_context"]["denominator"], 3)

    def test_blank_review_time_is_not_zero_and_zero_is_a_recorded_value(self) -> None:
        """Compute elapsed review work only from explicitly supplied durations."""
        snapshot, selections, labels = create_review_inputs([("direct_use", "direct_use")] * 3)
        labels[0]["review_minutes"] = ""
        labels[1]["review_minutes"] = "0"
        labels[2]["review_minutes"] = "4.5"
        result = compare_review_labels(snapshot, selections, labels)
        time = result["summary"]["human_review_time"]
        self.assertEqual(time["recorded_minutes"], 4.5)
        self.assertEqual(time["groups_with_recorded_minutes"], 2)
        self.assertEqual(time["reviewed_groups_without_recorded_minutes"], 1)
        self.assertEqual(time["mean_recorded_minutes"], 2.25)

    def test_invalid_time_counts_and_boolean_flags_fail_without_coercion(self) -> None:
        """Reject nonfinite durations and ambiguous strings masquerading as counts."""
        for invalid_minutes in ("nan", "inf", "-1", True, "not a number"):
            with self.subTest(minutes=invalid_minutes), self.assertRaises(ValueError):
                read_review_minutes({"review_minutes": invalid_minutes})
        for field, value in (("input_tokens", -1), ("attempt_count", "3"), ("output_tokens", None), ("needs_review", "false")):
            snapshot, selections, labels = create_review_inputs([("direct_use", "direct_use")])
            snapshot["groups"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                compare_review_labels(snapshot, selections, labels)


class ReviewMetricsHtmlTests(unittest.TestCase):
    """Keep the local report readable, safe, and honest about missing measurements."""

    def test_report_shows_counts_rates_and_separate_sample_groups(self) -> None:
        """Present understandable results without requiring readers to inspect JSON."""
        snapshot, selections, labels = create_review_inputs([
            ("direct_use", "direct_use"), ("direct_use", "unrelated"), ("unclear", "direct_use"),
        ])
        selections[1]["sample_kind"] = "challenge"
        summary = compare_review_labels(snapshot, selections, labels)["summary"]
        summary["provenance"] = {"model": "test-model", "prompt_version": "repository-product-v2", "manifest_id": "sample-hash"}
        before = copy.deepcopy(summary)
        document = render_review_metrics(summary)
        self.assertTrue(document.startswith("<!doctype html>"))
        self.assertIn("Model: <strong>test-model</strong>", document)
        self.assertIn("sample-hash", document)
        self.assertIn("1 / 2 (50.0%)", document)
        self.assertIn("1 / 3 (33.3%)", document)
        self.assertIn("Selected examples", document)
        self.assertIn("Human categories recorded", document)
        self.assertIn("Eligible category comparisons", document)
        for text in ("Development", "Held-out test", "Regression", "Ordinary search results", "Deliberately difficult examples"):
            self.assertIn(text, document)
        self.assertIn("not a measure of all GitHub usage or search coverage", document)
        self.assertEqual(summary, before)

    def test_report_escapes_all_supplied_text_and_uses_no_external_assets(self) -> None:
        """Render hostile metadata as text rather than executable HTML or attributes."""
        summary = compare_review_labels(*create_review_inputs([("direct_use", "direct_use")]))["summary"]
        hostile_text = '\"><script>alert("unsafe")</script><img src="https://invalid.test" onerror="bad()">'
        summary["provenance"] = {"model": hostile_text, "manifest_id": hostile_text}
        summary["scope"] = hostile_text
        summary["limitations"] = [hostile_text]
        summary["metrics"]["category_agreement"]["definition"] = hostile_text
        summary["selected_groups"] = hostile_text
        document = render_review_metrics(summary)
        self.assertIn(escape(hostile_text), document)
        self.assertNotIn(hostile_text, document)
        self.assertNotIn("<script", document)
        self.assertNotIn("<img", document)
        self.assertNotIn("<link", document)
        self.assertNotIn('src="https://', document)
        self.assertIn("default-src 'none'", document)
        self.assertIn('scope="col"', document)

    def test_empty_denominators_and_unrecorded_time_are_not_zero_percent_or_minutes(self) -> None:
        """Keep an unreviewed pilot visibly unmeasured rather than apparently accurate."""
        snapshot, selections, labels = create_review_inputs([("direct_use", "")])
        labels[0]["review_minutes"] = ""
        summary = compare_review_labels(snapshot, selections, labels)["summary"]
        document = render_review_metrics(summary)
        self.assertIn("Not available (0 eligible)", document)
        self.assertNotIn("0 / 0", document)
        self.assertIn("Recorded human review time (minutes)</td><td>Not recorded", document)
        self.assertIn("Mean recorded review time per example (minutes)</td><td>Not recorded", document)
        self.assertIn("Blank review durations are unknown, not zero minutes", document)

    def test_unknown_usage_is_explicit_even_if_recorded_tokens_are_zero(self) -> None:
        """Never describe missing token receipts as zero-cost classification work."""
        snapshot, selections, labels = create_review_inputs([("direct_use", "direct_use")])
        snapshot["groups"][0].update(input_tokens=0, output_tokens=0, attempts_without_usage=2)
        labels[0]["review_minutes"] = "0"
        document = render_review_metrics(compare_review_labels(snapshot, selections, labels)["summary"])
        self.assertIn("2 recorded attempt(s) have no token-usage record", document)
        self.assertIn("totals are partial", document)
        self.assertIn("unknown, not zero or free", document)
        self.assertIn("Recorded human review time (minutes)</td><td>0", document)
        self.assertIn("Recorded input tokens</td><td>0", document)

    def test_missing_optional_provenance_and_usage_show_unavailable_information(self) -> None:
        """Render a minimal summary without inventing a model or processing totals."""
        document = render_review_metrics({})
        self.assertIn("Model: <strong>Not recorded</strong>", document)
        self.assertIn("Token-usage completeness is not recorded", document)
        self.assertIn("Planned classification requests</td><td>Not recorded", document)


if __name__ == "__main__":
    unittest.main()

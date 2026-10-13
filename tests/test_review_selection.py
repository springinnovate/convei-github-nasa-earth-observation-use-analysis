"""Exercise frozen review membership and independent retrieval comparisons."""

from copy import deepcopy
import json
import unittest

from nasa_eo_search.batch_files import COMPLETED_SEARCH
from nasa_eo_search.review_selection import build_review_selection, compare_known_examples


def make_review_group(repository: str, short_name: str = "ATL03") -> dict:
    """Create a flat immutable-review fixture without files or network access.

    Args:
        repository: Public GitHub repository identity.
        short_name: Product short name within the NSIDC provider.

    Returns:
        Flat group snapshot suitable for build_review_selection.
    """
    return {"repository": repository, "provider": "NSIDC", "short_name": short_name,
            "group_id": f"{repository}:{short_name}", "evidence_revision": "saved-evidence-revision"}


def make_selection_row(group: dict, split: str = "test", family: str = "shared-family") -> dict:
    """Describe an explicit review split for one fixture group.

    Args:
        group: Flat source group.
        split: Regression, development, or test assignment.
        family: Reviewer-supplied related-repository identifier.

    Returns:
        Six-column selection row.
    """
    return {**{field: group[field] for field in ("repository", "provider", "short_name")},
            "split": split, "sample_kind": "ordinary", "family": family}


def make_known_example(repository: str, short_name: str = "ATL03") -> dict:
    """Create an independently documented repository/product example.

    Args:
        repository: Known repository identity.
        short_name: Expected product short name.

    Returns:
        Known-example metadata with no inferred missing-match reason.
    """
    return {"repository": repository, "provider": "NSIDC", "short_name": short_name,
            "source_url": "https://example.org/independent-product-examples", "discovery_basis": "independent_documentation",
            "miss_reason": "", "notes": "Reviewer independently verified the program."}


def make_saved_query(short_name: str = "ATL03", status: str = COMPLETED_SEARCH) -> dict:
    """Create a saved product query, including products with no matching groups.

    Args:
        short_name: Product searched by this query.
        status: Latest saved attempt status.

    Returns:
        Source-product-query record with immutable test provenance.
    """
    return {"provider": "NSIDC", "short_name": short_name, "search_id": f"query-{short_name}",
            "term": short_name, "query": f"repo:^github\\.com/ {short_name}", "status": status,
            "warnings": [], "error": ""}


def make_retrieval_plan() -> dict:
    """Create one matched repository and a separate zero-hit product search.

    Returns:
        Validated-plan-shaped data with match and query provenance.
    """
    return {"groups": [{"group_id": "found-group", "repository": "github.com/example/Found",
                        "product": {"provider": "NSIDC", "short_name": "ATL03"},
                        "files": [{"file": "example.py", "commit": "a" * 40}]}],
            "source_product_queries": [make_saved_query(), make_saved_query("ATL06")],
            "search_scope": "Sourcegraph indexed public repositories"}


class ReviewSelectionTests(unittest.TestCase):
    """Keep reviewer choices explicit, deterministic, and free of family leakage."""

    def test_default_selection_freezes_all_groups_as_ordinary_regression(self) -> None:
        """Preserve source revisions and treat the existing pilot as regression data."""
        groups = [make_review_group("github.com/example/z"), make_review_group("github.com/example/a")]
        original = deepcopy(groups)
        entries = build_review_selection(groups)
        self.assertEqual([entry["repository"] for entry in entries], ["github.com/example/a", "github.com/example/z"])
        self.assertTrue(all(entry["split"] == "regression" and entry["sample_kind"] == "ordinary" for entry in entries))
        self.assertTrue(all(entry["family"] == entry["repository"] for entry in entries))
        self.assertTrue(all(entry["evidence_revision"] == "saved-evidence-revision" for entry in entries))
        self.assertEqual(groups, original)

    def test_explicit_selection_can_choose_a_subset_and_challenge_examples(self) -> None:
        """Freeze explicit choices without adding unselected groups or random splits."""
        groups = [make_review_group("github.com/example/original"), make_review_group("github.com/example/fork")]
        selected = make_selection_row(groups[1], "test", "library-lineage")
        selected["sample_kind"] = "challenge"
        entries = build_review_selection(groups, [selected])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["group_id"], groups[1]["group_id"])
        self.assertEqual(entries[0]["sample_kind"], "challenge")
        self.assertEqual(entries[0]["family"], "library-lineage")

    def test_forks_in_one_family_cannot_cross_splits(self) -> None:
        """Reject related-project leakage even if family letter case differs."""
        groups = [make_review_group("github.com/example/original"), make_review_group("github.com/example/fork")]
        for first_split, second_split in (("development", "test"), ("regression", "test"), ("regression", "development")):
            with self.subTest(splits=(first_split, second_split)), self.assertRaisesRegex(ValueError, "family cannot cross"):
                build_review_selection(groups, [make_selection_row(groups[0], first_split, "Lineage"),
                                                make_selection_row(groups[1], second_split, "lineage")])
        self.assertEqual(len(build_review_selection(groups, [make_selection_row(group) for group in groups])), 2)

    def test_one_repository_across_products_requires_one_family(self) -> None:
        """Prevent product-specific family names from bypassing held-out separation."""
        groups = [make_review_group("github.com/example/project", "ATL03"),
                  make_review_group("github.com/example/project", "ATL06")]
        with self.assertRaisesRegex(ValueError, "same repository"):
            build_review_selection(groups, [make_selection_row(groups[0], family="first"),
                                            make_selection_row(groups[1], family="second")])

    def test_invalid_selection_schema_values_and_unknown_groups_are_rejected(self) -> None:
        """Catch typos and missing explicit family mappings before saving a sample."""
        group = make_review_group("github.com/example/project")
        valid = make_selection_row(group)
        for changes in ({"split": "validation"}, {"sample_kind": "random"}, {"family": " "},
                        {"provider": "another-provider"}, {"short_name": "ATL99"},
                        {"repository": "https://github.com/example/project"}, {"unexpected": "value"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_review_selection([group], [{**valid, **changes}])
        missing_family = {field: value for field, value in valid.items() if field != "family"}
        with self.assertRaises(ValueError):
            build_review_selection([group], [missing_family])

    def test_duplicate_selection_and_duplicate_source_identities_are_rejected(self) -> None:
        """Treat GitHub owner/project case variants as the same repository identity."""
        group = make_review_group("github.com/Example/Project")
        selected = make_selection_row(group)
        lowercase = {**selected, "repository": "github.com/example/project"}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_review_selection([group], [selected, lowercase])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_review_selection([group, {**group, "repository": "github.com/example/project", "group_id": "different-id"}])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_review_selection([group, {**group, "repository": "github.com/example/other"}])
        self.assertEqual(build_review_selection([group], [lowercase])[0]["repository"], group["repository"])

    def test_empty_inputs_and_invalid_source_snapshot_are_explicit(self) -> None:
        """Keep an empty selection empty and require source evidence revisions."""
        self.assertEqual(build_review_selection([]), [])
        group = make_review_group("github.com/example/project")
        self.assertEqual(build_review_selection([group], []), [])
        for changes in ({"evidence_revision": ""}, {"group_id": None}, {"provider": 12}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_review_selection([{**group, **changes}])


class KnownExampleRetrievalTests(unittest.TestCase):
    """Separate saved query coverage from retrieval findings and inferred causes."""

    def test_found_zero_hit_incomplete_and_unsearched_statuses_have_explicit_rates(self) -> None:
        """Measure every known example, not just products with saved hit groups."""
        plan = make_retrieval_plan()
        plan["source_product_queries"].append(make_saved_query("MOD09", "incomplete"))
        known = [make_known_example("github.com/example/found"),
                 make_known_example("github.com/example/zero", "ATL06"),
                 make_known_example("github.com/example/partial", "MOD09"),
                 make_known_example("github.com/example/unsearched", "NEWPRODUCT")]
        original_plan, original_known = deepcopy(plan), deepcopy(known)
        result = compare_known_examples(plan, known)
        self.assertEqual([row["retrieval_status"] for row in result["rows"]],
                         ["found", "searched_zero_not_observed", "incomplete", "not_searched"])
        summary = result["summary"]
        self.assertEqual((summary["found"], summary["known_examples"]), (1, 4))
        self.assertEqual(summary["known_example_retrieval_rate"], 0.25)
        self.assertEqual((summary["searched_found"], summary["searched_examples"]), (1, 3))
        self.assertEqual(summary["searched_known_example_retrieval_rate"], 1 / 3)
        self.assertEqual((summary["complete_search_found"], summary["complete_search_examples"]), (1, 2))
        self.assertEqual(summary["complete_search_known_example_retrieval_rate"], 0.5)
        self.assertIn("not GitHub-wide recall", summary["interpretation"])
        self.assertEqual(plan, original_plan)
        self.assertEqual(known, original_known)

    def test_saved_match_is_found_even_if_search_is_partial_or_metadata_missing(self) -> None:
        """Do not erase observed retrieval merely because global coverage is unknown."""
        for metadata in ([], None, [make_saved_query(status="incomplete")]):
            with self.subTest(metadata=metadata):
                plan = make_retrieval_plan()
                plan["source_product_queries"] = metadata
                result = compare_known_examples(plan, [make_known_example("github.com/example/found")])
                row = result["rows"][0]
                self.assertEqual(row["retrieval_status"], "found")
                self.assertFalse(row["search_complete"])
                self.assertEqual(result["summary"]["found"], 1)

    def test_pending_or_unknown_queries_are_incomplete_not_confirmed_started(self) -> None:
        """Exclude only-planned queries from the searched-subset denominator."""
        for status in ("pending", "unrecognized", None):
            with self.subTest(status=status):
                plan = {"groups": [], "source_product_queries": [make_saved_query(status=status)]}
                result = compare_known_examples(plan, [make_known_example("github.com/example/missing")])
                row = result["rows"][0]
                self.assertEqual(row["retrieval_status"], "incomplete")
                self.assertFalse(row["search_started"])
                self.assertIsNone(result["summary"]["searched_known_example_retrieval_rate"])
                self.assertEqual(result["summary"]["searched_examples"], 0)

    def test_any_unfinished_query_prevents_complete_coverage(self) -> None:
        """Use every saved query for a product, including an unfinished alternate term."""
        plan = make_retrieval_plan()
        alternate = {**make_saved_query(status="interrupted"), "search_id": "alternate", "term": "photon"}
        plan["source_product_queries"].append(alternate)
        row = compare_known_examples(plan, [make_known_example("github.com/example/absent")])["rows"][0]
        self.assertEqual(row["retrieval_status"], "incomplete")
        self.assertEqual(row["query_count"], 2)
        self.assertFalse(row["search_complete"])
        self.assertTrue(row["search_started"])
        self.assertEqual(json.loads(row["queries"])[1]["search_id"], "alternate")

    def test_query_provenance_and_reviewer_miss_reason_are_preserved_without_inference(self) -> None:
        """Keep evidence of limits separate from a reviewer-declared cause."""
        plan = make_retrieval_plan()
        plan["source_product_queries"][0].update(status="incomplete", warnings=["Search result limit reached"], error="limit")
        known = make_known_example("github.com/example/absent")
        row = compare_known_examples(plan, [known])["rows"][0]
        self.assertEqual(row["miss_reason"], "")
        self.assertIn("limit reached", row["query_warnings"])
        self.assertEqual(json.loads(row["queries"])[0]["error"], "limit")
        known["miss_reason"] = "index_coverage"
        row = compare_known_examples(plan, [known])["rows"][0]
        self.assertEqual(row["miss_reason"], "index_coverage")
        self.assertEqual(row["notes"], known["notes"])

    def test_invalid_or_nonindependent_examples_are_rejected(self) -> None:
        """Require external reference provenance and reject malformed repository keys."""
        valid = make_known_example("github.com/example/project")
        for changes in ({"repository": "gitlab.com/example/project"}, {"repository": "github.com/a/.."},
                        {"repository": "github.com/a/b/tree/main"}, {"source_url": "file:///tmp/readme"},
                        {"source_url": "https:///missing-host"}, {"source_url": "https://user:secret@example.org"},
                        {"source_url": "https://example.org/invalid path"}, {"source_url": "https://[bad"},
                        {"source_url": "https://example.org:wrong"}, {"discovery_basis": "search_results"},
                        {"miss_reason": "NASA does not use this"}, {"typo_column": "value"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                compare_known_examples(make_retrieval_plan(), [{**valid, **changes}])

    def test_duplicate_examples_are_rejected_instead_of_inflating_denominators(self) -> None:
        """Count a repository/product once even if supplied with different casing."""
        example = make_known_example("github.com/example/Project")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            compare_known_examples(make_retrieval_plan(), [example, {**example, "repository": "github.com/example/project"}])

    def test_missing_sources_empty_examples_and_malformed_warning_metadata_are_safe(self) -> None:
        """Report unknown coverage without fabricating a search or dividing by zero."""
        empty = compare_known_examples({"groups": []}, [])
        self.assertEqual(empty["summary"]["known_examples"], 0)
        self.assertIsNone(empty["summary"]["known_example_retrieval_rate"])
        row = compare_known_examples({"groups": []}, [make_known_example("github.com/example/missing")])["rows"][0]
        self.assertEqual(row["retrieval_status"], "not_searched")
        self.assertIn("cannot be established", row["coverage_note"])
        plan = make_retrieval_plan()
        plan["source_product_queries"][0]["warnings"] = None
        row = compare_known_examples(plan, [make_known_example("github.com/example/found")])["rows"][0]
        self.assertEqual(row["query_warnings"], "")


if __name__ == "__main__":
    unittest.main()

"""Verify bounded repository diversity, query fairness, and legacy selection."""

import copy
import unittest

from nasa_eo_search.github_search import reconcile_snapshot_repositories


def create_selection_plan(maximum_repositories: int = 4, queries: list[dict] | None = None) -> dict:
    """Build a new-policy search plan without files, network, or credentials.

    Args:
        maximum_repositories: Total selection cap, including prior failures.
        queries: Optional ordered dataset/term associations, including aliases.

    Returns:
        Minimal plan containing the settings used by repository selection.
    """
    selected_queries = queries or [{"short_name": "FIRST", "term": "FIRST"},
                                  {"short_name": "SECOND", "term": "SECOND"}]
    return {"selection_policy": "diverse-repository-names-v1",
            "discovery_terms": list(dict.fromkeys(query["short_name"] for query in selected_queries)),
            "discovery_queries": selected_queries, "max_repositories": maximum_repositories,
            "max_archive_mb": 10}


def create_selection_receipt(term: str, repositories: list[str | tuple[str, dict]],
                             provider: str = "github-repositories", page: int = 1) -> dict:
    """Build an ordered public search receipt for repository-selection tests.

    Args:
        term: Actual query term, which may be an alias for a selected dataset.
        repositories: Repository names or name/metadata pairs in service order.
        provider: Discovery service recorded on the receipt.
        page: Saved result-page number used for deterministic ordering.

    Returns:
        Receipt with realistic result names and optional sanitized metadata.
    """
    results = []
    for repository in repositories:
        name, metadata = repository if isinstance(repository, tuple) else (repository, None)
        result = {"repository": "github.com/" + name, "file": "example.py",
                  "url": "https://github.com/" + name + "/blob/main/example.py"}
        if metadata is not None:
            result["repository_metadata"] = metadata
        results.append(result)
    return {"provider": provider, "term": term, "page": page, "attempt": 1,
            "status": "returned", "results": results}


def selected_repository_names(records: list[dict]) -> list[str]:
    """Return selected repository names in their persisted download order.

    Args:
        records: Candidate rows returned by repository reconciliation.

    Returns:
        Host-prefixed names of selected repositories in list order.
    """
    return [record["repository"] for record in records if record["selected"]]


class DiscoverySelectionTests(unittest.TestCase):
    """Keep the sample varied without changing saved selections or exceeding caps."""

    def test_aliases_share_one_dataset_queue_and_do_not_take_extra_turns(self) -> None:
        """Alternate aliases within a dataset while preserving other datasets' turns."""
        plan = create_selection_plan(4, [
            {"short_name": "FIRST", "term": "FIRST"},
            {"short_name": "FIRST", "term": "FIRST_ALIAS"},
            {"short_name": "SECOND", "term": "SECOND"},
        ])
        receipts = [create_selection_receipt("FIRST", ["one/first", "one/later"]),
                    create_selection_receipt("FIRST_ALIAS", ["alias/alternative", "alias/last"]),
                    create_selection_receipt("SECOND", ["two/second", "two/next"])]
        records = reconcile_snapshot_repositories(receipts, [], plan)
        self.assertEqual(selected_repository_names(records), [
            "github.com/one/first", "github.com/two/second",
            "github.com/alias/alternative", "github.com/two/next",
        ])
        alternative = next(record for record in records if record["repository"].endswith("/alternative"))
        self.assertEqual(alternative["discovered_by"], ["github-repositories:FIRST_ALIAS"])

    def test_repository_search_queues_precede_code_queues_across_datasets(self) -> None:
        """Give both products a repository-search slot before a code-search slot."""
        receipts = [create_selection_receipt("FIRST", ["one/metadata"]),
                    create_selection_receipt("FIRST", ["one/code"], provider="github"),
                    create_selection_receipt("SECOND", ["two/readme"]),
                    create_selection_receipt("SECOND", ["two/source"], provider="github")]
        records = reconcile_snapshot_repositories(receipts, [], create_selection_plan(2))
        self.assertEqual(selected_repository_names(records),
                         ["github.com/one/metadata", "github.com/two/readme"])

    def test_repeated_names_forks_and_large_size_proxies_are_deferred(self) -> None:
        """Prefer distinct names and suitable reported metadata without hard exclusion."""
        receipts = [create_selection_receipt("FIRST", [
            ("owner/library", {"fork": False, "size_kb": 100, "stars": 5, "archived": False}),
            ("forker/other", {"fork": True, "size_kb": 100}),
            ("large/archive", {"fork": False, "size_kb": 10001}),
            "unknown/small-tool",
        ]), create_selection_receipt("SECOND", ["copy/LIBRARY", "another/analysis", "two/reader"])]
        records = reconcile_snapshot_repositories(receipts, [], create_selection_plan(4))
        self.assertEqual(selected_repository_names(records), [
            "github.com/owner/library", "github.com/another/analysis",
            "github.com/unknown/small-tool", "github.com/two/reader",
        ])
        by_name = {record["repository"]: record for record in records}
        self.assertEqual(by_name["github.com/copy/LIBRARY"]["selection_group"], "library")
        self.assertIn("repeated repository name", by_name["github.com/copy/LIBRARY"]["selection_reason"])
        self.assertIn("reported fork", by_name["github.com/forker/other"]["selection_reason"])
        self.assertIn("size", by_name["github.com/large/archive"]["selection_reason"])
        self.assertEqual(by_name["github.com/owner/library"]["discovery_metadata"]["stars"], 5)

    def test_deferred_candidates_fill_remaining_slots_and_keep_the_cap(self) -> None:
        """Fall back to copies and larger forks when no preferred candidates remain."""
        plan = create_selection_plan(3)
        receipts = [create_selection_receipt("FIRST", ["one/shared", "copy/shared"]),
                    create_selection_receipt("SECOND", [("two/large-fork", {"fork": True, "size_kb": 20000})])]
        records = reconcile_snapshot_repositories(receipts, [], plan)
        self.assertEqual(selected_repository_names(records), [
            "github.com/one/shared", "github.com/copy/shared", "github.com/two/large-fork",
        ])
        self.assertTrue(all(record["selection_reason"].startswith("Fallback:") for record in records[1:]))
        self.assertEqual(len(selected_repository_names(records)), plan["max_repositories"])

    def test_selected_duplicates_do_not_consume_another_queue_turn(self) -> None:
        """Skip already-selected candidates within a queue before serving its turn."""
        receipts = [create_selection_receipt("FIRST", ["one/shared", "one/third"]),
                    create_selection_receipt("SECOND", ["one/shared", "two/second"])]
        records = reconcile_snapshot_repositories(receipts, [], create_selection_plan(3))
        self.assertEqual(selected_repository_names(records),
                         ["github.com/one/shared", "github.com/two/second", "github.com/one/third"])
        self.assertEqual(records[0]["discovered_by"], ["github-repositories:FIRST", "github-repositories:SECOND"])

    def test_resume_keeps_selected_failures_and_original_download_order(self) -> None:
        """Do not evict selected repositories or replace failures with extra downloads."""
        plan = create_selection_plan(2)
        original_receipts = [create_selection_receipt("FIRST", ["old/first"]),
                             create_selection_receipt("SECOND", ["old/second"])]
        original = reconcile_snapshot_repositories(original_receipts, [], plan)
        original[0]["status"], original[0]["error"] = "error", "Download budget exceeded."
        original[1]["status"], original[1]["commit"] = "complete", "a" * 40
        frozen = copy.deepcopy(original)
        new_receipts = [*original_receipts, create_selection_receipt("FIRST", ["new/better"], page=2)]
        resumed = reconcile_snapshot_repositories(new_receipts, original, plan)
        self.assertEqual(selected_repository_names(resumed), selected_repository_names(original))
        self.assertEqual(resumed[0]["status"], "error")
        self.assertEqual(resumed[1]["commit"], "a" * 40)
        self.assertEqual(original, frozen)
        self.assertFalse(resumed[-1]["selected"])

    def test_partial_resume_keeps_prior_selection_before_new_round_robin_entries(self) -> None:
        """Append newly discovered selections after previously pinned repository rows."""
        first_receipts = [create_selection_receipt("SECOND", ["saved/second"])]
        original = reconcile_snapshot_repositories(first_receipts, [], create_selection_plan(4))
        receipts = [create_selection_receipt("FIRST", ["new/first", "new/third"]), *first_receipts,
                    create_selection_receipt("SECOND", ["new/fourth"], page=2)]
        resumed = reconcile_snapshot_repositories(receipts, original, create_selection_plan(4))
        self.assertEqual(selected_repository_names(resumed), [
            "github.com/saved/second", "github.com/new/first", "github.com/new/fourth", "github.com/new/third",
        ])

    def test_receipt_order_does_not_change_selection_or_report_metadata(self) -> None:
        """Sort saved pages consistently even when the receipt list arrives reversed."""
        receipts = [create_selection_receipt("FIRST", ["one/later"], page=2),
                    create_selection_receipt("SECOND", ["two/next"]),
                    create_selection_receipt("FIRST", ["one/first"], page=1)]
        self.assertEqual(reconcile_snapshot_repositories(receipts, [], create_selection_plan(3)),
                         reconcile_snapshot_repositories(list(reversed(receipts)), [], create_selection_plan(3)))

    def test_unknown_metadata_remains_eligible_and_only_public_fields_are_retained(self) -> None:
        """Do not invent fork or size facts when optional values are absent or malformed."""
        receipt = create_selection_receipt("FIRST", [("one/unknown", {
            "fork": "false", "archived": None, "size_kb": True, "stars": -1, "token": "do-not-copy",
        })])
        records = reconcile_snapshot_repositories([receipt], [], create_selection_plan(1))
        self.assertEqual(selected_repository_names(records), ["github.com/one/unknown"])
        self.assertEqual(records[0]["discovery_metadata"], {})
        self.assertNotIn("token", str(records))

    def test_legacy_plan_preserves_original_rank_and_insertion_order(self) -> None:
        """Keep old selection behavior and fields when a saved plan has no policy."""
        plan = create_selection_plan(3)
        plan.pop("selection_policy")
        receipts = [create_selection_receipt("FIRST", ["one/shared", "copy/shared"], provider="github"),
                    create_selection_receipt("SECOND", ["two/second", "two/third"], provider="github"),
                    create_selection_receipt("FIRST", ["repo-search/ignored"])]
        records = reconcile_snapshot_repositories(receipts, [], plan)
        self.assertEqual(selected_repository_names(records),
                         ["github.com/one/shared", "github.com/copy/shared", "github.com/two/second"])
        self.assertFalse(any("selection_group" in record or "discovery_metadata" in record for record in records))
        self.assertNotIn("github.com/repo-search/ignored", [record["repository"] for record in records])

    def test_new_policy_can_use_legacy_term_list_when_queries_are_absent(self) -> None:
        """Build one alias queue per selected short name when no query map is stored."""
        plan = create_selection_plan(2)
        plan.pop("discovery_queries")
        receipts = [create_selection_receipt("FIRST", ["one/first"]),
                    create_selection_receipt("SECOND", ["two/second"])]
        self.assertEqual(selected_repository_names(reconcile_snapshot_repositories(receipts, [], plan)),
                         ["github.com/one/first", "github.com/two/second"])

    def test_unknown_policy_fails_instead_of_silently_changing_selection(self) -> None:
        """Reject an unrecognized saved strategy before selecting or changing rows."""
        plan = {**create_selection_plan(), "selection_policy": "unknown-policy"}
        with self.assertRaisesRegex(ValueError, "Unsupported repository selection policy"):
            reconcile_snapshot_repositories([], [], plan)


if __name__ == "__main__":
    unittest.main()

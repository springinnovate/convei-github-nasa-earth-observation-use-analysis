"""Verify exhaustive repository partitions and legacy-compatible saved plans."""

from copy import deepcopy
import string
import unittest

from nasa_eo_search.batch_plan import identify_search_query, validate_saved_batch_plan
from nasa_eo_search.repo_preview import build_sourcegraph_product_query
from nasa_eo_search.search_partitions import (
    PARTITION_SEARCH_STRATEGY,
    REPOSITORY_PARTITIONS,
    build_partition_query,
    partition_catalog_plan,
    repository_matches_partition,
)
from nasa_eo_search.sourcegraph import DEFAULT_SOURCEGRAPH_ENDPOINT


def make_legacy_partition_fixture(all_hosts: bool = False) -> dict:
    """Create a legacy plan with shared terms and complete source associations.

    Args:
        all_hosts: Whether to include public hosts other than GitHub.

    Returns:
        Valid schema-version-1 plan with two independently searchable terms.
    """

    jobs = []
    for term in ("ATL03", "ASF_ECMWF_SMST_1"):
        query = build_sourcegraph_product_query(term, all_hosts, collect_all=True)
        jobs.append({
            "term": term, "query": query, "search_id": identify_search_query(query),
            "products": [{
                "provider": provider, "short_name": term, "dataset_name": f"NASA {term}",
                "candidate": {"term": term, "review_status": "unreviewed", "sources": [
                    {"concept_id": f"C-{provider}-{term}", "source_field": "short_name"},
                ]},
            } for provider in ("FIRST", "SECOND")],
        })
    return {
        "schema_version": 1, "created_at": "2026-10-15T12:00:00+00:00",
        "catalog_directory": "saved-catalog", "catalog_summary": {"status": "complete", "products_saved": 4},
        "all_hosts": all_hosts, "endpoint": DEFAULT_SOURCEGRAPH_ENDPOINT,
        "scope": "Sourcegraph indexed public content, including forks and archives",
        "report_unit": "file", "selection_mode": "all_collected_products", "jobs": jobs,
    }


def matching_partition_ids(repository: str, all_hosts: bool) -> list[str]:
    """Evaluate the simple partition filters against a repository name offline.

    Args:
        repository: Sourcegraph-style host, owner, and repository path.
        all_hosts: Whether the legacy GitHub-only filter is absent.

    Returns:
        IDs whose filter and host scope include the repository.
    """

    return [partition["id"] for partition in REPOSITORY_PARTITIONS
            if repository_matches_partition(repository, all_hosts, partition["id"])]


class PartitionQueryTests(unittest.TestCase):
    """Keep partition queries exhaustive without changing product-match semantics."""

    def test_all_ascii_alphanumeric_owner_prefixes_have_one_partition(self) -> None:
        """Cover both cases and digits exactly once with the required bucket ranges."""

        expected_ranges = (
            "[0-9a-cA-C]", "[d-fD-F]", "[g-iG-I]", "[j-lJ-L]",
            "[m-oM-O]", "[p-rP-R]", "[s-uS-U]", "[v-zV-Z]",
        )
        self.assertEqual(len(REPOSITORY_PARTITIONS), 9)
        self.assertEqual(len({partition["id"] for partition in REPOSITORY_PARTITIONS}), 9)
        for partition, owner_range in zip(REPOSITORY_PARTITIONS, expected_ranges):
            self.assertEqual(partition["filter"], r"repo:^github\.com/" + owner_range)
        for owner_prefix in string.ascii_letters + string.digits:
            with self.subTest(owner_prefix=owner_prefix):
                matches = matching_partition_ids(f"github.com/{owner_prefix}owner/project", False)
                self.assertEqual(len(matches), 1)
                self.assertNotIn("remainder", matches)

    def test_remainder_preserves_unusual_prefixes_and_all_hosts_scope(self) -> None:
        """Include every complementary name without accidentally expanding GitHub-only scope."""

        for repository in ("github.com/-owner/project", "github.com/_owner/project", "github.com/éowner/project",
                           "github.com/", "gitlab.com/owner/project", "code.example.org/team/project",
                           "notgithub.com/owner/project"):
            with self.subTest(repository=repository):
                self.assertEqual(matching_partition_ids(repository, True), ["remainder"])
                expected = ["remainder"] if repository.startswith("github.com/") else []
                self.assertEqual(matching_partition_ids(repository, False), expected)

    def test_query_retains_legacy_boundary_and_filters_with_shorter_timeout(self) -> None:
        """Change only the explicit timeout and appended repository partition filter."""

        for all_hosts in (False, True):
            for term in ("ATL03", "ASF_ECMWF_SMST_1", 'ATL03.(x) repo:other "quoted"'):
                legacy_query = build_sourcegraph_product_query(term, all_hosts, collect_all=True)
                for partition in REPOSITORY_PARTITIONS:
                    with self.subTest(all_hosts=all_hosts, term=term, partition=partition["id"]):
                        query = build_partition_query(term, all_hosts, partition["id"])
                        self.assertEqual(query, legacy_query.removesuffix(" timeout:60s")
                                         + " timeout:10s " + partition["filter"])
                        self.assertIn("fork:yes archived:yes count:all", query)
                        self.assertIn(r"content:(^|[^A-Za-z0-9])", query)
                        if not all_hosts:
                            self.assertTrue(query.startswith(r"repo:^github\.com/ "))

    def test_response_partition_check_uses_exact_hosts_and_validates_inputs(self) -> None:
        """Route a known owner and reject invalid scope arguments without network access."""

        self.assertTrue(repository_matches_partition("github.com/example/project", False, "github-d-f"))
        self.assertFalse(repository_matches_partition("github.com/example/project", False, "remainder"))
        self.assertFalse(repository_matches_partition("GitHub.com/example/project", False, "github-d-f"))
        self.assertTrue(repository_matches_partition("GitHub.com/example/project", True, "remainder"))
        for repository in ("", None, 3):
            with self.subTest(repository=repository):
                self.assertFalse(repository_matches_partition(repository, True, "remainder"))
        with self.assertRaises(ValueError):
            repository_matches_partition("github.com/example/project", "false", "remainder")
        with self.assertRaises(ValueError):
            repository_matches_partition("github.com/example/project", False, "missing")

    def test_timeout_boundaries_change_query_identity_without_changing_scope(self) -> None:
        """Accept one and 60 seconds while giving each exact query its own identity."""

        queries = [build_partition_query("ATL03", False, "remainder", duration) for duration in (1, 10, 60)]
        self.assertEqual(len({identify_search_query(query) for query in queries}), 3)
        for duration, query in zip((1, 10, 60), queries):
            self.assertIn(f"timeout:{duration}s ", query)
            self.assertTrue(query.endswith(REPOSITORY_PARTITIONS[-1]["filter"]))

    def test_invalid_query_inputs_fail_before_building_a_search(self) -> None:
        """Reject invalid terms, boolean timeouts, host flags, and unknown partition IDs."""

        for timeout in (0, 61, -1, 10.0, "10", True, None):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                build_partition_query("ATL03", False, "remainder", timeout)
        for term in ("", " ATL03", "ATL03 ", "ATL03\n", "ATL\x00", None, 3):
            with self.subTest(term=term), self.assertRaises(ValueError):
                build_partition_query(term, False, "remainder")
        for partition_id in ("missing", "", None, [], True):
            with self.subTest(partition_id=partition_id), self.assertRaises(ValueError):
                build_partition_query("ATL03", False, partition_id)
        with self.assertRaises(ValueError):
            build_partition_query("ATL03", "false", "remainder")


class PartitionPlanTests(unittest.TestCase):
    """Validate all partition jobs while retaining original catalog provenance."""

    def test_expansion_preserves_metadata_associations_and_input_without_network(self) -> None:
        """Build deterministic term-first jobs and retain all product association details."""

        legacy_plan = make_legacy_partition_fixture()
        original_plan = deepcopy(legacy_plan)
        partitioned_plan = partition_catalog_plan(legacy_plan)
        self.assertEqual(legacy_plan, original_plan)
        self.assertEqual(partitioned_plan, partition_catalog_plan(legacy_plan))
        self.assertEqual(partitioned_plan["schema_version"], 2)
        self.assertEqual(partitioned_plan["search_strategy"], PARTITION_SEARCH_STRATEGY)
        self.assertEqual(partitioned_plan["search_timeout_seconds"], 10)
        self.assertEqual(len(partitioned_plan["jobs"]), 18)
        for field in set(legacy_plan) - {"schema_version", "jobs"}:
            self.assertEqual(partitioned_plan[field], legacy_plan[field])
        for term_index, legacy_job in enumerate(legacy_plan["jobs"]):
            partition_jobs = partitioned_plan["jobs"][term_index * 9:(term_index + 1) * 9]
            self.assertEqual([job["partition_id"] for job in partition_jobs],
                             [partition["id"] for partition in REPOSITORY_PARTITIONS])
            for job in partition_jobs:
                self.assertEqual(job["term"], legacy_job["term"])
                self.assertEqual(job["products"], legacy_job["products"])
                self.assertEqual(job["search_id"], identify_search_query(job["query"]))
                self.assertIs(job["products"], partition_jobs[0]["products"])
                self.assertIsNot(job["products"], legacy_job["products"])
        validate_saved_batch_plan(partitioned_plan)

    def test_all_hosts_and_nondefault_timeout_survive_validation(self) -> None:
        """Keep the selected public-host scope and custom duration in every job."""

        plan = partition_catalog_plan(make_legacy_partition_fixture(True), 30)
        self.assertTrue(plan["all_hosts"])
        self.assertEqual(plan["search_timeout_seconds"], 30)
        for job in plan["jobs"]:
            self.assertTrue(job["query"].startswith("visibility:public "))
            self.assertIn("timeout:30s ", job["query"])
        validate_saved_batch_plan(plan)

    def test_legacy_validation_leaves_original_queries_and_schema_unchanged(self) -> None:
        """Permit existing schema-version-1 runs to resume with their original query IDs."""

        plan = make_legacy_partition_fixture()
        original_plan = deepcopy(plan)
        validate_saved_batch_plan(plan)
        self.assertEqual(plan, original_plan)
        self.assertTrue(all(job["query"].endswith("timeout:60s") for job in plan["jobs"]))

    def test_missing_duplicate_and_unknown_partitions_are_rejected(self) -> None:
        """Refuse incomplete partition coverage instead of silently narrowing search scope."""

        for problem in ("missing", "duplicate", "unknown", "absent"):
            with self.subTest(problem=problem):
                plan = partition_catalog_plan(make_legacy_partition_fixture())
                if problem == "missing":
                    plan["jobs"].pop(0)
                elif problem == "duplicate":
                    plan["jobs"].append(deepcopy(plan["jobs"][0]))
                elif problem == "unknown":
                    plan["jobs"][0]["partition_id"] = "unknown"
                else:
                    del plan["jobs"][0]["partition_id"]
                with self.assertRaises(ValueError):
                    validate_saved_batch_plan(plan)

    def test_association_changes_between_partitions_are_rejected(self) -> None:
        """Detect edited product names, missing associations, and altered source provenance."""

        for problem in ("missing", "name", "source", "duplicate"):
            with self.subTest(problem=problem):
                plan = partition_catalog_plan(make_legacy_partition_fixture())
                associations = deepcopy(plan["jobs"][0]["products"])
                plan["jobs"][0]["products"] = associations
                if problem == "missing":
                    associations.pop()
                elif problem == "name":
                    associations[0]["dataset_name"] = "Edited dataset"
                elif problem == "source":
                    associations[0]["candidate"]["sources"][0]["concept_id"] = "C-EDITED"
                else:
                    associations.append(deepcopy(associations[0]))
                with self.assertRaises(ValueError):
                    validate_saved_batch_plan(plan)

    def test_association_and_partition_order_do_not_change_coverage(self) -> None:
        """Accept reordered saved rows when identities and all associations remain intact."""

        plan = partition_catalog_plan(make_legacy_partition_fixture())
        plan["jobs"][0]["products"] = list(reversed(plan["jobs"][0]["products"]))
        plan["jobs"].reverse()
        validate_saved_batch_plan(plan)

    def test_query_id_scope_timeout_and_strategy_edits_are_rejected(self) -> None:
        """Reconstruct exact queries so stale IDs and narrowed filters cannot pass validation."""

        for problem in ("query", "id", "host", "timeout", "strategy", "schema"):
            with self.subTest(problem=problem):
                plan = partition_catalog_plan(make_legacy_partition_fixture())
                if problem == "query":
                    plan["jobs"][0]["query"] += " repo:handpicked"
                    plan["jobs"][0]["search_id"] = identify_search_query(plan["jobs"][0]["query"])
                elif problem == "id":
                    plan["jobs"][0]["search_id"] = "../unsafe"
                elif problem == "host":
                    plan["all_hosts"] = True
                elif problem == "timeout":
                    plan["search_timeout_seconds"] = 20
                elif problem == "strategy":
                    plan["search_strategy"] = "handpicked-repositories"
                else:
                    plan["schema_version"] = 1
                with self.assertRaises(ValueError):
                    validate_saved_batch_plan(plan)

    def test_invalid_plan_strategy_timeout_and_repeated_expansion_are_rejected(self) -> None:
        """Require the exact strategy and integer duration and avoid expanding partitions twice."""

        for timeout in (None, 0, 61, True, "10", 10.0):
            with self.subTest(timeout=timeout):
                plan = partition_catalog_plan(make_legacy_partition_fixture())
                plan["search_timeout_seconds"] = timeout
                with self.assertRaises(ValueError):
                    validate_saved_batch_plan(plan)
                with self.assertRaises(ValueError):
                    partition_catalog_plan(make_legacy_partition_fixture(), timeout)
        plan = partition_catalog_plan(make_legacy_partition_fixture())
        with self.assertRaises(ValueError):
            partition_catalog_plan(plan)
        del plan["search_strategy"]
        with self.assertRaises(ValueError):
            validate_saved_batch_plan(plan)


if __name__ == "__main__":
    unittest.main()

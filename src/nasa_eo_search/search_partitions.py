"""Partition public repository searches into exhaustive, reproducible scopes."""

from copy import deepcopy
import hashlib
import re
from typing import Any

from nasa_eo_search.repo_preview import build_sourcegraph_product_query


REPOSITORY_PARTITIONS: tuple[dict[str, str], ...] = (
    {"id": "github-0-9-a-c", "filter": r"repo:^github\.com/[0-9a-cA-C]"},
    {"id": "github-d-f", "filter": r"repo:^github\.com/[d-fD-F]"},
    {"id": "github-g-i", "filter": r"repo:^github\.com/[g-iG-I]"},
    {"id": "github-j-l", "filter": r"repo:^github\.com/[j-lJ-L]"},
    {"id": "github-m-o", "filter": r"repo:^github\.com/[m-oM-O]"},
    {"id": "github-p-r", "filter": r"repo:^github\.com/[p-rP-R]"},
    {"id": "github-s-u", "filter": r"repo:^github\.com/[s-uS-U]"},
    {"id": "github-v-z", "filter": r"repo:^github\.com/[v-zV-Z]"},
    {"id": "remainder", "filter": r"-repo:^github\.com/[0-9a-zA-Z]"},
)
PARTITION_SEARCH_STRATEGY = "repository-partitions-v1"


def repository_matches_partition(repository: str, all_hosts: bool, partition_id: str) -> bool:
    """Check whether a returned repository belongs to the requested partition.

    Evaluate the fixed prefix filters without network access. The GitHub host
    prefix is exact, while the owner ranges include both ASCII letter cases.
    The remainder is the complement of the eight positive GitHub partitions,
    intersected with the caller's selected host scope.

    Args:
        repository: Sourcegraph repository name, normally host/owner/project.
        all_hosts: Include other public code hosts in the remainder partition.
        partition_id: Identifier from REPOSITORY_PARTITIONS.

    Returns:
        Whether the repository is within the partition and host scope. Empty or
        non-string repository values return False.

    Raises:
        ValueError: If the host scope or partition identifier is invalid.
    """

    if type(all_hosts) is not bool:
        raise ValueError("Partition host scope must be a boolean.")
    partition_filter = next(
        (partition["filter"] for partition in REPOSITORY_PARTITIONS if partition["id"] == partition_id),
        None,
    )
    if partition_filter is None:
        raise ValueError(f"Unknown repository partition: {partition_id!r}.")
    if not isinstance(repository, str) or not repository:
        return False
    if not all_hosts and not repository.startswith("github.com/"):
        return False
    negative_filter = partition_filter.startswith("-repo:")
    repository_pattern = partition_filter.split(":", 1)[1]
    pattern_matches = re.search(repository_pattern, repository) is not None
    return pattern_matches != negative_filter


def build_partition_query(
    term: str, all_hosts: bool, partition_id: str, search_timeout_seconds: int = 10,
) -> str:
    """Build a bounded product query for one disjoint repository partition.

    Keep the existing literal identifier boundaries, public visibility, and
    inclusion of forks and archives. Partition GitHub repositories by the first
    character of their owner. A complementary remainder captures other owner
    prefixes and, when all hosts are selected, non-GitHub repositories.

    Args:
        term: Nonempty, unpadded product identifier without control characters.
        all_hosts: Include other public hosts in the remainder partition.
        partition_id: Identifier from REPOSITORY_PARTITIONS.
        search_timeout_seconds: Server search duration from one to 60 seconds.

    Returns:
        Exact Sourcegraph query, including the partition and server timeout.

    Raises:
        ValueError: If the term, host scope, partition, or timeout is invalid.
    """

    if (not isinstance(term, str) or not term or term != term.strip()
            or any(ord(character) < 32 for character in term)):
        raise ValueError("Partition search term must be nonempty, unpadded text without control characters.")
    if type(all_hosts) is not bool:
        raise ValueError("Partition host scope must be a boolean.")
    if type(search_timeout_seconds) is not int or not 1 <= search_timeout_seconds <= 60:
        raise ValueError("Partition search timeout must be an integer from 1 to 60 seconds.")
    partition_filter = next(
        (partition["filter"] for partition in REPOSITORY_PARTITIONS if partition["id"] == partition_id),
        None,
    )
    if partition_filter is None:
        raise ValueError(f"Unknown repository partition: {partition_id!r}.")
    legacy_query = build_sourcegraph_product_query(term, all_hosts, collect_all=True)
    query_without_timeout = legacy_query.removesuffix(" timeout:60s")
    return f"{query_without_timeout} timeout:{search_timeout_seconds}s {partition_filter}"


def partition_catalog_plan(
    legacy_plan: dict[str, Any], search_timeout_seconds: int = 10,
) -> dict[str, Any]:
    """Expand a validated legacy catalog plan into independent partition jobs.

    Copy the plan and preserve all catalog metadata and product associations.
    Each existing term becomes nine jobs, retaining source term order followed
    by the fixed partition order. Each term's copied product metadata is shared
    between its new partition jobs to avoid nine in-memory copies. Treat these
    associations as immutable. Do not mutate the input or access the network.

    Args:
        legacy_plan: Schema-version-1 batch plan containing unique term queries.
        search_timeout_seconds: Server search duration from one to 60 seconds.

    Returns:
        Validated schema-version-2 plan with deterministic partition query IDs.

    Raises:
        ValueError: If the input is not a valid legacy plan or timeout is invalid.
    """

    # Import here so the validator can use the query builder without a cycle.
    from nasa_eo_search.batch_plan import validate_saved_batch_plan

    validate_saved_batch_plan(legacy_plan)
    if legacy_plan["schema_version"] != 1:
        raise ValueError("Partition expansion requires a schema-version-1 batch plan.")
    partitioned_plan = deepcopy({key: value for key, value in legacy_plan.items() if key != "jobs"})
    partitioned_plan.update({
        "schema_version": 2,
        "search_strategy": PARTITION_SEARCH_STRATEGY,
        "search_timeout_seconds": search_timeout_seconds,
        "jobs": [],
    })
    for legacy_job in legacy_plan["jobs"]:
        copied_job_metadata = deepcopy(legacy_job)
        for partition in REPOSITORY_PARTITIONS:
            query = build_partition_query(
                legacy_job["term"], legacy_plan["all_hosts"], partition["id"], search_timeout_seconds,
            )
            partitioned_job = dict(copied_job_metadata)
            partitioned_job.update({
                "partition_id": partition["id"],
                "query": query,
                "search_id": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            })
            partitioned_plan["jobs"].append(partitioned_job)
    validate_saved_batch_plan(partitioned_plan)
    return partitioned_plan

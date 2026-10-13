"""Freeze independent review selections and compare known retrieval examples."""

from collections import Counter, defaultdict
import re
from typing import Any
from urllib.parse import urlsplit

from nasa_eo_search.batch_files import COMPLETED_SEARCH
from nasa_eo_search.evidence_inputs import serialize_evidence


SELECTION_COLUMNS = ("repository", "provider", "short_name", "split", "sample_kind", "family")
KNOWN_EXAMPLE_COLUMNS = (
    "repository", "provider", "short_name", "source_url", "discovery_basis", "miss_reason", "notes",
)
RETRIEVAL_COLUMNS = (
    *KNOWN_EXAMPLE_COLUMNS, "retrieval_status", "group_id", "file_count", "search_started",
    "search_complete", "query_count", "query_statuses", "queries", "query_warnings",
    "search_scope", "coverage_note",
)
REVIEW_SPLITS = frozenset(("regression", "development", "test"))
SAMPLE_KINDS = frozenset(("ordinary", "challenge"))
DISCOVERY_BASES = frozenset(("independent_documentation", "domain_knowledge"))
MISS_REASONS = frozenset(("", "query_signature", "index_coverage", "search_limits", "revision_access", "unknown"))
RETRIEVAL_STATUSES = ("found", "searched_zero_not_observed", "incomplete", "not_searched")
STARTED_SEARCH_STATUSES = frozenset((COMPLETED_SEARCH, "incomplete", "failed", "interrupted", "running"))


def require_review_text(value: Any, field_name: str, allow_blank: bool = False) -> str:
    """Validate a reviewer-controlled text field without silently coercing values.

    Args:
        value: Field value from a decoded group or CSV row.
        field_name: Name to include in a validation error.
        allow_blank: Whether empty or whitespace-only text is accepted.

    Returns:
        Text with surrounding whitespace removed.

    Raises:
        ValueError: If the value is not text or a required value is blank.
    """
    if not isinstance(value, str) or (not allow_blank and not value.strip()):
        raise ValueError(f"{field_name} must be {'text' if allow_blank else 'nonempty text'}.")
    return value.strip()


def validate_review_repository(value: Any) -> str:
    """Require the canonical public GitHub repository format used by saved searches.

    Args:
        value: Repository identifier such as github.com/owner/project.

    Returns:
        Trimmed identifier with its original owner/repository letter case.

    Raises:
        ValueError: If the value is not a GitHub repository identity.
    """
    repository = require_review_text(value, "repository")
    if (not re.fullmatch(r"github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository)
            or repository.rsplit("/", 1)[-1] in (".", "..")):
        raise ValueError("repository must use github.com/owner/repository without a branch, path, or URL scheme.")
    return repository


def read_review_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    """Build a comparable repository/product identity from a flat review record.

    Args:
        row: Record containing repository, provider, and short_name.

    Returns:
        Case-insensitive GitHub identity and case-sensitive catalog product key.

    Raises:
        ValueError: If a required identity field is missing or malformed.
    """
    return (
        validate_review_repository(row.get("repository")).casefold(),
        require_review_text(row.get("provider"), "provider"),
        require_review_text(row.get("short_name"), "short_name"),
    )


def validate_review_columns(row: Any, allowed: tuple[str, ...], required: tuple[str, ...]) -> None:
    """Reject misspelled or missing CSV columns before freezing a review sample.

    Args:
        row: Parsed CSV row or equivalent dictionary.
        allowed: Supported column names.
        required: Required column names, including fields whose values may be blank.

    Raises:
        ValueError: If the row is not an object or has missing/unknown columns.
    """
    if not isinstance(row, dict) or set(row) - set(allowed) or set(required) - set(row):
        raise ValueError(f"Review CSV requires columns {', '.join(required)}; allowed columns: {', '.join(allowed)}.")


def build_review_selection(
    groups: list[dict[str, Any]], selection_rows: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Freeze explicit review membership without splitting related repositories.

    The default treats all supplied groups as ordinary regression examples. An
    explicit selection must name its family for every row: offline preparation
    cannot discover forks or related projects. Family comparisons ignore letter
    case; one repository cannot have conflicting family names across products.

    Args:
        groups: Flat group snapshots containing group_id, repository, provider,
            short_name, and evidence_revision.
        selection_rows: Optional six-column reviewer selection. Only selected
            rows are returned; no random sampling or assignment is performed.

    Returns:
        Repository/product-sorted frozen entries including source revision,
        split, sample kind, and reviewer-supplied family.

    Raises:
        ValueError: If identities are invalid, selections duplicate or name
            unknown groups, or related repositories cross review splits.
    """
    groups_by_identity = {}
    group_ids = set()
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("Review groups must be objects.")
        identity = read_review_identity(group)
        group_id = require_review_text(group.get("group_id"), "group_id")
        evidence_revision = require_review_text(group.get("evidence_revision"), "evidence_revision")
        if identity in groups_by_identity or group_id in group_ids:
            raise ValueError("Review groups contain a duplicate repository/product or group_id.")
        group_ids.add(group_id)
        groups_by_identity[identity] = {
            "group_id": group_id, "repository": validate_review_repository(group["repository"]),
            "provider": identity[1], "short_name": identity[2], "evidence_revision": evidence_revision,
        }
    if selection_rows is None:
        selection_rows = [{"repository": group["repository"], "provider": group["provider"],
                           "short_name": group["short_name"], "split": "regression",
                           "sample_kind": "ordinary", "family": group["repository"]}
                          for group in groups_by_identity.values()]
    selected_identities = set()
    repository_families = {}
    family_splits = {}
    entries = []
    for selection in selection_rows:
        validate_review_columns(selection, SELECTION_COLUMNS, SELECTION_COLUMNS)
        identity = read_review_identity(selection)
        if identity in selected_identities:
            raise ValueError("Selection contains a duplicate repository/product.")
        if identity not in groups_by_identity:
            raise ValueError(f"Selected repository/product is absent from the supplied evidence: {identity}.")
        selected_identities.add(identity)
        split = require_review_text(selection["split"], "split")
        sample_kind = require_review_text(selection["sample_kind"], "sample_kind")
        family = require_review_text(selection["family"], "family")
        if split not in REVIEW_SPLITS or sample_kind not in SAMPLE_KINDS:
            raise ValueError("split must be regression/development/test and sample_kind must be ordinary/challenge.")
        family_identity = family.casefold()
        if identity[0] in repository_families and repository_families[identity[0]] != family_identity:
            raise ValueError("Every product from the same repository must use the same family.")
        if family_identity in family_splits and family_splits[family_identity] != split:
            raise ValueError("A repository family cannot cross regression, development, and test splits.")
        repository_families[identity[0]] = family_identity
        family_splits[family_identity] = split
        entries.append({**groups_by_identity[identity], "split": split, "sample_kind": sample_kind, "family": family})
    return sorted(entries, key=lambda entry: read_review_identity(entry))


def validate_known_example(row: dict[str, str]) -> dict[str, str]:
    """Validate a known example declared independently of the measured searches.

    Args:
        row: Known-example CSV record; miss_reason and notes may be omitted.

    Returns:
        Normalized metadata without an inferred cause for a missing match.

    Raises:
        ValueError: If identity, source URL, independent basis, or reason is invalid.
    """
    validate_review_columns(row, KNOWN_EXAMPLE_COLUMNS, KNOWN_EXAMPLE_COLUMNS[:5])
    identity = read_review_identity(row)
    source_url = require_review_text(row["source_url"], "source_url")
    try:
        parsed_url = urlsplit(source_url)
        valid_url = (parsed_url.scheme in ("http", "https") and bool(parsed_url.hostname)
                     and parsed_url.username is None and parsed_url.password is None
                     and not any(character.isspace() or ord(character) < 32 for character in source_url))
        unused_port = parsed_url.port
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("source_url must be an HTTP(S) reference without embedded credentials.")
    discovery_basis = require_review_text(row["discovery_basis"], "discovery_basis")
    if discovery_basis not in DISCOVERY_BASES:
        raise ValueError("discovery_basis must be independent_documentation or domain_knowledge, not the measured search results.")
    miss_reason = require_review_text(row.get("miss_reason", ""), "miss_reason", allow_blank=True)
    if miss_reason not in MISS_REASONS:
        raise ValueError("miss_reason must be blank/query_signature/index_coverage/search_limits/revision_access/unknown.")
    return {
        "repository": validate_review_repository(row["repository"]), "provider": identity[1], "short_name": identity[2],
        "source_url": source_url, "discovery_basis": discovery_basis, "miss_reason": miss_reason,
        "notes": require_review_text(row.get("notes", ""), "notes", allow_blank=True),
    }


def compare_known_examples(plan: dict[str, Any], known_rows: list[dict[str, str]]) -> dict[str, Any]:
    """Measure known-example retrieval using saved matches and explicit denominators.

    Found means a repository/product has saved file matches, regardless of its
    later model category. Completed searches without a hit are not proof that
    a repository lacks NASA use. Query metadata is needed to establish completion
    even for zero-hit products; missing query metadata remains unknown coverage.

    Args:
        plan: Validated classifier plan containing groups and, when available,
            source_product_queries with their latest saved search statuses.
        known_rows: Independently assembled examples, not sampled from these hits.

    Returns:
        rows suitable for the retrieval CSV and a summary with known-example,
        confirmed-started-search, and completed-search numerators/denominators.
        No network requests occur and no retrieval miss cause is inferred.

    Raises:
        ValueError: If known examples are duplicated or their metadata is invalid.
    """
    matches_by_identity = {}
    for group in plan.get("groups", []):
        product = group.get("product", {})
        identity = read_review_identity({"repository": group.get("repository"),
                                         "provider": product.get("provider"), "short_name": product.get("short_name")})
        matches_by_identity[identity] = group
    queries_by_product = defaultdict(list)
    source_queries = plan.get("source_product_queries", [])
    for query in source_queries if isinstance(source_queries, list) else []:
        if isinstance(query, dict) and isinstance(query.get("provider"), str) and isinstance(query.get("short_name"), str):
            queries_by_product[(query["provider"], query["short_name"])].append(query)
    rows = []
    seen_identities = set()
    for original_row in known_rows:
        known = validate_known_example(original_row)
        identity = read_review_identity(known)
        if identity in seen_identities:
            raise ValueError("Known examples contain a duplicate repository/product.")
        seen_identities.add(identity)
        group = matches_by_identity.get(identity, {})
        files = group.get("files", [])
        file_count = len(files) if isinstance(files, list) else 0
        found = file_count > 0
        queries = queries_by_product.get(identity[1:], [])
        statuses = [query.get("status") if isinstance(query.get("status"), str) else "unknown" for query in queries]
        search_started = any(status in STARTED_SEARCH_STATUSES for status in statuses)
        search_complete = bool(statuses) and all(status == COMPLETED_SEARCH for status in statuses)
        if found:
            retrieval_status = "found"
        elif not queries:
            retrieval_status = "not_searched"
        elif search_complete:
            retrieval_status = "searched_zero_not_observed"
        else:
            retrieval_status = "incomplete"
        if not queries:
            coverage_note = "No saved product-query metadata; search coverage cannot be established."
        elif not search_complete:
            coverage_note = "One or more selected product queries are unfinished or have unknown status."
        else:
            coverage_note = "Selected product queries finished without reported limits; coverage is limited to the search service's index."
        if found and not search_complete:
            coverage_note += " The saved file match still establishes retrieval of this known example."
        query_records = [{field: query.get(field, "") for field in ("search_id", "term", "query", "status", "warnings", "error")}
                         for query in queries]
        warning_texts = [warning for query in queries
                         for warning in (query.get("warnings", []) if isinstance(query.get("warnings", []), list) else [])
                         if isinstance(warning, str)]
        rows.append({
            **known, "retrieval_status": retrieval_status, "group_id": group.get("group_id", ""),
            "file_count": file_count, "search_started": search_started, "search_complete": search_complete,
            "query_count": len(queries), "query_statuses": "; ".join(statuses),
            "queries": serialize_evidence(query_records), "query_warnings": "; ".join(dict.fromkeys(warning_texts)),
            "search_scope": plan.get("search_scope", "Unknown saved search scope"), "coverage_note": coverage_note,
        })
    status_counts = Counter(row["retrieval_status"] for row in rows)
    found_count = status_counts["found"]
    searched_rows = [row for row in rows if row["search_started"]]
    searched_found = sum(row["retrieval_status"] == "found" for row in searched_rows)
    complete_rows = [row for row in rows if row["search_complete"]]
    complete_found = sum(row["retrieval_status"] == "found" for row in complete_rows)
    return {"rows": rows, "summary": {
        "known_examples": len(rows), "found": found_count,
        "known_example_retrieval_rate": found_count / len(rows) if rows else None,
        "searched_examples": len(searched_rows), "searched_found": searched_found,
        "searched_known_example_retrieval_rate": searched_found / len(searched_rows) if searched_rows else None,
        "complete_search_examples": len(complete_rows), "complete_search_found": complete_found,
        "complete_search_known_example_retrieval_rate": complete_found / len(complete_rows) if complete_rows else None,
        "status_counts": {status: status_counts[status] for status in RETRIEVAL_STATUSES},
        "searched_denominator": "Known examples with at least one saved query status confirming a started attempt; pending/unknown-only queries are excluded.",
        "interpretation": "Known-example retrieval only, not GitHub-wide recall or a measure of classification accuracy. Independence and miss causes are reviewer declarations.",
    }}

"""Build and validate saved search plans from reviewed catalog selections."""

import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from nasa_eo_search.catalog_csv import build_product_csv_row, require_catalog_text
from nasa_eo_search.repo_preview import build_sourcegraph_product_query
from nasa_eo_search.sourcegraph import DEFAULT_SOURCEGRAPH_ENDPOINT


def identify_search_query(query: str) -> str:
    """Create a stable filesystem-safe identifier for one exact search query.

    Args:
        query: Full Sourcegraph query including its scope and limits.

    Returns:
        The query's hexadecimal SHA-256 digest.
    """

    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def build_batch_search_plan(
    catalog_directory: Path, selection_path: Path, all_hosts: bool, max_searches: int,
) -> dict[str, Any]:
    """Validate selected product terms and group shared terms into search jobs.

    Read provider/short_name rows from a CSV; search_term defaults to short_name.
    Verify each selected term against the original catalog candidates and retain
    its field/collection provenance. Exact shared terms produce one query with
    multiple product associations. Validate everything before a search is started.

    Args:
        catalog_directory: Saved catalog containing products.jsonl and summary.json.
        selection_path: Reviewed selection CSV, optionally with other catalog columns.
        all_hosts: Include all public hosts indexed by Sourcegraph.
        max_searches: Maximum allowed number of distinct selected queries.

    Returns:
        A serializable plan containing jobs, associations, scope, and source metadata.

    Raises:
        ValueError: For invalid selections, catalog metadata, or the query limit.
        OSError: If a source file cannot be read.
        csv.Error: If the selection file contains malformed CSV.
    """

    if max_searches < 1:
        raise ValueError("max_searches must be positive.")
    selections: set[tuple[str, str, str]] = set()
    with selection_path.open(encoding="utf-8-sig", newline="") as selection_input:
        reader = csv.DictReader(selection_input, strict=True)
        if not reader.fieldnames or not {"provider", "short_name"}.issubset(reader.fieldnames):
            raise ValueError("Selection CSV requires provider and short_name columns.")
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("Selection CSV contains duplicate column names.")
        for line_number, selection in enumerate(reader, start=2):
            if None in selection:
                raise ValueError(f"Selection row {line_number} has extra fields.")
            provider = require_catalog_text(selection.get("provider"), f"row {line_number} provider").strip()
            short_name = require_catalog_text(selection.get("short_name"), f"row {line_number} short_name").strip()
            term = (selection.get("search_term") or short_name).strip()
            if not term or any(ord(character) < 32 for character in term):
                raise ValueError(f"Selection row {line_number} has a blank or control-character search term.")
            selections.add((provider, short_name, term))
    if not selections:
        raise ValueError("Select at least one product.")
    if len({term for _, _, term in selections}) > max_searches:
        raise ValueError(f"Selection exceeds the {max_searches}-query limit; use --max-searches to change it.")
    with (catalog_directory / "summary.json").open(encoding="utf-8-sig") as summary_input:
        catalog_summary = json.load(summary_input)
    if (not isinstance(catalog_summary, dict) or type(catalog_summary.get("schema_version")) is not int
            or catalog_summary["schema_version"] != 1
            or catalog_summary.get("status") not in ("complete", "incomplete", "failed", "interrupted")):
        raise ValueError("Use a finished schema-version-1 catalog; wait for active runs to finish.")
    selected_keys = {(provider, short_name) for provider, short_name, _ in selections}
    selected_products = {}
    seen_products = set()
    with (catalog_directory / "products.jsonl").open(encoding="utf-8-sig") as products_input:
        for product_line in products_input:
            if not product_line.strip():
                continue
            product = json.loads(product_line)
            if not isinstance(product, dict):
                raise ValueError("Catalog products must be objects.")
            key = tuple(require_catalog_text(product.get(field), field) for field in ("provider", "short_name"))
            if key in seen_products:
                raise ValueError(f"Duplicate catalog product: {key}.")
            seen_products.add(key)
            if key in selected_keys:
                build_product_csv_row(product)
                selected_products[key] = product
    if type(catalog_summary.get("products_saved")) is not int or len(seen_products) != catalog_summary["products_saved"]:
        raise ValueError("Catalog product count disagrees with summary.json.")
    jobs_by_term: dict[str, dict[str, Any]] = {}
    for provider, short_name, term in sorted(selections):
        product = selected_products.get((provider, short_name))
        if product is None:
            raise ValueError(f"Selected product is absent from catalog: {provider}/{short_name}.")
        candidates = {candidate["term"]: candidate for candidate in product["candidate_signatures"]}
        if term not in candidates:
            raise ValueError(f"{term!r} is not a catalog candidate for {provider}/{short_name}.")
        query = build_sourcegraph_product_query(term, all_hosts, collect_all=True)
        job = jobs_by_term.setdefault(term, {
            "search_id": identify_search_query(query), "term": term, "query": query, "products": [],
        })
        job["products"].append({"provider": provider, "short_name": short_name,
                                "candidate": candidates[term]})
    return {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "catalog_directory": str(catalog_directory.resolve()),
        "selection_path": str(selection_path.resolve()), "catalog_summary": catalog_summary,
        "all_hosts": all_hosts, "endpoint": DEFAULT_SOURCEGRAPH_ENDPOINT,
        "scope": "Sourcegraph indexed public content, including forks and archives",
        "jobs": list(jobs_by_term.values()),
    }


def validate_saved_batch_plan(plan: Any) -> None:
    """Check that a resumed plan contains safe, reproducible query jobs.

    Rebuild each query and identifier from its saved term and scope. This prevents
    malformed job paths and detects edited queries before any network requests.

    Args:
        plan: Decoded plan.json, independent of the original catalog files.

    Raises:
        ValueError: If the plan schema, query identities, or associations are invalid.
    """

    if (not isinstance(plan, dict) or type(plan.get("schema_version")) is not int
            or plan["schema_version"] != 1 or type(plan.get("all_hosts")) is not bool
            or plan.get("endpoint") != DEFAULT_SOURCEGRAPH_ENDPOINT):
        raise ValueError("Invalid batch plan schema or endpoint.")
    require_catalog_text(plan.get("scope"), "plan.scope")
    catalog_summary = plan.get("catalog_summary")
    if not isinstance(catalog_summary, dict) or catalog_summary.get("status") not in (
        "complete", "incomplete", "failed", "interrupted",
    ):
        raise ValueError("Saved plan requires source catalog status.")
    jobs = plan.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("Batch plan requires a nonempty jobs list.")
    seen_ids = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("Each search job must be an object.")
        term = require_catalog_text(job.get("term"), "job.term")
        if term != term.strip() or any(ord(character) < 32 for character in term):
            raise ValueError("Invalid search term in saved plan.")
        query = build_sourcegraph_product_query(term, plan["all_hosts"], collect_all=True)
        search_id = identify_search_query(query)
        if job.get("query") != query or job.get("search_id") != search_id or search_id in seen_ids:
            raise ValueError("Saved query or identifier differs from its term/scope, or is duplicated.")
        seen_ids.add(search_id)
        products = job.get("products")
        if not isinstance(products, list) or not products:
            raise ValueError("Each job needs product associations.")
        seen_products = set()
        for product in products:
            if not isinstance(product, dict):
                raise ValueError("Each product association must be an object.")
            key = tuple(require_catalog_text(product.get(field), field) for field in ("provider", "short_name"))
            candidate = product.get("candidate")
            if key in seen_products or not isinstance(candidate, dict) or candidate.get("term") != term:
                raise ValueError("Invalid or duplicate product association.")
            seen_products.add(key)
            require_catalog_text(candidate.get("review_status"), "candidate.review_status")
            sources = candidate.get("sources")
            if not isinstance(sources, list) or not sources:
                raise ValueError("Candidate requires source provenance.")
            for source in sources:
                if not isinstance(source, dict):
                    raise ValueError("Candidate source must be an object.")
                for field in ("concept_id", "source_field"):
                    require_catalog_text(source.get(field), f"candidate source {field}")

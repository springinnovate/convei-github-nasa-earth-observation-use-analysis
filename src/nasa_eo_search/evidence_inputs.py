"""Group saved batch evidence by repository and catalog product."""

import hashlib
import json
from pathlib import Path
from typing import Any

from nasa_eo_search.batch_files import COMPLETED_SEARCH, inspect_search_attempt, list_search_attempts
from nasa_eo_search.batch_plan import validate_saved_batch_plan
from nasa_eo_search.catalog_csv import build_product_csv_row
from nasa_eo_search.repo_preview import format_repository_match


def serialize_evidence(value: Any) -> str:
    """Serialize evidence deterministically for request sizing and identities.

    Args:
        value: JSON-compatible evidence or request data.

    Returns:
        Compact, sorted-key JSON text with Unicode preserved.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def identify_evidence(value: Any) -> str:
    """Hash evidence content into a stable, filesystem-safe identifier.

    Args:
        value: JSON-compatible evidence or request data.

    Returns:
        A full hexadecimal SHA-256 digest.
    """
    return hashlib.sha256(serialize_evidence(value).encode("utf-8")).hexdigest()


def read_evidence_json(path: Path) -> dict[str, Any]:
    """Read a saved JSON object with clear errors for other root types.

    Args:
        path: UTF-8 JSON file to read.

    Returns:
        The decoded object.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If the file is not a JSON object.
    """
    with path.open(encoding="utf-8-sig") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def read_product_evidence_groups(batch_directory: Path, catalog_directory: Path) -> dict[str, Any]:
    """Combine all readable saved attempts without counting repeated file hits.

    Keep provider/product associations separate, union matching lines by immutable
    repository/commit/file identity, and retain every attempt's provenance. Search
    coverage comes from the latest attempt of every selected query for a product,
    including queries without matches. No network requests or source writes occur.

    Args:
        batch_directory: Finished or interrupted nasa-repo-batch output directory.
        catalog_directory: Saved catalog supplying product descriptions.

    Returns:
        Product/repository groups and source catalog/search metadata.

    Raises:
        OSError: If required source files cannot be read.
        ValueError: If source metadata, product associations, or matches are invalid.
    """
    batch_plan = read_evidence_json(batch_directory / "plan.json")
    validate_saved_batch_plan(batch_plan)
    batch_summary = read_evidence_json(batch_directory / "summary.json")
    if batch_summary.get("status") not in ("complete", "incomplete", "failed", "interrupted"):
        raise ValueError("Wait for the source batch to finish or stop it before preparing evidence.")
    catalog_summary = read_evidence_json(catalog_directory / "summary.json")
    if (type(catalog_summary.get("schema_version")) is not int
            or catalog_summary["schema_version"] != 1
            or type(catalog_summary.get("products_saved")) is not int
            or catalog_summary["products_saved"] < 0 or catalog_summary.get("status") not in (
        "complete", "incomplete", "failed", "interrupted",
    )):
        raise ValueError("Use a finished schema-version-1 catalog.")
    selected_keys = {(product["provider"], product["short_name"])
                     for job in batch_plan["jobs"] for product in job["products"]}
    products = {}
    seen_keys = set()
    with (catalog_directory / "products.jsonl").open(encoding="utf-8-sig") as source:
        for source_line in source:
            if not source_line.strip():
                continue
            product = json.loads(source_line)
            if not isinstance(product, dict):
                raise ValueError("Catalog products must be objects.")
            product_key = (product.get("provider"), product.get("short_name"))
            if not all(isinstance(value, str) and value.strip() for value in product_key):
                raise ValueError("Catalog products require provider and short_name.")
            if product_key in seen_keys:
                raise ValueError(f"Duplicate catalog product: {product_key}.")
            seen_keys.add(product_key)
            if product_key in selected_keys:
                build_product_csv_row(product)
                products[product_key] = product
    if len(seen_keys) != catalog_summary.get("products_saved") or selected_keys - products.keys():
        raise ValueError("Catalog count is inconsistent or selected products are missing.")

    groups = {}
    product_searches: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for job in batch_plan["jobs"]:
        attempts = list_search_attempts(batch_directory, job["search_id"])
        inspected = [(attempt, *inspect_search_attempt(attempt, job["query"])) for attempt in attempts]
        latest_summary = inspected[-1][1] if inspected else {"status": "pending"}
        for association in job["products"]:
            product_key = (association["provider"], association["short_name"])
            known_ids = {collection["concept_id"] for collection in products[product_key]["collections"]}
            if any(source["concept_id"] not in known_ids for source in association["candidate"]["sources"]):
                raise ValueError(f"Catalog no longer contains the selected collections for {product_key}.")
            product_searches.setdefault(product_key, []).append({
                "search_id": job["search_id"], "term": job["term"], "query": job["query"],
                "status": latest_summary["status"], "warnings": latest_summary.get("warnings", []),
                "error": latest_summary.get("error", ""),
            })
            for attempt, attempt_summary, matches in inspected:
                for raw_match in matches:
                    match = format_repository_match(raw_match)
                    if match["commit"] is not None and not isinstance(match["commit"], str):
                        raise ValueError("Match commit must be text or null.")
                    group_key = (match["repository"], *product_key)
                    group = groups.setdefault(group_key, {
                        "group_id": identify_evidence(group_key), "repository": match["repository"],
                        "product": products[product_key], "files": {},
                    })
                    file_key = (match["commit"], match["file"])
                    file_record = group["files"].setdefault(file_key, {**match, "matching_lines": [], "sources": []})
                    for matching_line in match["matching_lines"]:
                        if matching_line not in file_record["matching_lines"]:
                            file_record["matching_lines"].append(matching_line)
                    provenance = {
                        "search_id": job["search_id"], "term": job["term"], "attempt": attempt.name,
                        "status": attempt_summary["status"], "latest_attempt": attempt == attempts[-1],
                        "error": attempt_summary.get("error", ""),
                    }
                    if provenance not in file_record["sources"]:
                        file_record["sources"].append(provenance)
    ordered_groups = []
    for group_key, group in sorted(groups.items()):
        group["files"] = sorted(group["files"].values(), key=lambda record: (record["file"], record["commit"] or ""))
        group["searches"] = product_searches[group_key[1:]]
        group["search_complete"] = all(search["status"] == COMPLETED_SEARCH for search in group["searches"])
        group["warnings"] = []
        if not group["search_complete"]:
            group["warnings"].append("One or more selected product searches are incomplete.")
        if catalog_summary["status"] != "complete" or batch_plan["catalog_summary"]["status"] != "complete":
            group["warnings"].append("The source catalog is partial.")
        if any(not any(source["latest_attempt"] for source in record["sources"]) for record in group["files"]):
            group["warnings"].append("Some evidence appears only in historical search attempts.")
        if any(source["status"] != COMPLETED_SEARCH for record in group["files"] for source in record["sources"]):
            group["warnings"].append("Some evidence came from partial or damaged search attempts; inspect provenance.")
        if len({record["commit"] for record in group["files"]}) > 1:
            group["warnings"].append("Evidence spans multiple commits; it is not a single repository snapshot.")
        ordered_groups.append(group)
    return {
        "groups": ordered_groups, "catalog_status": catalog_summary["status"],
        "search_scope": batch_plan["scope"], "selected_products": len(selected_keys),
        "catalog_summary": catalog_summary, "source_batch_summary": batch_summary,
        "source_product_queries": [
            {"provider": product_key[0], "short_name": product_key[1], **search}
            for product_key, searches in sorted(product_searches.items()) for search in searches
        ],
        "batch_directory": str(batch_directory.resolve()), "catalog_directory": str(catalog_directory.resolve()),
    }

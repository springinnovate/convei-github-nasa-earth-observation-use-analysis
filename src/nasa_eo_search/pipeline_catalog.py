"""Turn saved NASA catalog metadata into an inventory and automatic search plan."""

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from nasa_eo_search.batch_plan import identify_search_query, validate_saved_batch_plan
from nasa_eo_search.catalog_csv import build_product_csv_row, protect_spreadsheet_cell, read_catalog_csv_rows
from nasa_eo_search.evidence_inputs import read_evidence_json
from nasa_eo_search.repo_preview import build_sourcegraph_product_query
from nasa_eo_search.sourcegraph import DEFAULT_SOURCEGRAPH_ENDPOINT


DATASET_COLUMNS = (
    "provider", "short_name", "dataset_name", "description", "source_url", "known_from",
    "candidate_search_terms", "candidate_sources",
)


def read_pipeline_catalog(catalog_directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate a finished saved catalog and return its unescaped source metadata.

    Reuse the catalog export reader's schema, provenance, duplicate, and count
    checks. Then read original objects rather than reusing spreadsheet-protected
    strings: search terms must remain exact and CSV protection must happen once.
    No metadata is downloaded and no source files are changed.

    Args:
        catalog_directory: Directory containing summary.json and products.jsonl.

    Returns:
        Catalog summary and original product records in their saved order.

    Raises:
        OSError: If a required source file cannot be read.
        ValueError: If metadata is invalid, active, duplicated, or inconsistent.
    """
    catalog_status, validated_rows = read_catalog_csv_rows(catalog_directory)
    expected_count = len(validated_rows)
    del validated_rows
    summary = read_evidence_json(catalog_directory / "summary.json")
    if summary.get("status") != catalog_status or summary.get("products_saved") != expected_count:
        raise ValueError("Catalog changed during validation; wait for it to finish and retry.")
    products = []
    identities = set()
    with (catalog_directory / "products.jsonl").open(encoding="utf-8-sig") as catalog_input:
        for line_number, product_line in enumerate(catalog_input, start=1):
            if not product_line.strip():
                continue
            try:
                product = json.loads(product_line)
                validated_product = build_product_csv_row(product)
            except ValueError as error:
                raise ValueError(f"products.jsonl line {line_number}: {error}") from error
            identity = (validated_product["provider"], validated_product["short_name"])
            if identity in identities:
                raise ValueError(f"Duplicate catalog product: {identity}.")
            identities.add(identity)
            products.append(product)
    if len(products) != expected_count:
        raise ValueError("Catalog product count changed during validation; retry with a finished catalog.")
    return summary, products


def build_dataset_name(product: dict[str, Any]) -> str:
    """Join distinct collection titles into one readable product-family name.

    Args:
        product: Validated catalog product containing one or more collections.

    Returns:
        Unique titles joined with semicolons in their original collection order.
    """
    return "; ".join(dict.fromkeys(collection["title"] for collection in product["collections"]))


def build_dataset_row(product: dict[str, Any], summary: dict[str, Any]) -> dict[str, str]:
    """Explain a collected dataset and the original fields supplying search terms.

    Args:
        product: Validated catalog product with version and candidate provenance.
        summary: Validated source catalog summary, including its actual query URL.

    Returns:
        Unescaped inventory fields ready for exclusive CSV export. All saved
        terms are retained; URL and filename patterns are never invented.
    """
    descriptions = list(dict.fromkeys(collection["summary"] for collection in product["collections"] if collection.get("summary")))
    collection_labels = [collection["concept_id"] + (f" (version {collection['version']})" if collection.get("version") else "")
                         for collection in product["collections"]]
    candidate_labels = []
    for candidate in product["candidate_signatures"]:
        sources = "; ".join(f"{source['concept_id']} ({source['source_field']})" for source in candidate["sources"])
        candidate_labels.append(f"{candidate['term']} [{candidate['review_status']}]: {sources}")
    known_from = ("Saved NASA Common Metadata Repository (CMR) collection metadata. "
                  f"Scope: {summary['scope']}. Catalog status: {summary['status']}. "
                  f"Collections: {'; '.join(collection_labels)}.")
    if summary.get("warnings"):
        known_from += " Catalog warnings: " + "; ".join(summary["warnings"])
    if summary.get("error"):
        known_from += " Catalog error: " + summary["error"]
    return {
        "provider": product["provider"], "short_name": product["short_name"],
        "dataset_name": build_dataset_name(product), "description": "\n\n".join(descriptions),
        "source_url": summary["source_url"], "known_from": known_from,
        "candidate_search_terms": "\n".join(candidate["term"] for candidate in product["candidate_signatures"]),
        "candidate_sources": "\n".join(candidate_labels),
    }


def export_dataset_table(catalog_directory: Path, output_path: Path) -> list[dict[str, str]]:
    """Write a new dataset inventory from saved catalog metadata without refetching.

    Each provider/short-name product gets one row with a name, description, source
    URL, catalog provenance, and metadata-derived candidate search terms. Existing
    files are never replaced. Formula-like text is protected in the CSV only.

    Args:
        catalog_directory: Existing saved catalog to read without modifying it.
        output_path: New CSV path inside an existing parent directory.

    Returns:
        Raw row dictionaries, without spreadsheet protection prefixes.

    Raises:
        OSError: If an input cannot be read, output already exists, or writing fails.
        ValueError: If catalog validation fails.
        csv.Error: If a row cannot be serialized.
        KeyboardInterrupt: If writing is interrupted; the new partial file is removed.
    """
    summary, products = read_pipeline_catalog(catalog_directory)
    rows = [build_dataset_row(product, summary) for product in products]
    csv_output = output_path.open("x", encoding="utf-8-sig", newline="")
    try:
        with csv_output:
            writer = csv.DictWriter(csv_output, fieldnames=DATASET_COLUMNS, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            for row in rows:
                writer.writerow({column: protect_spreadsheet_cell(row[column]) for column in DATASET_COLUMNS})
    except (OSError, csv.Error, KeyboardInterrupt):
        output_path.unlink()
        raise
    return rows


def build_catalog_search_plan(
    catalog_directory: Path, short_names: list[str] | None = None, all_hosts: bool = False,
) -> dict[str, Any]:
    """Plan searches for every saved candidate term without a selection spreadsheet.

    Default to all collected products; an exact short-name filter can select a
    pilot across providers. Identical terms share one query with every product
    association preserved. Use the existing literal identifier-boundary search
    builder, including forks and archives, without a hidden twenty-query cap.

    Args:
        catalog_directory: Saved metadata catalog to validate and read.
        short_names: Optional nonempty list of exact product short names.
        all_hosts: Include all indexed public hosts instead of GitHub alone.

    Returns:
        Validated schema-version-one batch plan with report_unit=file, readable
        dataset names on associations, and original candidate provenance.

    Raises:
        OSError: If catalog source files cannot be read.
        ValueError: If the catalog, filters, or candidate terms are invalid, a
            requested product is absent, or a selected product has no candidates.
    """
    if type(all_hosts) is not bool:
        raise ValueError("all_hosts must be true or false.")
    if short_names is not None and (not isinstance(short_names, list) or not short_names
                                   or any(not isinstance(name, str) or not name.strip() for name in short_names)):
        raise ValueError("short_names must be a nonempty list of exact product short names.")
    summary, products = read_pipeline_catalog(catalog_directory)
    selected_names = set(short_names) if short_names is not None else None
    if selected_names is not None:
        missing_names = selected_names - {product["short_name"] for product in products}
        if missing_names:
            raise ValueError("Requested short names are absent from the saved catalog: " + ", ".join(sorted(missing_names)))
        products = [product for product in products if product["short_name"] in selected_names]
    jobs_by_term = {}
    for product in sorted(products, key=lambda item: (item["provider"], item["short_name"])):
        if not product["candidate_signatures"]:
            raise ValueError(f"Catalog product {product['provider']}/{product['short_name']} has no saved candidate terms.")
        seen_terms = set()
        for candidate in product["candidate_signatures"]:
            term = candidate["term"]
            if term != term.strip() or any(ord(character) < 32 for character in term):
                raise ValueError(f"Catalog product {product['provider']}/{product['short_name']} has an invalid candidate term.")
            if term in seen_terms:
                raise ValueError(f"Catalog product {product['provider']}/{product['short_name']} has duplicate candidate term {term!r}.")
            seen_terms.add(term)
            query = build_sourcegraph_product_query(term, all_hosts, collect_all=True)
            job = jobs_by_term.setdefault(term, {
                "search_id": identify_search_query(query), "term": term, "query": query, "products": [],
            })
            job["products"].append({
                "provider": product["provider"], "short_name": product["short_name"],
                "dataset_name": build_dataset_name(product), "candidate": candidate,
            })
    if not jobs_by_term:
        raise ValueError("The saved catalog has no products to search.")
    plan = {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "catalog_directory": str(catalog_directory.resolve()), "catalog_summary": summary,
        "selection_path": None, "selection_mode": "short_name_filter" if selected_names is not None else "all_collected_products",
        "selected_short_names": sorted(selected_names) if selected_names is not None else None,
        "all_hosts": all_hosts, "endpoint": DEFAULT_SOURCEGRAPH_ENDPOINT,
        "scope": ("Sourcegraph indexed public content on all hosts, including forks and archives" if all_hosts else
                  "Sourcegraph indexed public GitHub content, including forks and archives"),
        "report_unit": "file", "jobs": list(jobs_by_term.values()),
    }
    validate_saved_batch_plan(plan)
    return plan

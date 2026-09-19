"""Collect public EOSDIS catalog metadata and group versions for code searches.

EOSDIS is NASA's Earth Observing System Data and Information System. Its tagged
collections in the Common Metadata Repository (CMR) define this command's scope.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import ssl
import sys
import time
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import certifi

from nasa_eo_search.cmr import CMR_COLLECTIONS_URL, CatalogError


@dataclass
class CollectionPage:
    """Validated collection records and the metadata needed to continue paging."""

    records: list[dict[str, Any]]
    reported_hits: int
    next_cursor: str | None
    retrieved_at: str


def build_catalog_search_url(short_name: str | None, page_size: int) -> str:
    """Build the fixed query used for every page of an EOSDIS catalog run.

    Args:
        short_name: Optional exact product short name for a trial collection run.
        page_size: Number of collection records requested per response.

    Returns:
        CMR JSON URL with the EOSDIS tag and optional product filter.
    """

    query_parameters = {"tag_key": "gov.nasa.eosdis", "page_size": str(page_size)}
    if short_name is not None:
        query_parameters["short_name"] = short_name
        query_parameters["options[short_name][pattern]"] = "false"
        query_parameters["options[short_name][ignore_case]"] = "false"
    return f"{CMR_COLLECTIONS_URL}?{urlencode(query_parameters)}"


def fetch_cmr_collection_page(
    source_url: str,
    search_after_cursor: str | None,
    timeout_seconds: float,
    ca_bundle_path: str | None,
) -> CollectionPage:
    """Fetch and validate one catalog page and its continuation header.

    Pass the previous page's cursor unchanged to advance through the same query.
    Validate the total count and fields used for grouping before returning a page.

    Args:
        source_url: Fixed catalog query from build_catalog_search_url.
        search_after_cursor: Previous CMR-Search-After header, or None initially.
        timeout_seconds: Maximum wait for each socket connection or read.
        ca_bundle_path: Trusted certificate file; defaults to SSL_CERT_FILE or Certifi.

    Returns:
        Collection records, CMR's matching count, next cursor, and retrieval time.

    Raises:
        CatalogError: For network errors, timed-out searches, or invalid responses.
    """

    request_headers = {
        "Accept": "application/json", "Client-Id": "nasa-eo-catalog-collector",
        "User-Agent": "nasa-eo-search/0.1",
    }
    if search_after_cursor is not None:
        request_headers["CMR-Search-After"] = search_after_cursor
    try:
        tls_context = ssl.create_default_context(
            cafile=ca_bundle_path or os.environ.get("SSL_CERT_FILE") or certifi.where()
        )
        with urlopen(Request(source_url, headers=request_headers),
                     timeout=timeout_seconds, context=tls_context) as response:
            if response.headers.get_content_type() != "application/json":
                raise CatalogError("CMR returned a non-JSON response.")
            if any(response.headers.get(header, "").lower() == "true"
                   for header in ("CMR-Time-Out", "CMR-Timed-Out")):
                raise CatalogError("CMR reported a timed-out catalog search.")
            reported_hits_header = response.headers.get("CMR-Hits", "")
            if not reported_hits_header.isascii() or not reported_hits_header.isdecimal():
                raise CatalogError("CMR response requires a nonnegative integer CMR-Hits header.")
            reported_hits = int(reported_hits_header)
            next_cursor = response.headers.get("CMR-Search-After") or None
            payload = json.load(response)
    except HTTPError as request_error:
        raise CatalogError(f"CMR returned HTTP {request_error.code}; rerun in a new directory.") from request_error
    except (OSError, URLError, ValueError) as request_error:
        raise CatalogError(f"Could not read NASA CMR: {request_error}") from request_error
    collection_feed = payload.get("feed") if isinstance(payload, dict) else None
    records = collection_feed.get("entry") if isinstance(collection_feed, dict) else None
    if not isinstance(records, list):
        raise CatalogError("CMR response is missing its collection list.")
    for record in records:
        if not isinstance(record, dict) or any(
            not isinstance(record.get(field), str) or not record[field].strip()
            for field in ("id", "short_name", "title", "data_center")
        ):
            raise CatalogError("CMR collection requires ID, short name, title, and provider.")
    return CollectionPage(records, reported_hits, next_cursor,
                          datetime.now(timezone.utc).isoformat())


def add_collection_to_product_groups(
    product_groups: dict[tuple[str, str], dict[str, Any]],
    collection_record: dict[str, Any],
) -> None:
    """Add a collection version and its search-term provenance to a product group.

    Group by exact provider and short name. Keep each version's title, description,
    and identifiers. Combine identical candidate terms within the group while
    retaining every source field and collection; all terms start as unreviewed.
    Call once per unique collection concept ID.

    Args:
        product_groups: Mutable mapping of provider/short-name pairs to products.
        collection_record: Validated CMR collection to add to the mapping.
    """

    group_key = (collection_record["data_center"], collection_record["short_name"])
    product = product_groups.setdefault(group_key, {
        "provider": group_key[0], "short_name": group_key[1],
        "collections": [], "candidate_signatures": [],
    })
    product["collections"].append({
        "concept_id": collection_record["id"],
        "entry_id": collection_record.get("entry_id"),
        "version": collection_record.get("version_id"),
        "title": collection_record["title"],
        "summary": collection_record.get("summary"),
    })
    signatures_by_term = {item["term"]: item for item in product["candidate_signatures"]}
    for source_field in ("short_name", "entry_id", "id"):
        search_term = collection_record.get(source_field)
        if not isinstance(search_term, str) or not search_term.strip():
            continue
        if search_term not in signatures_by_term:
            signature = {"term": search_term, "review_status": "unreviewed", "sources": []}
            product["candidate_signatures"].append(signature)
            signatures_by_term[search_term] = signature
        signatures_by_term[search_term]["sources"].append({
            "concept_id": collection_record["id"], "source_field": source_field,
        })


def collect_catalog_pages(
    source_url: str,
    timeout_seconds: float,
    ca_bundle_path: str | None,
    collections_output: TextIO,
    diagnostics_output: TextIO,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Save catalog pages as they arrive and build grouped product records.

    Follow CMR Search After cursors until the query ends. Save raw records with
    retrieval provenance and flush each page. Deduplicate concept IDs for grouping,
    and flag changed totals, duplicate records, and broken pagination as incomplete.
    Completion requires a stable reported total equal to the unique saved count.

    Args:
        source_url: Fixed catalog query used for every request.
        timeout_seconds: Maximum wait for each socket connection or read.
        ca_bundle_path: Optional trusted certificate file.
        collections_output: Caller-owned destination for raw JSON Lines records.
        diagnostics_output: Caller-owned destination for progress messages.

    Returns:
        Run summary and products sorted by provider and short name. Network and
        output failures or interruption are recorded in the summary, with products
        built from the records saved so far. The caller closes the output streams.
    """

    started_at = time.monotonic()
    product_groups: dict[tuple[str, str], dict[str, Any]] = {}
    seen_concept_ids: set[str] = set()
    seen_cursors: set[str] = set()
    search_after_cursor = None
    summary: dict[str, Any] = {
        "status": "incomplete", "pages_received": 0, "records_saved": 0,
        "unique_collections": 0, "products_grouped": 0, "duplicate_records": 0,
        "initial_reported_hits": None, "last_reported_hits": None,
        "reached_end": False, "warnings": [],
    }
    try:
        while True:
            page = fetch_cmr_collection_page(
                source_url, search_after_cursor, timeout_seconds, ca_bundle_path,
            )
            summary["pages_received"] += 1
            if summary["initial_reported_hits"] is None:
                summary["initial_reported_hits"] = page.reported_hits
            if page.reported_hits != summary["initial_reported_hits"]:
                warning = "CMR's matching collection count changed during this run."
                if warning not in summary["warnings"]:
                    summary["warnings"].append(warning)
            summary["last_reported_hits"] = page.reported_hits
            for collection_record in page.records:
                print(json.dumps({
                    "source_url": source_url, "retrieved_at": page.retrieved_at,
                    "page": summary["pages_received"], "collection": collection_record,
                }, ensure_ascii=True), file=collections_output)
                summary["records_saved"] += 1
                concept_id = collection_record["id"]
                if concept_id in seen_concept_ids:
                    summary["duplicate_records"] += 1
                else:
                    add_collection_to_product_groups(product_groups, collection_record)
                    seen_concept_ids.add(concept_id)
            collections_output.flush()
            print(f"Page {summary['pages_received']}: {len(seen_concept_ids)} / "
                  f"{page.reported_hits} collections; {len(product_groups)} products.",
                  file=diagnostics_output, flush=True)
            if not page.records and page.next_cursor is not None:
                summary["warnings"].append("CMR returned an empty page with a continuation cursor.")
            if not page.records or page.next_cursor is None:
                summary["reached_end"] = True
                break
            if page.next_cursor in seen_cursors:
                summary["warnings"].append("CMR repeated a pagination cursor; stopped paging.")
                break
            seen_cursors.add(page.next_cursor)
            search_after_cursor = page.next_cursor
        if len(seen_concept_ids) != summary["last_reported_hits"]:
            summary["warnings"].append("Unique collection count differs from CMR's reported total.")
        if summary["duplicate_records"]:
            summary["warnings"].append("Repeated collection IDs were saved once in product groups.")
        if summary["reached_end"] and not summary["warnings"]:
            summary["status"] = "complete"
    except (CatalogError, OSError) as catalog_error:
        summary["status"] = "failed"
        summary["error"] = str(catalog_error)
    except KeyboardInterrupt:
        summary["status"] = "interrupted"
    summary["unique_collections"] = len(seen_concept_ids)
    summary["products_grouped"] = len(product_groups)
    summary["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return summary, [product_groups[key] for key in sorted(product_groups)]


def main(argument_values: list[str] | None = None) -> int:
    """Run nasa-catalog-collect to save catalog records and grouped search terms.

    Create a new output directory, report page counts to standard error, and save
    collections.jsonl, products.jsonl, and summary.json. Preserve partial results
    with a failure or interruption status when collection stops early.

    Args:
        argument_values: Arguments without the executable name; defaults to sys.argv.

    Returns:
        Zero for a complete run, four for inconsistent pagination/counts, one for
        failure, or 130 for interruption. Argparse exits with two for invalid input.
    """

    argument_parser = argparse.ArgumentParser(
        prog="nasa-catalog-collect",
        description="Collect public EOSDIS products and candidate code-search terms.",
    )
    argument_parser.add_argument("--output", required=True, type=Path,
                                 help="new directory for catalog files")
    argument_parser.add_argument("--short-name", help="exact product name for a trial run")
    argument_parser.add_argument("--page-size", type=int, default=500,
                                 help="records per page, 1 to 2000 (default: 500)")
    argument_parser.add_argument("--timeout", type=float, default=30,
                                 help="connection/read timeout in seconds (default: 30)")
    argument_parser.add_argument("--ca-bundle", help="optional PEM certificate authority bundle")
    arguments = argument_parser.parse_args(argument_values)
    if not 1 <= arguments.page_size <= 2000:
        argument_parser.error("--page-size must be between 1 and 2000")
    if not math.isfinite(arguments.timeout) or arguments.timeout <= 0:
        argument_parser.error("--timeout must be a positive finite number")
    if arguments.short_name is not None:
        arguments.short_name = arguments.short_name.strip()
        if not arguments.short_name:
            argument_parser.error("--short-name must not be blank")
    source_url = build_catalog_search_url(arguments.short_name, arguments.page_size)
    output_directory = arguments.output.resolve()
    summary_path = output_directory / "summary.json"
    run_summary: dict[str, Any] = {
        "schema_version": 1, "status": "running", "source_url": source_url,
        "scope": "Public CMR collections tagged gov.nasa.eosdis",
        "short_name_filter": arguments.short_name,
        "grouping": "Exact provider and short_name; versions retained per collection",
        "catalog_consistency": "CMR is a live catalog; records can change during pagination.",
        "started_at": datetime.now(timezone.utc).isoformat(), "products_saved": 0,
    }
    try:
        output_directory.mkdir(parents=True, exist_ok=False)
        summary_path.write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
        print(f"Collecting NASA EOSDIS catalog into {output_directory}",
              file=sys.stderr, flush=True)
        try:
            with (output_directory / "collections.jsonl").open("x", encoding="utf-8") as collection_output:
                collection_summary, products = collect_catalog_pages(
                    source_url, arguments.timeout, arguments.ca_bundle,
                    collection_output, sys.stderr,
                )
                run_summary.update(collection_summary)
            with (output_directory / "products.jsonl").open("x", encoding="utf-8") as product_output:
                for product in products:
                    print(json.dumps(product, ensure_ascii=True), file=product_output)
                    run_summary["products_saved"] += 1
        except OSError as output_error:
            run_summary.update(status="failed", error=str(output_error))
        except KeyboardInterrupt:
            run_summary["status"] = "interrupted"
        run_summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary_path.write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
    except OSError as output_error:
        print(f"nasa-catalog-collect: {output_error}", file=sys.stderr)
        return 1
    print(f"{run_summary['status']}: {run_summary['products_saved']} products. "
          f"Summary: {summary_path}", file=sys.stderr)
    for warning in run_summary.get("warnings", []):
        print(f"Warning: {warning}", file=sys.stderr)
    if "error" in run_summary:
        print(run_summary["error"], file=sys.stderr)
    return {"complete": 0, "incomplete": 4, "failed": 1, "interrupted": 130}[run_summary["status"]]


if __name__ == "__main__":
    raise SystemExit(main())

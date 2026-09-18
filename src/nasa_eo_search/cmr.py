"""Look up a NASA data collection and extract identifiers to search for in code.

NASA's Common Metadata Repository (CMR) describes data collections. The catalog
request selects records tagged for the Earth Observing System Data and Information
System (EOSDIS), NASA's system for managing and distributing Earth science data.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
import ssl
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import certifi


CMR_COLLECTIONS_URL = "https://cmr.earthdata.nasa.gov/search/collections.json"


class CatalogError(RuntimeError):
    """A catalog request failed or returned an unexpected response."""


def build_eosdis_collection_request(short_name: str | None) -> Request:
    """Create a CMR catalog request filtered to one EOSDIS collection.

    The ``gov.nasa.eosdis`` tag selects collections in NASA's Earth Observing
    System Data and Information System. A collection describes a dataset and
    version, such as ATL03 version 007. The returned request is ready to pass to
    ``fetch_first_cmr_collection``.

    Args:
        short_name: Optional product identifier, such as ATL03. When omitted,
            CMR chooses the first collection from the tagged catalog.

    Returns:
        An HTTP request with the EOSDIS tag, a page size of one, and the supplied
        product filter.
    """

    query_parameters = {"tag_key": "gov.nasa.eosdis", "page_size": "1"}
    if short_name:
        query_parameters["short_name"] = short_name
    return Request(
        f"{CMR_COLLECTIONS_URL}?{urlencode(query_parameters)}",
        headers={
            "Accept": "application/json",
            "Client-Id": "nasa-eo-search-phase1-preview",
            "User-Agent": "nasa-eo-search/0.1",
        },
    )


def fetch_first_cmr_collection(
    collection_request: Request,
    timeout_seconds: float,
    ca_bundle_path: str | None,
) -> dict[str, Any] | None:
    """Send a catalog request to CMR and return its first collection record.

    Decode the response and check that the record contains the collection ID,
    short name, and title needed to display a product preview.

    Args:
        collection_request: Catalog request from build_eosdis_collection_request.
        timeout_seconds: Maximum wait for each socket connection or read operation.
        ca_bundle_path: Optional file of trusted certificate authorities for the
            HTTPS connection. Defaults to SSL_CERT_FILE or Certifi's bundle.

    Returns:
        The first collection record, or None when no collections match.

    Raises:
        CatalogError: If the connection fails, CMR reports a timeout or HTTP error,
            or the response lacks valid collection metadata.
    """

    resolved_ca_bundle = (
        ca_bundle_path or os.environ.get("SSL_CERT_FILE") or certifi.where()
    )
    try:
        tls_context = ssl.create_default_context(cafile=resolved_ca_bundle)
        with urlopen(
            collection_request, timeout=timeout_seconds, context=tls_context
        ) as catalog_response:
            if catalog_response.headers.get_content_type() != "application/json":
                raise CatalogError("CMR returned a non-JSON response.")
            if any(
                catalog_response.headers.get(header_name, "").lower() == "true"
                for header_name in ("CMR-Time-Out", "CMR-Timed-Out")
            ):
                raise CatalogError("CMR reported a timed-out search; try again.")
            catalog_payload = json.load(catalog_response)
    except HTTPError as http_error:
        raise CatalogError(f"CMR returned HTTP {http_error.code}.") from http_error
    except (OSError, URLError, ValueError) as request_error:
        raise CatalogError(f"Could not read NASA CMR: {request_error}") from request_error

    if not isinstance(catalog_payload, dict):
        raise CatalogError("CMR response must be a JSON object.")
    collection_feed = catalog_payload.get("feed")
    if not isinstance(collection_feed, dict):
        raise CatalogError("CMR response is missing its collection feed.")
    collection_entries = collection_feed.get("entry")
    if not isinstance(collection_entries, list):
        raise CatalogError("CMR response is missing its collection list.")
    if not collection_entries:
        return None
    first_collection = collection_entries[0]
    if not isinstance(first_collection, dict) or any(
        not isinstance(first_collection.get(field_name), str)
        or not first_collection[field_name].strip()
        for field_name in ("id", "short_name", "title")
    ):
        raise CatalogError("CMR collection is missing its ID, short name, or title.")
    return first_collection


def build_product_search_term_preview(collection_record: dict[str, Any]) -> dict[str, Any]:
    """Prepare a product preview with identifiers that can be searched in code.

    Select the collection's name, version, provider, description, and identifiers
    for display by ``nasa-product-preview``. The short name, collection entry ID,
    and concept ID become candidate search terms. Repeated terms appear once;
    each term records its original metadata field and starts as unreviewed.

    Args:
        collection_record: CMR record from fetch_first_cmr_collection, containing
            at least title, short_name, and id.

    Returns:
        A dictionary containing product metadata and a candidate_signatures list.
        Each candidate has a term, source_field, and review_status.
    """

    candidate_signatures = []
    seen_terms: set[str] = set()
    for field_name in ("short_name", "entry_id", "id"):
        candidate_term = collection_record.get(field_name)
        if isinstance(candidate_term, str) and candidate_term not in seen_terms:
            seen_terms.add(candidate_term)
            candidate_signatures.append({
                "term": candidate_term,
                "source_field": field_name,
                "review_status": "unreviewed",
            })
    return {
        "title": collection_record["title"],
        "short_name": collection_record["short_name"],
        "version": collection_record.get("version_id"),
        "provider": collection_record.get("data_center"),
        "concept_id": collection_record["id"],
        "summary": collection_record.get("summary"),
        "candidate_signatures": candidate_signatures,
        "note": "Candidate search terms extracted from CMR collection identifiers.",
    }


def main(argument_values: list[str] | None = None) -> int:
    """Run the nasa-product-preview command to inspect a NASA catalog entry.

    Parse the product filter and connection options, fetch a collection from
    CMR, and print its metadata and candidate code-search terms as formatted JSON
    to standard output. Progress and errors go to standard error. The returned
    status becomes the command's exit code.

    Args:
        argument_values: Arguments without the executable name; defaults to sys.argv.

    Returns:
        Zero for one result, one for failure, two for invalid input, three for no
        matches, or 130 for interruption. Argparse exits for help or syntax errors.
    """

    argument_parser = argparse.ArgumentParser(
        prog="nasa-product-preview",
        description="Look up a NASA data collection and display its code-search identifiers.",
    )
    argument_parser.add_argument("--short-name", help="optional product, e.g. ATL03")
    argument_parser.add_argument(
        "--timeout", type=float, default=20.0,
        help="socket connection/read timeout in seconds (default: 20)",
    )
    argument_parser.add_argument(
        "--ca-bundle", help="PEM CA bundle (default: SSL_CERT_FILE or Certifi)",
    )
    parsed_arguments = argument_parser.parse_args(argument_values)
    if not math.isfinite(parsed_arguments.timeout) or parsed_arguments.timeout <= 0:
        argument_parser.error("--timeout must be a positive finite number")
    if parsed_arguments.short_name is not None:
        parsed_arguments.short_name = parsed_arguments.short_name.strip()
        if not parsed_arguments.short_name:
            argument_parser.error("--short-name must not be blank")

    collection_request = build_eosdis_collection_request(parsed_arguments.short_name)
    started_at = time.monotonic()
    print("Contacting NASA CMR for one collection...", file=sys.stderr, flush=True)
    try:
        collection_record = fetch_first_cmr_collection(
            collection_request, parsed_arguments.timeout, parsed_arguments.ca_bundle,
        )
        if collection_record is None:
            print("No matching EOSDIS collection found.", file=sys.stderr)
            return 3
        result_record = {
            "source_url": collection_request.full_url,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "collection": build_product_search_term_preview(collection_record),
        }
        print(json.dumps(result_record, indent=2, ensure_ascii=True))
        print(
            f"Returned one collection in {time.monotonic() - started_at:.1f}s; finished.",
            file=sys.stderr, flush=True,
        )
    except CatalogError as catalog_error:
        print(f"nasa-product-preview: {catalog_error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Catalog preview interrupted.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

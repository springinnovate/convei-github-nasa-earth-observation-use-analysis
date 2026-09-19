"""Test catalog pagination, product grouping, provenance, and saved run statuses."""

from contextlib import redirect_stderr
from email.message import Message
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import certifi

from nasa_eo_search.catalog import (
    CatalogError, CollectionPage, add_collection_to_product_groups,
    build_catalog_search_url, collect_catalog_pages, fetch_cmr_collection_page, main,
)


EXAMPLE_COLLECTION = {
    "id": "C1-NSIDC", "short_name": "ATL03", "entry_id": "ATL03_006",
    "title": "Photon data version 006", "version_id": "006",
    "data_center": "NSIDC", "summary": "Photon heights and coordinates.",
}
SECOND_VERSION = {**EXAMPLE_COLLECTION, "id": "C2-NSIDC", "entry_id": "ATL03_007",
                  "version_id": "007", "title": "Photon data version 007"}


def make_catalog_response(
    payload: object, headers: dict[str, str] | None = None,
) -> BytesIO:
    """Create a mock HTTP response with CMR JSON content and response headers.

    Args:
        payload: Object to serialize as the response body.
        headers: Overrides for JSON content type and matching count headers.

    Returns:
        In-memory byte stream with HTTP-style headers for urlopen tests.
    """

    response = BytesIO(json.dumps(payload).encode())
    response.headers = Message()
    for name, value in {"Content-Type": "application/json", "CMR-Hits": "1",
                        **(headers or {})}.items():
        response.headers[name] = value
    return response


class CatalogCollectorTests(unittest.TestCase):
    """Exercise full catalog collection using deterministic CMR response pages."""

    def test_command_writes_files_and_status_for_every_outcome(self) -> None:
        """Save complete, empty, incomplete, failed, and interrupted catalog runs."""

        cases = [
            ([CollectionPage([EXAMPLE_COLLECTION], 1, None, "now")], 0, "complete", 1),
            ([CollectionPage([], 0, None, "now")], 0, "complete", 0),
            ([CollectionPage([EXAMPLE_COLLECTION], 2, None, "now")], 4, "incomplete", 1),
            ([CatalogError("offline")], 1, "failed", 0),
            ([CollectionPage([EXAMPLE_COLLECTION], 2, "cursor", "now"),
              KeyboardInterrupt()], 130, "interrupted", 1),
        ]
        for pages, exit_code, status, count in cases:
            with self.subTest(status=status, count=count), TemporaryDirectory() as temporary_directory:
                output = Path(temporary_directory) / "catalog"
                with patch("nasa_eo_search.catalog.fetch_cmr_collection_page", side_effect=pages), redirect_stderr(StringIO()):
                    self.assertEqual(main(["--output", str(output)]), exit_code)
                summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["status"], status)
                self.assertEqual(summary["products_saved"], count)
                self.assertEqual(summary["unique_collections"], count)
                self.assertIn("gov.nasa.eosdis", summary["source_url"])
                self.assertIn("finished_at", summary)
                self.assertEqual(len((output / "products.jsonl").read_text(encoding="utf-8").splitlines()), count)
                self.assertEqual(len((output / "collections.jsonl").read_text(encoding="utf-8").splitlines()), count)

    def test_existing_directory_is_preserved_without_network_call(self) -> None:
        """Refuse an existing output directory and leave its files unchanged."""

        with TemporaryDirectory() as temporary_directory:
            marker = Path(temporary_directory) / "summary.json"
            marker.write_text("existing results", encoding="utf-8")
            with patch("nasa_eo_search.catalog.fetch_cmr_collection_page") as fetch_mock, redirect_stderr(StringIO()):
                self.assertEqual(main(["--output", temporary_directory]), 1)
            fetch_mock.assert_not_called()
            self.assertEqual(marker.read_text(encoding="utf-8"), "existing results")

    def test_invalid_arguments_do_not_create_output_or_contact_cmr(self) -> None:
        """Validate page sizes, finite positive timeouts, and nonempty filters."""

        for arguments in (["--page-size", "0"], ["--page-size", "2001"],
                          ["--timeout", "0"], ["--timeout", "nan"],
                          ["--timeout", "inf"], ["--short-name", " "]):
            with self.subTest(arguments=arguments), TemporaryDirectory() as temporary_directory:
                output = Path(temporary_directory) / "catalog"
                with patch("nasa_eo_search.catalog.fetch_cmr_collection_page") as fetch_mock, redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    main(["--output", str(output), *arguments])
                fetch_mock.assert_not_called()
                self.assertFalse(output.exists())

    def test_raw_output_failure_is_reported(self) -> None:
        """Return a failed run when writing a raw collection record fails."""

        with patch("nasa_eo_search.catalog.fetch_cmr_collection_page", return_value=CollectionPage(
            [EXAMPLE_COLLECTION], 1, None, "now",
        )):
            raw_output = StringIO()
            with patch.object(raw_output, "write", side_effect=OSError("disk full")):
                summary, products = collect_catalog_pages("url", 30, None, raw_output, StringIO())
        self.assertEqual(summary["status"], "failed")
        self.assertIn("disk full", summary["error"])
        self.assertEqual(products, [])

    def test_multiple_pages_preserve_raw_records_and_group_versions(self) -> None:
        """Traverse cursors through an empty final page and retain source metadata."""

        pages = [CollectionPage([EXAMPLE_COLLECTION], 2, "first", "time-one"),
                 CollectionPage([SECOND_VERSION], 2, "second", "time-two"),
                 CollectionPage([], 2, None, "time-three")]
        raw_output = StringIO()
        diagnostics = StringIO()
        with patch("nasa_eo_search.catalog.fetch_cmr_collection_page", side_effect=pages) as fetch_mock:
            summary, products = collect_catalog_pages("query-url", 30, None, raw_output, diagnostics)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["records_saved"], 2)
        self.assertEqual(summary["unique_collections"], 2)
        self.assertEqual(summary["pages_received"], 3)
        self.assertEqual(summary["products_grouped"], 1)
        self.assertEqual([call.args[1] for call in fetch_mock.call_args_list], [None, "first", "second"])
        self.assertTrue(all(call.args[0] == "query-url" for call in fetch_mock.call_args_list))
        saved_records = [json.loads(line) for line in raw_output.getvalue().splitlines()]
        self.assertEqual(saved_records[0], {"source_url": "query-url", "retrieved_at": "time-one",
                                          "page": 1, "collection": EXAMPLE_COLLECTION})
        self.assertEqual(saved_records[1]["collection"], SECOND_VERSION)
        self.assertIn("2 / 2 collections; 1 products", diagnostics.getvalue())
        self.assertEqual([item["version"] for item in products[0]["collections"]], ["006", "007"])
        signatures = {item["term"]: item for item in products[0]["candidate_signatures"]}
        self.assertEqual(len(signatures["ATL03"]["sources"]), 2)
        self.assertEqual(set(signatures), {"ATL03", "ATL03_006", "ATL03_007", "C1-NSIDC", "C2-NSIDC"})
        self.assertTrue(all(item["review_status"] == "unreviewed" for item in signatures.values()))

    def test_grouping_separates_providers_and_preserves_equal_term_sources(self) -> None:
        """Keep provider/name pairs separate and record every source of a shared term."""

        groups = {}
        add_collection_to_product_groups(groups, {**EXAMPLE_COLLECTION, "entry_id": "ATL03"})
        add_collection_to_product_groups(groups, {**SECOND_VERSION, "data_center": "OTHER"})
        add_collection_to_product_groups(groups, {**SECOND_VERSION, "short_name": "atl03", "entry_id": " "})
        self.assertEqual(len(groups), 3)
        signature = groups[("NSIDC", "ATL03")]["candidate_signatures"][0]
        self.assertEqual(signature["sources"], [
            {"concept_id": "C1-NSIDC", "source_field": "short_name"},
            {"concept_id": "C1-NSIDC", "source_field": "entry_id"},
        ])
        self.assertEqual(len(groups[("NSIDC", "atl03")]["candidate_signatures"]), 2)

    def test_pagination_problems_are_incomplete(self) -> None:
        """Flag truncated, duplicate, repeated-cursor, and changing-count responses."""

        first_page = CollectionPage([EXAMPLE_COLLECTION], 2, "cursor", "now")
        page_sequences = [
            [CollectionPage([], 0, "cursor", "now")],
            [CollectionPage([EXAMPLE_COLLECTION], 2, None, "now")],
            [first_page, CollectionPage([], 2, None, "now")],
            [first_page, CollectionPage([SECOND_VERSION], 2, "cursor", "now")],
            [first_page, CollectionPage([EXAMPLE_COLLECTION], 2, None, "now")],
            [CollectionPage([EXAMPLE_COLLECTION], 3, "cursor", "now"),
             CollectionPage([SECOND_VERSION], 2, None, "now")],
        ]
        for pages in page_sequences:
            with self.subTest(pages=pages), patch("nasa_eo_search.catalog.fetch_cmr_collection_page", side_effect=pages):
                summary, products = collect_catalog_pages("url", 30, None, StringIO(), StringIO())
            self.assertEqual(summary["status"], "incomplete")
            self.assertTrue(summary["warnings"])
            self.assertEqual(len(products), 1 if pages[0].records else 0)

    def test_failure_and_interruption_preserve_partial_products(self) -> None:
        """Keep completed pages when a subsequent request fails or is interrupted."""

        for error, status in ((CatalogError("offline"), "failed"), (KeyboardInterrupt(), "interrupted")):
            with self.subTest(status=status), patch("nasa_eo_search.catalog.fetch_cmr_collection_page", side_effect=[
                CollectionPage([EXAMPLE_COLLECTION], 2, "cursor", "now"), error,
            ]):
                raw_output = StringIO()
                summary, products = collect_catalog_pages("url", 30, None, raw_output, StringIO())
            self.assertEqual(summary["status"], status)
            self.assertEqual(summary["records_saved"], 1)
            self.assertEqual(len(products), 1)
            self.assertEqual(len(raw_output.getvalue().splitlines()), 1)

    def test_request_keeps_scope_filter_and_cursor(self) -> None:
        """Send the exact product filter and forward the opaque pagination header."""

        source_url = build_catalog_search_url("ATL03", 1)
        query = parse_qs(urlsplit(source_url).query)
        self.assertEqual(query["tag_key"], ["gov.nasa.eosdis"])
        self.assertEqual(query["page_size"], ["1"])
        self.assertEqual(query["short_name"], ["ATL03"])
        self.assertEqual(query["options[short_name][pattern]"], ["false"])
        self.assertEqual(query["options[short_name][ignore_case]"], ["false"])
        self.assertNotIn("short_name", parse_qs(urlsplit(build_catalog_search_url(None, 500)).query))
        for explicit_bundle, environment_bundle, expected_bundle in (
            (None, None, certifi.where()), (None, "environment.pem", "environment.pem"),
            ("custom.pem", "environment.pem", "custom.pem"),
        ):
            with (
                self.subTest(bundle=expected_bundle),
                patch("nasa_eo_search.catalog.urlopen", return_value=make_catalog_response(
                    {"feed": {"entry": [EXAMPLE_COLLECTION]}}, {"CMR-Search-After": '["next", 2]'},
                )) as open_mock,
                patch("nasa_eo_search.catalog.ssl.create_default_context") as context_mock,
                patch.dict(os.environ, {"SSL_CERT_FILE": environment_bundle} if environment_bundle else {}, clear=True),
            ):
                page = fetch_cmr_collection_page(source_url, '["prior", 1]', 8, explicit_bundle)
                self.assertEqual(page.next_cursor, '["next", 2]')
                self.assertEqual(page.records, [EXAMPLE_COLLECTION])
                self.assertEqual(page.reported_hits, 1)
                self.assertTrue(page.retrieved_at)
                self.assertEqual(open_mock.call_args.args[0].get_header("Cmr-search-after"), '["prior", 1]')
                self.assertEqual(open_mock.call_args.kwargs["timeout"], 8)
                context_mock.assert_called_once_with(cafile=expected_bundle)

    def test_invalid_responses_raise_catalog_error(self) -> None:
        """Reject malformed records, invalid totals, wrong formats, and timeouts."""

        valid_payload = {"feed": {"entry": [EXAMPLE_COLLECTION]}}
        cases = [([], {}), ({}, {}), ({"feed": {"entry": {}}}, {}),
                 ({"feed": {"entry": [None]}}, {}),
                 (valid_payload, {"CMR-Hits": ""}), (valid_payload, {"CMR-Hits": "-1"}),
                 (valid_payload, {"Content-Type": "text/html"}),
                 (valid_payload, {"CMR-Time-Out": "true"}),
                 (valid_payload, {"CMR-Timed-Out": "true"})]
        for field in ("id", "short_name", "title", "data_center"):
            for invalid_value in (None, " ", 42):
                cases.append(({"feed": {"entry": [{**EXAMPLE_COLLECTION, field: invalid_value}]}}, {}))
        for payload, headers in cases:
            with self.subTest(payload=payload, headers=headers), patch(
                "nasa_eo_search.catalog.urlopen", return_value=make_catalog_response(payload, headers),
            ), self.assertRaises(CatalogError):
                fetch_cmr_collection_page("https://example.test", None, 1, None)

    def test_network_and_decode_errors_raise_catalog_error(self) -> None:
        """Translate transport and malformed JSON failures into readable errors."""

        for error in (URLError("offline"), TimeoutError("slow"),
                      HTTPError("https://example.test", 429, "limited", None, None),
                      HTTPError("https://example.test", 503, "unavailable", None, None)):
            with self.subTest(error=error), patch(
                "nasa_eo_search.catalog.urlopen", side_effect=error,
            ), self.assertRaises(CatalogError):
                fetch_cmr_collection_page("https://example.test", None, 1, None)
        response = make_catalog_response({})
        response.seek(0)
        response.truncate()
        response.write(b"invalid json")
        response.seek(0)
        with patch("nasa_eo_search.catalog.urlopen", return_value=response), self.assertRaises(CatalogError):
            fetch_cmr_collection_page("https://example.test", None, 1, None)

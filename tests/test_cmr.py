"""Offline checks for the single-collection Phase 1 preview."""

from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from io import BytesIO, StringIO
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import certifi

from nasa_eo_search.cmr import (
    CatalogError,
    build_collection_request,
    fetch_first_collection,
    main,
    summarize_collection,
)


EXAMPLE_COLLECTION = {
    "id": "C3326974349-NSIDC_CPRD",
    "short_name": "ATL03",
    "entry_id": "ATL03_007",
    "title": "ATLAS/ICESat-2 L2A Global Geolocated Photon Data V007",
    "version_id": "007",
    "data_center": "NSIDC_CPRD",
}


class CatalogPreviewTests(unittest.TestCase):
    """Exercise request limits, transport failures, and candidate provenance."""

    def test_one_request_yields_one_record_with_verified_tls(self) -> None:
        """Request a single EOSDIS result and use the configured CA bundle."""

        for custom_bundle in (None, "company-ca.pem"):
            with self.subTest(custom_bundle=custom_bundle):
                response_headers = Message()
                response_headers["Content-Type"] = "application/json"
                response_stream = BytesIO(json.dumps({
                    "feed": {"entry": [EXAMPLE_COLLECTION]}
                }).encode())
                response_stream.headers = response_headers
                with (
                    patch("nasa_eo_search.cmr.urlopen", return_value=response_stream)
                    as open_response_mock,
                    patch("nasa_eo_search.cmr.ssl.create_default_context")
                    as context_factory_mock,
                    patch.dict(os.environ, {}, clear=True),
                ):
                    search_request = build_collection_request("ATL03")
                    collection_record = fetch_first_collection(
                        search_request, 20, custom_bundle
                    )
                    self.assertEqual(collection_record, EXAMPLE_COLLECTION)
                    open_response_mock.assert_called_once_with(
                        search_request, timeout=20,
                        context=context_factory_mock.return_value,
                    )
                    context_factory_mock.assert_called_once_with(
                        cafile=custom_bundle or certifi.where()
                    )
                query_parameters = parse_qs(urlsplit(search_request.full_url).query)
                self.assertEqual(query_parameters, {
                    "page_size": ["1"], "tag_key": ["gov.nasa.eosdis"],
                    "short_name": ["ATL03"],
                })

    def test_empty_malformed_and_timed_out_responses(self) -> None:
        """Distinguish no matches from invalid or incomplete catalog responses."""

        for response_body, timed_out, expected_empty in (
            ('{"feed":{"entry":[]}}', False, True),
            ('{"feed":{"entry":[]}}', True, False),
            ('<html>error</html>', False, False),
            ('{}', False, False),
            ('{"feed":{"entry":[{}]}}', False, False),
            ('[]', False, False),
        ):
            with self.subTest(response_body=response_body, timed_out=timed_out):
                response_stream = BytesIO(response_body.encode())
                response_headers = Message()
                response_headers["Content-Type"] = "application/json"
                response_headers["CMR-Time-Out"] = str(timed_out).lower()
                response_stream.headers = response_headers
                with patch("nasa_eo_search.cmr.urlopen", return_value=response_stream):
                    if expected_empty:
                        self.assertIsNone(fetch_first_collection(
                            build_collection_request(None), 20, None
                        ))
                    else:
                        with self.assertRaises(CatalogError):
                            fetch_first_collection(build_collection_request(None), 20, None)

    def test_transport_errors_become_catalog_errors(self) -> None:
        """Wrap network, timeout, and HTTP failures for readable CLI errors."""

        for transport_error in (
            URLError("offline"), TimeoutError("read timed out"),
            HTTPError("https://example.test", 503, "unavailable", None, None),
        ):
            with self.subTest(transport_error=transport_error):
                with patch("nasa_eo_search.cmr.urlopen", side_effect=transport_error):
                    with self.assertRaises(CatalogError):
                        fetch_first_collection(build_collection_request(None), 20, None)

    def test_candidate_terms_preserve_source_fields_and_deduplicate(self) -> None:
        """Expose observed identifiers as unreviewed terms without invented names."""

        collection_record = {**EXAMPLE_COLLECTION, "entry_id": "ATL03"}
        collection_preview = summarize_collection(collection_record)
        self.assertEqual(collection_preview["candidate_signatures"], [
            {"term": "ATL03", "source_field": "short_name", "review_status": "unreviewed"},
            {"term": EXAMPLE_COLLECTION["id"], "source_field": "id",
             "review_status": "unreviewed"},
        ])
        self.assertIsNone(collection_preview["summary"])

    def test_command_success_empty_and_failure(self) -> None:
        """Keep results in stdout and report completion or failure in stderr."""

        for collection_result, expected_status in (
            (EXAMPLE_COLLECTION, 0), (None, 3), (CatalogError("offline"), 1),
        ):
            results_output = StringIO()
            diagnostics_output = StringIO()
            with (
                patch("nasa_eo_search.cmr.fetch_first_collection") as fetch_mock,
                redirect_stdout(results_output), redirect_stderr(diagnostics_output),
            ):
                if isinstance(collection_result, Exception):
                    fetch_mock.side_effect = collection_result
                else:
                    fetch_mock.return_value = collection_result
                self.assertEqual(main(["--short-name", "ATL03"]), expected_status)
                fetch_mock.assert_called_once()
            self.assertIn("Contacting NASA CMR", diagnostics_output.getvalue())
            if expected_status == 0:
                result_record = json.loads(results_output.getvalue())
                self.assertEqual(result_record["collection"]["short_name"], "ATL03")
                self.assertIn("retrieved_at", result_record)
            else:
                self.assertEqual(results_output.getvalue(), "")

    def test_invalid_arguments_do_not_contact_cmr(self) -> None:
        """Reject blank product names and nonpositive or nonfinite timeouts."""

        for argument_values in (
            ["--timeout", "0"], ["--timeout", "nan"],
            ["--timeout", "inf"], ["--short-name", " "],
        ):
            with (
                patch("nasa_eo_search.cmr.urlopen") as open_response_mock,
                redirect_stderr(StringIO()), self.assertRaises(SystemExit),
            ):
                main(argument_values)
            open_response_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

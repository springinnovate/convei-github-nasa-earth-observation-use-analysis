"""Verify bounded web discovery and public-only evidence with offline services."""

import json
from io import BytesIO
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from nasa_eo_search import web_discovery


QUERIES = ['site:github.com "ATL03"', 'site:github.com "ATL03" photon data']
PUBLIC_METADATA = {"fork": False, "archived": False, "size_kb": 10, "stars": 2}


def create_web_search_response(source_urls: list[str] | None = None) -> dict:
    """Build a Responses fixture with two search queries and consulted source URLs.

    Args:
        source_urls: Tool-backed URLs; None supplies one public GitHub file URL.

    Returns:
        Completed Responses object with tool metadata and deliberately ignored prose.
    """
    urls = source_urls if source_urls is not None else ["https://github.com/example/project/blob/main/use.py"]
    return {
        "id": "resp_fixture", "model": "test-model", "status": "completed",
        "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
        "output": [
            {"type": "web_search_call", "id": "ws_fixture", "status": "completed",
             "action": {"type": "search", "queries": QUERIES,
                        "sources": [{"type": "url", "url": url} for url in urls]}},
            {"type": "message", "content": [{"type": "output_text",
                "text": "Invented https://github.com/invented/unsupported", "annotations": [
                    {"type": "url_citation", "url": "https://github.com/citation/unsupported"}]}]},
        ],
    }


class WebDiscoveryTests(unittest.TestCase):
    """Check source provenance, request bounds, failures, and visibility filtering."""

    def setUp(self) -> None:
        """Replace both paid search and public metadata lookups with offline fixtures."""
        response_patch = patch.object(web_discovery, "request_openai_web_response",
                                      return_value=create_web_search_response())
        metadata_patch = patch.object(web_discovery, "fetch_web_candidate_metadata", return_value=PUBLIC_METADATA)
        self.request_response = response_patch.start()
        self.fetch_metadata = metadata_patch.start()
        self.addCleanup(response_patch.stop)
        self.addCleanup(metadata_patch.stop)

    def search(self) -> dict:
        """Run one fixture-backed discovery request and return its sanitized receipt.

        Returns:
            Receipt for the fixed dataset, query pair, and mock model.
        """
        return web_discovery.search_openai_web("ATL03", queries=QUERIES, model="test-model", api_key="fixture-key")

    def test_request_bounds_and_only_tool_sources_produce_candidates(self) -> None:
        """Ignore generated prose and annotations while retaining sourced public repositories."""
        receipt = self.search()
        self.assertEqual(receipt["status"], "limited")
        self.assertEqual(receipt["results"], [{
            "repository": "github.com/example/project", "file": "", "url": "https://github.com/example/project",
            "repository_metadata": PUBLIC_METADATA,
            "source_urls": ["https://github.com/example/project/blob/main/use.py"],
        }])
        request_body, credential, bundle = self.request_response.call_args.args
        self.assertEqual(credential, "fixture-key")
        self.assertIsNone(bundle)
        self.assertFalse(request_body["store"])
        self.assertEqual(request_body["max_tool_calls"], 2)
        self.assertEqual(request_body["max_output_tokens"], 2000)
        self.assertEqual(request_body["tool_choice"], "required")
        self.assertEqual(request_body["tools"][0]["filters"], {"allowed_domains": ["github.com"]})
        self.assertEqual(request_body["include"], ["web_search_call.action.sources"])
        self.assertEqual(json.loads(request_body["input"]), {"queries": QUERIES})
        self.assertEqual(receipt["usage"], {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30})
        self.assertEqual(receipt["web_search_calls"], 1)
        self.assertEqual(receipt["actual_queries"], QUERIES)
        self.assertNotIn("invented", json.dumps(receipt))
        self.assertNotIn("fixture-key", json.dumps(receipt))

    def test_public_url_normalization_rejects_unsafe_and_non_code_routes(self) -> None:
        """Accept supported GitHub routes and exclude alternate hosts and path manipulation."""
        for url in ("https://github.com/Owner/Repo", "https://github.com/Owner/Repo/",
                    "https://github.com/Owner/Repo/blob/main/file.py?raw=1#L4",
                    "https://github.com/Owner/Repo/tree/main/src"):
            with self.subTest(url=url):
                self.assertEqual(web_discovery.normalize_public_github_repository_url(url), "github.com/Owner/Repo")
        for url in (None, 4, "http://github.com/example/project", "https://github.com.evil/example/project",
                    "https://api.github.com/repos/example/project", "https://user@github.com/example/project",
                    "https://github.com:443/example/project", "https://github.com/example/../project",
                    "https://github.com//example/project", "https://github.com/example/project/issues/1",
                    "https://github.com/example/project/pull/1", "https://github.com/example/project/blob",
                    "https://github.com/example/project/blob/main/%2e%2e/secret",
                    "https://github.com/example/project/tree/main/%252e%252e/secret",
                    "https://github.com/example/project/blob/main/%2fsecret",
                    "https://github.com/%65xample/project", "https://github.com/example/project\n"):
            with self.subTest(url=url):
                self.assertIsNone(web_discovery.normalize_public_github_repository_url(url))

    def test_deduplication_and_source_limits_bound_public_verification(self) -> None:
        """Keep at most twenty verified repositories and five distinct links per repository."""
        urls = [f"https://github.com/example/project/blob/main/file{number}.py" for number in range(9)]
        urls += [f"https://github.com/example/other{number}" for number in range(25)]
        self.request_response.return_value = create_web_search_response(urls)
        receipt = self.search()
        self.assertEqual(self.fetch_metadata.call_count, 20)
        self.assertEqual(receipt["public_metadata_requests"], 20)
        self.assertEqual(len(receipt["results"]), 20)
        self.assertEqual(len(receipt["results"][0]["source_urls"]), 5)
        self.assertEqual(receipt["candidate_source_repositories"], 26)
        self.assertTrue(any("twenty" in warning for warning in receipt["warnings"]))
        self.assertTrue(any("five source" in warning for warning in receipt["warnings"]))

    def test_private_unknown_and_unverifiable_sources_are_not_persisted(self) -> None:
        """Omit rejected URL identities and model prose while disclosing verification gaps."""
        self.request_response.return_value = create_web_search_response([
            "https://github.com/private/secret/blob/main/private.py",
            "https://github.com/unknown/hidden", "https://github.com/public/accepted",
        ])
        self.fetch_metadata.side_effect = [None, RuntimeError("RAW_PRIVATE_DETAILS"), PUBLIC_METADATA]
        receipt = self.search()
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(receipt["omitted_unverified_repositories"], 2)
        self.assertEqual(receipt["results"][0]["repository"], "github.com/public/accepted")
        saved_receipt = json.dumps(receipt)
        for forbidden in ("private/secret", "unknown/hidden", "RAW_PRIVATE_DETAILS", "invented/unsupported"):
            self.assertNotIn(forbidden, saved_receipt)

    def test_missing_search_or_sources_is_failure_not_a_genuine_zero(self) -> None:
        """Require completed tool search metadata before accepting an empty candidate set."""
        fixtures = [create_web_search_response() for _ in range(3)]
        fixtures[0]["output"] = fixtures[0]["output"][1:]
        del fixtures[1]["output"][0]["action"]["sources"]
        fixtures[2]["output"][0]["action"]["type"] = "open_page"
        for fixture in fixtures:
            with self.subTest(output=fixture["output"]):
                self.request_response.return_value = fixture
                receipt = self.search()
                self.assertEqual(receipt["status"], "error")
                self.assertEqual(receipt["results"], [])
        self.fetch_metadata.assert_not_called()

    def test_valid_empty_search_remains_limited_without_claiming_coverage(self) -> None:
        """Preserve an explicit empty consulted-source list as a bounded completed request."""
        self.request_response.return_value = create_web_search_response([])
        receipt = self.search()
        self.assertEqual(receipt["status"], "limited")
        self.assertEqual(receipt["results"], [])
        self.assertEqual(receipt["pages_returned"], 1)
        self.assertFalse(receipt["has_more"])
        self.fetch_metadata.assert_not_called()

    def test_incomplete_response_retains_completed_public_source_evidence(self) -> None:
        """Retain a completed search's verified candidates when the overall response is incomplete."""
        fixture = create_web_search_response()
        fixture["status"] = "incomplete"
        self.request_response.return_value = fixture
        receipt = self.search()
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(len(receipt["results"]), 1)

    def test_actual_query_drift_is_recorded_without_replacing_requested_queries(self) -> None:
        """Retain service-reported query strings and warn when they differ from frozen inputs."""
        fixture = create_web_search_response()
        fixture["output"][0]["action"].pop("queries")
        fixture["output"][0]["action"]["query"] = "ATL03 photon data github"
        self.request_response.return_value = fixture
        receipt = self.search()
        self.assertEqual(receipt["actual_queries"], ["ATL03 photon data github"])
        self.assertEqual(receipt["requested_queries"], QUERIES)
        self.assertTrue(any("differ" in warning for warning in receipt["warnings"]))

    def test_service_failure_is_saved_once_without_automatic_retry(self) -> None:
        """Return a failure receipt after one sanitized transport failure."""
        self.request_response.side_effect = ValueError("OpenAI web search HTTP 429; check quota.")
        receipt = self.search()
        self.assertEqual(receipt["status"], "error")
        self.assertEqual(self.request_response.call_count, 1)
        self.fetch_metadata.assert_not_called()

    def test_invalid_input_is_rejected_before_any_service_call(self) -> None:
        """Validate frozen model and query values without reading credentials or searching."""
        for queries, model in (([], "test"), (["one", "two", "three"], "test"),
                               (["one", "bad\nquery"], "test"), (["one", "two"], "")):
            with self.subTest(queries=queries, model=model):
                with self.assertRaises(ValueError):
                    web_discovery.search_openai_web("ATL03", queries=queries, model=model, api_key="fixture")
        self.request_response.assert_not_called()
        self.fetch_metadata.assert_not_called()

    def test_missing_catalog_title_can_use_one_frozen_query(self) -> None:
        """Allow the helper's canonical-only fallback without requiring invented context."""
        request_body = web_discovery.build_openai_web_request(QUERIES[:1], "test-model")
        self.assertEqual(json.loads(request_body["input"]), {"queries": QUERIES[:1]})
        self.assertEqual(request_body["max_tool_calls"], 2)


class WebServiceBoundaryTests(unittest.TestCase):
    """Check the external service boundaries without network or real credentials."""

    @patch.object(web_discovery, "run_github_json")
    def test_public_metadata_is_filtered_before_cli_output(self, github_request: Mock) -> None:
        """Request only sanitized public metadata and reject private or malformed responses.

        Args:
            github_request: Mock GitHub CLI request boundary.
        """
        github_request.return_value = {"private": False, "full_name": "example/project",
            "html_url": "https://github.com/example/project", "fork": False, "archived": False,
            "size": 10, "stargazers_count": 2}
        self.assertEqual(web_discovery.fetch_web_candidate_metadata("example/project"), PUBLIC_METADATA)
        self.assertIn(web_discovery.PUBLIC_WEB_REPOSITORY_FILTER, github_request.call_args.args[0])
        for response in ({"private": True}, {}, None):
            github_request.return_value = response
            self.assertIsNone(web_discovery.fetch_web_candidate_metadata("example/project"))
        github_request.return_value = {"private": False, "full_name": "different/project"}
        with self.assertRaises(RuntimeError):
            web_discovery.fetch_web_candidate_metadata("example/project")

    @patch.object(web_discovery, "read_bounded_https")
    def test_transport_uses_fixed_endpoint_and_sanitizes_failures(self, https_reader: Mock) -> None:
        """Use the bounded redirect-rejecting transport and omit raw error bodies.

        Args:
            https_reader: Mock shared HTTPS reader receiving the authenticated request.
        """
        request_body = web_discovery.build_openai_web_request(QUERIES, "test-model")
        https_reader.return_value = b'{"output": []}'
        web_discovery.request_openai_web_response(request_body, "fixture-secret", "ca.pem")
        request, timeout, certificate_file = https_reader.call_args.args
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(timeout, 60.0)
        self.assertEqual(certificate_file, "ca.pem")
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-secret")
        failures = [HTTPError(request.full_url, 429, "SECRET_ERROR", {}, BytesIO(b"SECRET_BODY")),
                    URLError("SECRET_REASON"), ValueError("SECRET_SIZE_DETAIL")]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                https_reader.side_effect = failure
                with self.assertRaises(ValueError) as raised:
                    web_discovery.request_openai_web_response(request_body, "fixture-secret", None)
                self.assertNotIn("SECRET", str(raised.exception))
                self.assertNotIn("fixture-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

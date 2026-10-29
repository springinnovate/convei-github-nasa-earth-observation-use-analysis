"""Verify complementary public repository discovery without making API calls."""

import json
import unittest
from unittest.mock import Mock, patch

from nasa_eo_search import repository_discovery


def create_public_repository_result(name: str = "example/project") -> dict:
    """Build one public repository-search item with bounded selection metadata.

    Args:
        name: GitHub owner/repository used for its name and URL.

    Returns:
        Repository item resembling the sanitized GitHub API response.
    """
    return {"full_name": name, "private": False, "html_url": f"https://github.com/{name}",
            "fork": False, "archived": False, "size": 42, "stargazers_count": 7}


def create_repository_search_page(total_count: int = 1, incomplete: bool = False) -> dict:
    """Build a repository-search page containing one public candidate.

    Args:
        total_count: Number of candidates reported for the complete query.
        incomplete: Whether GitHub explicitly reports missing results.

    Returns:
        Page with completion metadata and one candidate, or none for zero hits.
    """
    return {"total_count": total_count, "incomplete_results": incomplete,
            "omitted_nonpublic": 0,
            "items": [create_public_repository_result()] if total_count else []}


class RepositoryReadmeDiscoveryTests(unittest.TestCase):
    """Check query scope, safe metadata, pagination, and recoverable failures."""

    def setUp(self) -> None:
        """Isolate rate state and replace waits so tests never contact or pause for GitHub."""
        repository_discovery._last_github_search_started = None
        sleep_patch = patch("nasa_eo_search.repository_discovery.time.sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    @patch("nasa_eo_search.repository_discovery.run_github_json")
    def test_query_searches_public_repository_metadata_without_inventing_file_hits(self, request: Mock) -> None:
        """Use the complementary API and report repository URLs with empty file paths.

        Args:
            request: Mock GitHub CLI request returning public repository metadata.
        """
        request.return_value = create_repository_search_page()
        receipt = repository_discovery.search_github_repositories("MODIS/061/MOD13Q1")
        self.assertEqual(receipt["query"], '"MODIS/061/MOD13Q1" in:name,description,readme is:public fork:true')
        self.assertIn("search/repositories", request.call_args.args[0])
        self.assertIn(repository_discovery.PUBLIC_REPOSITORY_SEARCH_FILTER, request.call_args.args[0])
        self.assertEqual(receipt["provider"], "github-repositories")
        self.assertEqual(receipt["results"], [{
            "repository": "github.com/example/project", "file": "", "url": "https://github.com/example/project",
            "repository_metadata": {"fork": False, "archived": False, "size_kb": 42, "stars": 7},
        }])

    @patch("nasa_eo_search.repository_discovery.run_github_json")
    def test_empty_results_are_returned_without_claiming_github_coverage(self, request: Mock) -> None:
        """Preserve a genuine zero-hit response and its search-scope warning.

        Args:
            request: Mock GitHub CLI request returning an empty response.
        """
        request.return_value = create_repository_search_page(0)
        receipt = repository_discovery.search_github_repositories("SPL3SMP")
        self.assertEqual(receipt["status"], "returned")
        self.assertEqual(receipt["results"], [])
        self.assertFalse(receipt["has_more"])
        self.assertEqual(receipt["pages_returned"], 1)
        self.assertTrue(any("coverage" in warning for warning in receipt["warnings"]))

    @patch("nasa_eo_search.repository_discovery.run_github_json")
    def test_capped_later_page_does_not_repeat_saved_pages(self, request: Mock) -> None:
        """Request a selected saved-run page and preserve the remaining-hit indicator.

        Args:
            request: Mock GitHub CLI request for page two of three.
        """
        request.return_value = create_repository_search_page(250)
        receipt = repository_discovery.search_github_repositories("SPL3SMP", start_page=2)
        self.assertIn("page=2", request.call_args.args[0])
        self.assertEqual(receipt["status"], "capped")
        self.assertTrue(receipt["has_more"])
        self.assertEqual(receipt["pages_returned"], 1)
        self.assertEqual(receipt["start_page"], 2)

    @patch("nasa_eo_search.repository_discovery.run_github_json")
    def test_pagination_deduplicates_case_and_preserves_incomplete_status(self, request: Mock) -> None:
        """Keep prior incompleteness and deduplicate a repository across result pages.

        Args:
            request: Mock GitHub CLI request serving two overlapping pages.
        """
        second_page = create_repository_search_page(150)
        second_page["items"] = [create_public_repository_result("Example/Project"),
                                create_public_repository_result("example/second")]
        request.side_effect = [create_repository_search_page(150, True), second_page]
        receipt = repository_discovery.search_github_repositories("SRTMGL1", max_pages=2)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(len(receipt["results"]), 2)
        self.assertEqual(receipt["pages_returned"], 2)
        self.assertFalse(receipt["has_more"])
        self.assertGreater(self.sleep.call_args.args[0], 6.0)

    @patch("nasa_eo_search.repository_discovery.run_github_json")
    def test_repository_and_code_search_share_request_spacing(self, request: Mock) -> None:
        """Honor the clock left by code search before issuing repository discovery.

        Args:
            request: Mock GitHub CLI request returning a repository result.
        """
        repository_discovery._last_github_search_started = repository_discovery.time.monotonic()
        request.return_value = create_repository_search_page()
        repository_discovery.search_github_repositories("SRTMGL1")
        self.assertGreater(self.sleep.call_args.args[0], 6.0)

    @patch("nasa_eo_search.repository_discovery.run_github_json")
    def test_nonpublic_items_and_extra_fields_never_enter_saved_receipt(self, request: Mock) -> None:
        """Apply a second visibility check and whitelist raw public metadata.

        Args:
            request: Mock response deliberately bypassing CLI sanitization.
        """
        page = create_repository_search_page(4)
        page["omitted_nonpublic"] = 2
        page["items"][0]["unrequested_secret"] = "must-not-persist"
        for visibility in (True, None, "false"):
            candidate = create_public_repository_result("private/secret")
            candidate["private"] = visibility
            page["items"].append(candidate)
        request.return_value = page
        receipt = repository_discovery.search_github_repositories("MOD13Q1")
        encoded_receipt = json.dumps(receipt)
        self.assertNotIn("private/secret", encoded_receipt)
        self.assertNotIn("must-not-persist", encoded_receipt)
        self.assertEqual(receipt["raw"][0]["omitted_nonpublic"], 5)
        self.assertEqual(len(receipt["raw"][0]["items"]), 1)

    @patch("nasa_eo_search.repository_discovery.run_github_json")
    def test_invalid_selection_metadata_is_omitted_and_disclosed(self, request: Mock) -> None:
        """Reject booleans as counts, negative integers, and nonboolean state flags.

        Args:
            request: Mock response containing malformed public metadata fields.
        """
        for field, value in (("fork", 0), ("archived", "false"), ("size", True),
                             ("size", -1), ("stargazers_count", -1), ("stargazers_count", "7")):
            with self.subTest(field=field, value=value):
                page = create_repository_search_page(2)
                malformed = create_public_repository_result("example/bad")
                malformed[field] = value
                page["items"].append(malformed)
                request.return_value = page
                receipt = repository_discovery.search_github_repositories("MOD13Q1")
                self.assertEqual(receipt["status"], "incomplete")
                self.assertEqual(len(receipt["results"]), 1)
                self.assertEqual(receipt["raw"][0]["omitted_malformed"], 1)
                self.assertNotIn("example/bad", json.dumps(receipt))

    @patch("nasa_eo_search.repository_discovery.run_github_json")
    def test_wrong_repository_urls_and_missing_names_are_not_persisted(self, request: Mock) -> None:
        """Refuse external URLs, URL suffixes, unsafe names, and missing required fields.

        Args:
            request: Mock GitHub response with hostile or malformed item identities.
        """
        invalid_items = []
        for field, value in (("html_url", "https://evil.example/x"),
                             ("html_url", "https://github.com/example/project/private"),
                             ("full_name", "../project"), ("full_name", None)):
            candidate = create_public_repository_result()
            candidate[field] = value
            invalid_items.append(candidate)
        missing_metadata = create_public_repository_result()
        del missing_metadata["archived"]
        invalid_items.append(missing_metadata)
        request.return_value = {**create_repository_search_page(5), "items": invalid_items}
        receipt = repository_discovery.search_github_repositories("SPL3SMP")
        self.assertEqual(receipt["results"], [])
        self.assertEqual(receipt["raw"][0]["items"], [])
        self.assertEqual(receipt["raw"][0]["omitted_malformed"], 5)
        self.assertEqual(receipt["status"], "incomplete")

    @patch("nasa_eo_search.repository_discovery.run_github_json")
    def test_forks_and_archived_repositories_remain_public_candidates(self, request: Mock) -> None:
        """Preserve explicit fork/archive flags for later selection and reporting.

        Args:
            request: Mock response containing a public archived fork.
        """
        page = create_repository_search_page()
        page["items"][0].update({"fork": True, "archived": True, "size": 0, "stargazers_count": 0})
        request.return_value = page
        receipt = repository_discovery.search_github_repositories("SRTMGL1")
        self.assertEqual(receipt["status"], "returned")
        self.assertEqual(receipt["results"][0]["repository_metadata"],
                         {"fork": True, "archived": True, "size_kb": 0, "stars": 0})

    @patch("nasa_eo_search.repository_discovery.run_github_json")
    def test_later_failure_preserves_public_results_from_earlier_pages(self, request: Mock) -> None:
        """Keep durable candidate evidence when a subsequent page fails.

        Args:
            request: Mock response followed by a sanitized GitHub request error.
        """
        request.side_effect = [create_repository_search_page(150), RuntimeError("GitHub CLI request failed")]
        receipt = repository_discovery.search_github_repositories("MOD13Q1", max_pages=2)
        self.assertEqual(receipt["status"], "error")
        self.assertEqual(receipt["pages_returned"], 1)
        self.assertEqual(len(receipt["raw"]), 1)
        self.assertEqual(len(receipt["results"]), 1)
        self.assertTrue(receipt["has_more"])

    @patch("nasa_eo_search.repository_discovery.run_github_json")
    def test_invalid_page_metadata_is_an_explicit_error(self, request: Mock) -> None:
        """Distinguish malformed completion records from successful zero-hit searches.

        Args:
            request: Mock response with invalid top-level completion fields.
        """
        invalid_pages = [[], {"items": None}]
        for field, value in (("total_count", True), ("total_count", -1),
                             ("incomplete_results", "false"), ("omitted_nonpublic", True)):
            candidate = create_repository_search_page()
            candidate[field] = value
            invalid_pages.append(candidate)
        for page in invalid_pages:
            with self.subTest(page=page):
                request.return_value = page
                receipt = repository_discovery.search_github_repositories("MOD13Q1")
                self.assertEqual(receipt["status"], "error")
                self.assertEqual(receipt["raw"], [])

    @patch("nasa_eo_search.repository_discovery.run_github_json")
    def test_invalid_terms_and_page_bounds_fail_before_requests(self, request: Mock) -> None:
        """Reject query injection and invalid page bounds without a GitHub call.

        Args:
            request: Mock request asserted unused for invalid arguments.
        """
        for term in ("", "x" * 257, 'MOD13Q1" fork:only', "MOD13Q1\nrepo:private/secret", "a\x00b"):
            with self.subTest(term=term), self.assertRaises(ValueError):
                repository_discovery.search_github_repositories(term)
        for maximum, start in ((0, 1), (11, 1), (True, 1), (1.5, 1), (1, 0), (1, True), (2, 10)):
            with self.subTest(maximum=maximum, start=start), self.assertRaises(ValueError):
                repository_discovery.search_github_repositories("MOD13Q1", maximum, start_page=start)
        request.assert_not_called()

    @patch("nasa_eo_search.repository_discovery.run_github_json")
    def test_backslashes_are_escaped_inside_the_quoted_term(self, request: Mock) -> None:
        """Keep a trailing backslash from escaping the search phrase's closing quote.

        Args:
            request: Mock GitHub request receiving the correctly quoted query.
        """
        request.return_value = create_repository_search_page(0)
        receipt = repository_discovery.search_github_repositories("dataset\\")
        self.assertEqual(receipt["query"], '"dataset\\\\" in:name,description,readme is:public fork:true')


if __name__ == "__main__":
    unittest.main()

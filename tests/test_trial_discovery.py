"""Test bounded provider discovery without making network requests."""

import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from nasa_eo_search import trial_discovery


def github_search_page(total_count: int = 1, incomplete: bool = False) -> dict:
    """Create a public GitHub code-search response for adapter tests.

    Args:
        total_count: Number of hits reported for the authenticated query.
        incomplete: Whether GitHub reports unfinished search work.

    Returns:
        A response containing one public file and completion metadata.
    """

    return {
        "total_count": total_count, "incomplete_results": incomplete,
        "items": [{"path": "src/example.py", "html_url": "https://github.com/example/project/blob/main/src/example.py",
                   "repository": {"full_name": "example/project", "private": False}}],
    }


def grep_search_envelope(text: str | None = None) -> dict:
    """Create a Grep MCP response with a recognizable public file header.

    Args:
        text: Optional replacement result text for malformed/empty fixtures.

    Returns:
        A JSON-RPC tool-response envelope with one text content block.
    """

    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text or (
        "Repository: example/project\nPath: src/example.py\n"
        "URL: https://github.com/example/project/blob/main/src/example.py\n"
        "License: MIT\n\nSnippets:\n--- Snippet 1 (Line 1) ---\nATL03_007\n"
    )}]}}


class GitHubDiscoveryTests(unittest.TestCase):
    """Preserve public-only evidence and accurate query-limit receipts."""

    def setUp(self) -> None:
        """Reset process-local throttling so tests are independent."""

        trial_discovery._last_github_search_started = None

    @patch("nasa_eo_search.trial_discovery.run_github_json")
    def test_search_caps_and_does_not_use_unsupported_public_filter(self, request: Mock) -> None:
        """Mark a one-page sample capped and use documented legacy query syntax.

        Args:
            request: Mock GitHub CLI JSON request.
        """

        request.return_value = github_search_page(total_count=4568)
        receipt = trial_discovery.search_github_code("ATL03")
        self.assertEqual(receipt["status"], "capped")
        self.assertEqual(receipt["results"][0]["repository"], "github.com/example/project")
        self.assertNotIn("is:public", receipt["query"])
        self.assertIn("fork:true", receipt["query"])
        self.assertIn(trial_discovery.PUBLIC_SEARCH_FILTER, request.call_args.args[0])

    @patch("nasa_eo_search.trial_discovery.run_github_json")
    def test_saved_pagination_can_request_one_later_page(self, request: Mock) -> None:
        """Request only the next persisted page without refetching earlier results.

        Args:
            request: Mock GitHub CLI JSON request.
        """

        request.return_value = github_search_page(total_count=250)
        receipt = trial_discovery.search_github_code("ATL03", max_pages=1, start_page=2)
        self.assertIn("page=2", request.call_args.args[0])
        self.assertEqual(receipt["start_page"], 2)
        self.assertEqual(receipt["pages_returned"], 1)
        self.assertTrue(receipt["has_more"])
        self.assertEqual(receipt["status"], "capped")
        for start_page, maximum in ((0, 1), (11, 1), (10, 2), (True, 1)):
            with self.assertRaises(ValueError):
                trial_discovery.search_github_code("ATL03", max_pages=maximum, start_page=start_page)

    @patch("nasa_eo_search.trial_discovery.run_github_json")
    def test_private_and_unknown_visibility_items_never_enter_receipt(self, request: Mock) -> None:
        """Drop private or malformed items even if CLI filtering fails.

        Args:
            request: Mock GitHub CLI JSON request.
        """

        page = github_search_page()
        page["items"].extend([
            {"path": "secret", "repository": {"full_name": "private/secret", "private": True}},
            {"path": "secret", "repository": {"full_name": "unknown/secret"}},
        ])
        request.return_value = page
        receipt = trial_discovery.search_github_code("ATL03")
        self.assertNotIn("private/secret", json.dumps(receipt))
        self.assertNotIn("unknown/secret", json.dumps(receipt))
        self.assertEqual(len(receipt["raw"][0]["items"]), 1)

    @patch("nasa_eo_search.trial_discovery.time.sleep")
    @patch("nasa_eo_search.trial_discovery.run_github_json")
    def test_pagination_throttles_deduplicates_and_keeps_incomplete(self, request: Mock, sleep: Mock) -> None:
        """Retain incomplete status across pages and space consecutive requests.

        Args:
            request: Mock GitHub CLI JSON request.
            sleep: Mock request-spacing wait.
        """

        request.side_effect = [github_search_page(150, True), github_search_page(150)]
        receipt = trial_discovery.search_github_code("ATL03", max_pages=2)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(len(receipt["results"]), 1)
        self.assertEqual(len(receipt["raw"]), 2)
        self.assertGreater(sleep.call_args.args[0], 6.0)

    @patch("nasa_eo_search.trial_discovery.run_github_json")
    def test_errors_return_receipts_and_validation_prevents_network(self, request: Mock) -> None:
        """Save safe service failures but reject invalid bounds before calling.

        Args:
            request: Mock GitHub CLI JSON request.
        """

        request.side_effect = RuntimeError("GitHub CLI request failed")
        self.assertEqual(trial_discovery.search_github_code("ATL03")["status"], "error")
        for max_pages in (0, 11, True, 1.5):
            with self.assertRaises(ValueError):
                trial_discovery.search_github_code("ATL03", max_pages)
        self.assertEqual(request.call_count, 1)

    @patch("nasa_eo_search.trial_discovery.subprocess.run")
    def test_cli_errors_do_not_expose_stderr(self, run_command: Mock) -> None:
        """Omit subprocess error output that might contain credentials.

        Args:
            run_command: Mock GitHub CLI subprocess.
        """

        run_command.return_value = subprocess.CompletedProcess([], 1, b"", b"sensitive-content")
        with self.assertRaises(RuntimeError) as error:
            trial_discovery.run_github_json(["--method", "GET", "search/code"])
        self.assertNotIn("sensitive-content", str(error.exception))

    @patch("nasa_eo_search.trial_discovery.run_github_json")
    def test_commit_resolution_refuses_nonpublic_and_pins_public_sha(self, request: Mock) -> None:
        """Verify visibility before following an immutable default-branch commit.

        Args:
            request: Mock GitHub CLI JSON request.
        """

        request.return_value = {"private": True}
        with self.assertRaises(RuntimeError):
            trial_discovery.resolve_public_repository_commit("example/private")
        self.assertEqual(request.call_count, 1)
        request.reset_mock()
        request.side_effect = [{"private": False, "default_branch": "release/current"}, {"sha": "a" * 40}]
        self.assertEqual(trial_discovery.resolve_public_repository_commit("github.com/example/project"), "a" * 40)
        self.assertIn("repos/example/project/commits/release%2Fcurrent", request.call_args.args[0])


class GrepDiscoveryTests(unittest.TestCase):
    """Check the official MCP transport and conservative coverage reporting."""

    def test_decodes_json_and_sse_responses(self) -> None:
        """Accept JSON and server-sent events without interpreting content as instructions."""

        envelope = grep_search_envelope()
        encoded = json.dumps(envelope).encode()
        self.assertEqual(trial_discovery.decode_mcp_response(encoded, "application/json"), envelope)
        self.assertEqual(trial_discovery.decode_mcp_response(
            b"event: message\ndata: " + encoded + b"\n\n", "text/event-stream",
        ), envelope)
        self.assertEqual(len(trial_discovery.parse_grep_search_results(envelope)), 1)

    def test_empty_unknown_and_error_responses_are_distinguished(self) -> None:
        """Accept explicit zero matches but reject changed formats and tool errors."""

        self.assertEqual(trial_discovery.parse_grep_search_results(
            grep_search_envelope("No results found for your query.")), [])
        for envelope in (grep_search_envelope("Please sign in"), {"error": {"code": 403}},
                         {"result": {"isError": True, "content": []}}):
            with self.assertRaises(ValueError):
                trial_discovery.parse_grep_search_results(envelope)

    @patch("nasa_eo_search.trial_discovery.urlopen")
    def test_grep_success_is_always_limited_and_preserves_boundaries(self, open_url: Mock) -> None:
        """Do not imply exhaustiveness for Grep's unpaginated ranked results.

        Args:
            open_url: Mock HTTP request function.
        """

        response = open_url.return_value.__enter__.return_value
        response.read.return_value = json.dumps(grep_search_envelope()).encode()
        response.headers = {"Content-Type": "application/json"}
        receipt = trial_discovery.search_grep_app("ATL03")
        self.assertEqual(receipt["status"], "limited")
        self.assertEqual(len(receipt["results"]), 1)
        query = json.loads(open_url.call_args.args[0].data)["params"]["arguments"]["query"]
        self.assertEqual(query, "(^|[^A-Za-z0-9])ATL03($|[^A-Za-z0-9])")

    @patch("nasa_eo_search.trial_discovery.urlopen")
    def test_oversized_grep_response_is_error(self, open_url: Mock) -> None:
        """Refuse oversized HTTP responses without attempting to parse them.

        Args:
            open_url: Mock HTTP request function.
        """

        response = open_url.return_value.__enter__.return_value
        response.read.return_value = b"x" * (trial_discovery.MAX_RESPONSE_BYTES + 1)
        self.assertEqual(trial_discovery.search_grep_app("ATL03")["status"], "error")

    def test_unsafe_repository_names_are_rejected(self) -> None:
        """Prevent returned repository names from changing hosts or URL paths."""

        for repository in ("https://evil.example/project", "../project", "owner/..", "owner/repo/path", "owner/a?b"):
            with self.assertRaises(ValueError):
                trial_discovery.validate_repository_name(repository)

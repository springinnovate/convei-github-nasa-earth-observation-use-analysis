"""Offline checks for a single public repository search result."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import unittest
from unittest.mock import patch

from nasa_eo_search.repo_preview import (
    build_preview_query, first_content_match, main, summarize_repository_match,
)
from nasa_eo_search.sourcegraph import ServerSentEvent, SourcegraphProtocolError


EXAMPLE_MATCH = {
    "type": "content", "repository": "github.com/example/project",
    "path": "read data.py", "commit": "abc123", "repoStars": 5,
    "language": "Python",
    "lineMatches": [{"lineNumber": 4, "line": 'load("ATL03")'}],
}


class RepositoryPreviewTests(unittest.TestCase):
    """Verify literal queries, first-result shutdown, evidence, and empty results."""

    def test_query_quotes_phrase_and_limits_public_scope(self) -> None:
        """Keep special characters inside the literal content parameter."""

        search_phrase = 'ATL03 "quoted" \\ repo:elsewhere'
        search_query = build_preview_query(search_phrase, False)
        self.assertIn('content:' + json.dumps(search_phrase), search_query)
        self.assertIn(r'repo:^github\.com/', search_query)
        self.assertIn('visibility:public', search_query)
        self.assertIn('count:1 timeout:15s', search_query)
        self.assertNotIn('repo:', build_preview_query('ATL03', True))

    def test_first_match_stops_consumption_and_closes_connection(self) -> None:
        """Stop at the first match and close the generator before printing results."""

        connection_closed = []

        def recorded_events():
            """Yield a result and record closure without advancing past the match.

            Yields:
                A Sourcegraph matches event containing two files.
            """

            try:
                yield ServerSentEvent("matches", json.dumps([EXAMPLE_MATCH, EXAMPLE_MATCH]))
                self.fail("The preview consumed events after finding its first match.")
            finally:
                connection_closed.append(True)

        results_output = StringIO()
        with (
            patch("nasa_eo_search.repo_preview.stream_sourcegraph_search_events",
                  return_value=recorded_events()) as stream_search_mock,
            redirect_stdout(results_output), redirect_stderr(StringIO()),
        ):
            self.assertEqual(main(["ATL03"]), 0)
            stream_search_mock.assert_called_once()
        self.assertEqual(connection_closed, [True])
        result_record = json.loads(results_output.getvalue())
        self.assertEqual(result_record["match"]["repository"], EXAMPLE_MATCH["repository"])

    def test_match_has_clickable_file_and_one_based_lines(self) -> None:
        """Preserve the indexed revision and convert code lines for human readers."""

        match_summary = summarize_repository_match(EXAMPLE_MATCH)
        self.assertEqual(match_summary["matching_lines"][0]["line_number"], 5)
        self.assertEqual(match_summary["file_url"],
                         "https://github.com/example/project/blob/abc123/read%20data.py")
        self.assertEqual(match_summary["stars"], 5)

    def test_empty_search_preserves_limit_warnings(self) -> None:
        """Show incomplete-search warnings even when the server returns no matches."""

        diagnostics_output = StringIO()
        search_events = [
            ServerSentEvent("progress", json.dumps({"skipped": [{"reason": "timeout"}]})),
            ServerSentEvent("done", "{}"),
        ]
        self.assertIsNone(first_content_match(search_events, diagnostics_output))
        self.assertIn("timeout", diagnostics_output.getvalue())

    def test_incomplete_and_invalid_streams_fail(self) -> None:
        """Do not interpret broken or malformed responses as empty search results."""

        for search_events in ([], [ServerSentEvent("matches", "{}")]):
            with self.assertRaises(SourcegraphProtocolError):
                first_content_match(search_events, StringIO())


if __name__ == "__main__":
    unittest.main()

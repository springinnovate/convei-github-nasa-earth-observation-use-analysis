"""Offline checks for a single public repository search result."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import re
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
        """Escape regex and query syntax in the user-supplied phrase."""

        search_phrase = 'ATL03 "quoted" \\ repo:elsewhere'
        search_query = build_preview_query(search_phrase, False)
        decoded_pattern = search_query.split('content:', 1)[1].rsplit(' count:', 1)[0]
        self.assertNotIn(' ', decoded_pattern)
        self.assertNotIn('"', decoded_pattern)
        # Python re uses a different syntax for the RE2 Unicode hex escapes.
        decoded_pattern = re.sub(
            r'\\x\{([0-9a-f]+)\}',
            lambda hex_match: re.escape(chr(int(hex_match.group(1), 16))),
            decoded_pattern,
        )
        self.assertIsNotNone(re.search(decoded_pattern, search_phrase))
        self.assertIn('patternType:regexp case:no', search_query)
        self.assertIn(r'repo:^github\.com/', search_query)
        self.assertIn('visibility:public', search_query)
        self.assertIn('count:1 timeout:15s', search_query)
        self.assertNotIn('repo:', build_preview_query('ATL03', True))

    def test_product_boundaries_reject_substrings_and_keep_filenames(self) -> None:
        """Reject MATL03 and suffix collisions while retaining product tokens."""

        search_query = build_preview_query("ATL03", False)
        encoded_pattern = search_query.split('content:', 1)[1].rsplit(' count:', 1)[0]
        search_pattern = re.compile(encoded_pattern, re.IGNORECASE)
        for file_contents in (
            "MATL03", "matl03", "ATL030", "ATL03X", "prefixATL03", "xATL03x",
        ):
            with self.subTest(file_contents=file_contents):
                self.assertIsNone(search_pattern.search(file_contents))
        for file_contents in (
            "ATL03", 'load("ATL03")', "ATL03_007", "ATL03.h5", "ATL03-007",
            "/data/ATL03/file.h5", "test_atl03", "read ATL03 data",
        ):
            with self.subTest(file_contents=file_contents):
                self.assertIsNotNone(search_pattern.search(file_contents))

    def test_domain_punctuation_remains_literal(self) -> None:
        """Do not allow regex dots to turn a domain into wildcard matches."""

        search_query = build_preview_query("earthdata.nasa.gov", False)
        encoded_pattern = search_query.split('content:', 1)[1].rsplit(' count:', 1)[0]
        search_pattern = re.compile(encoded_pattern, re.IGNORECASE)
        self.assertIsNotNone(search_pattern.search("https://earthdata.nasa.gov/"))
        self.assertIsNone(search_pattern.search("earthdataXnasaXgov"))
        self.assertIsNone(search_pattern.search("notearthdata.nasa.gov"))

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

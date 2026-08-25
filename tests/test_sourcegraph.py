from __future__ import annotations

from io import StringIO
import json
import os
import ssl
import unittest
from unittest.mock import patch

import certifi

from nasa_eo_search.sourcegraph import (
    ServerSentEvent,
    SourcegraphProtocolError,
    build_sourcegraph_search_request,
    build_tls_context,
    decode_sourcegraph_event_payload,
    parse_server_sent_events,
    write_search_results,
)


class ServerSentEventTests(unittest.TestCase):
    """Verify decoding of Sourcegraph server-sent event framing."""

    def test_parses_multiple_events_comments_and_multiline_data(self) -> None:
        """Parse event names, comments, and payloads split across data fields."""

        response_lines = [
            b": heartbeat\n",
            b"event: matches\n",
            b'data: [{"type":"content",\n',
            b'data: "path":"demo.py"}]\n',
            b"\n",
            b"event: done\n",
            b"data: {}\n",
            b"\n",
        ]

        parsed_events = list(parse_server_sent_events(response_lines))

        self.assertEqual(
            parsed_events,
            [
                ServerSentEvent(
                    "matches", '[{"type":"content",\n"path":"demo.py"}]'
                ),
                ServerSentEvent("done", "{}"),
            ],
        )

    def test_emits_pending_event_at_end_of_stream(self) -> None:
        """Emit a complete final event when the response omits a blank line."""

        parsed_events = list(
            parse_server_sent_events(["event: done\n", "data: {}\n"])
        )
        self.assertEqual(parsed_events, [ServerSentEvent("done", "{}")])

    def test_rejects_invalid_json(self) -> None:
        """Raise a protocol error when an event does not contain valid JSON."""

        with self.assertRaises(SourcegraphProtocolError):
            decode_sourcegraph_event_payload(
                ServerSentEvent("matches", "not-json")
            )


class RequestTests(unittest.TestCase):
    """Verify construction of Sourcegraph HTTP requests and TLS contexts."""

    def test_builds_one_v3_stream_request_with_optional_token(self) -> None:
        """Build one authenticated V3 event-stream request for the query."""

        search_request = build_sourcegraph_search_request(
            "https://sourcegraph.example/.api/search/stream",
            "earthdata.nasa.gov count:10",
            "secret",
        )

        self.assertIn("q=earthdata.nasa.gov+count%3A10", search_request.full_url)
        self.assertIn("v=V3", search_request.full_url)
        self.assertEqual(search_request.get_header("Accept"), "text/event-stream")
        self.assertEqual(
            search_request.get_header("Authorization"), "token secret"
        )

    @patch("nasa_eo_search.sourcegraph.ssl.create_default_context")
    def test_tls_context_uses_certifi_instead_of_windows_store(
        self, create_default_context_mock
    ) -> None:
        """Use Certifi instead of the Windows certificate store by default.

        Args:
            create_default_context_mock: Mocked TLS-context factory.
        """

        expected_tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        create_default_context_mock.return_value = expected_tls_context

        with patch.dict(os.environ, {}, clear=True):
            tls_context = build_tls_context(None)

        self.assertIs(tls_context, expected_tls_context)
        create_default_context_mock.assert_called_once_with(cafile=certifi.where())

    @patch("nasa_eo_search.sourcegraph.ssl.create_default_context")
    def test_tls_context_honors_explicit_ca_bundle(
        self, create_default_context_mock
    ) -> None:
        """Prefer an explicitly supplied CA bundle over the Certifi default.

        Args:
            create_default_context_mock: Mocked TLS-context factory.
        """

        expected_tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        create_default_context_mock.return_value = expected_tls_context

        tls_context = build_tls_context("company-ca.pem")

        self.assertIs(tls_context, expected_tls_context)
        create_default_context_mock.assert_called_once_with(cafile="company-ca.pem")


class OutputTests(unittest.TestCase):
    """Verify JSON Lines output and diagnostic summaries."""

    def test_flattens_matches_as_json_lines_and_summarizes_progress(self) -> None:
        """Flatten matches as JSON Lines and summarize final progress counts."""

        search_events = [
            ServerSentEvent(
                "matches",
                json.dumps(
                    [
                        {
                            "type": "content",
                            "repository": "github.com/example/repo",
                            "path": "search\u202fresult.py",
                        }
                    ]
                ),
            ),
            ServerSentEvent(
                "progress",
                json.dumps(
                    {
                        "done": True,
                        "matchCount": 1,
                        "repositoriesCount": 1,
                    }
                ),
            ),
            ServerSentEvent("done", "{}"),
        ]
        results_output = StringIO()
        diagnostics_output = StringIO()

        written_record_count = write_search_results(
            search_events,
            "earthdata",
            results_output,
            diagnostics_output,
        )

        self.assertEqual(written_record_count, 1)
        output_record = json.loads(results_output.getvalue())
        self.assertEqual(output_record["query"], "earthdata")
        self.assertEqual(output_record["match"]["path"], "search\u202fresult.py")
        self.assertIn("\\u202f", results_output.getvalue())
        self.assertIn("matches=1", diagnostics_output.getvalue())
        self.assertIn("repositories=1", diagnostics_output.getvalue())

    def test_raw_mode_writes_non_match_events(self) -> None:
        """Write non-match events when raw event output is requested."""

        results_output = StringIO()
        diagnostics_output = StringIO()

        write_search_results(
            [ServerSentEvent("done", "{}")],
            "earthdata",
            results_output,
            diagnostics_output,
            write_raw_events=True,
        )

        self.assertEqual(
            json.loads(results_output.getvalue()), {"event": "done", "data": {}}
        )


if __name__ == "__main__":
    unittest.main()

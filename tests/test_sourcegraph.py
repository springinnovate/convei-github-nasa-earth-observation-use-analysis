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
    build_request,
    build_ssl_context,
    decode_event,
    emit_results,
    iter_sse,
)


class ServerSentEventTests(unittest.TestCase):
    def test_parses_multiple_events_comments_and_multiline_data(self) -> None:
        stream = [
            b": heartbeat\n",
            b"event: matches\n",
            b'data: [{"type":"content",\n',
            b'data: "path":"demo.py"}]\n',
            b"\n",
            b"event: done\n",
            b"data: {}\n",
            b"\n",
        ]

        events = list(iter_sse(stream))

        self.assertEqual(
            events,
            [
                ServerSentEvent(
                    "matches", '[{"type":"content",\n"path":"demo.py"}]'
                ),
                ServerSentEvent("done", "{}"),
            ],
        )

    def test_emits_pending_event_at_end_of_stream(self) -> None:
        events = list(iter_sse(["event: done\n", "data: {}\n"]))
        self.assertEqual(events, [ServerSentEvent("done", "{}")])

    def test_rejects_invalid_json(self) -> None:
        with self.assertRaises(SourcegraphProtocolError):
            decode_event(ServerSentEvent("matches", "not-json"))


class RequestTests(unittest.TestCase):
    def test_builds_one_v3_stream_request_with_optional_token(self) -> None:
        request = build_request(
            "https://sourcegraph.example/.api/search/stream",
            "earthdata.nasa.gov count:10",
            "secret",
        )

        self.assertIn("q=earthdata.nasa.gov+count%3A10", request.full_url)
        self.assertIn("v=V3", request.full_url)
        self.assertEqual(request.get_header("Accept"), "text/event-stream")
        self.assertEqual(request.get_header("Authorization"), "token secret")

    @patch("nasa_eo_search.sourcegraph.ssl.create_default_context")
    def test_tls_context_uses_certifi_instead_of_windows_store(self, create_context) -> None:
        expected_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        create_context.return_value = expected_context

        with patch.dict(os.environ, {}, clear=True):
            context = build_ssl_context(None)

        self.assertIs(context, expected_context)
        create_context.assert_called_once_with(cafile=certifi.where())

    @patch("nasa_eo_search.sourcegraph.ssl.create_default_context")
    def test_tls_context_honors_explicit_ca_bundle(self, create_context) -> None:
        expected_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        create_context.return_value = expected_context

        context = build_ssl_context("company-ca.pem")

        self.assertIs(context, expected_context)
        create_context.assert_called_once_with(cafile="company-ca.pem")


class OutputTests(unittest.TestCase):
    def test_flattens_matches_as_json_lines_and_summarizes_progress(self) -> None:
        events = [
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
        output = StringIO()
        diagnostics = StringIO()

        count = emit_results(events, "earthdata", output, diagnostics)

        self.assertEqual(count, 1)
        record = json.loads(output.getvalue())
        self.assertEqual(record["query"], "earthdata")
        self.assertEqual(record["match"]["path"], "search\u202fresult.py")
        self.assertIn("\\u202f", output.getvalue())
        self.assertIn("matches=1", diagnostics.getvalue())
        self.assertIn("repositories=1", diagnostics.getvalue())

    def test_raw_mode_writes_non_match_events(self) -> None:
        output = StringIO()
        diagnostics = StringIO()

        emit_results(
            [ServerSentEvent("done", "{}")],
            "earthdata",
            output,
            diagnostics,
            raw_events=True,
        )

        self.assertEqual(json.loads(output.getvalue()), {"event": "done", "data": {}})


if __name__ == "__main__":
    unittest.main()

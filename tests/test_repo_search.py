"""Verify persistence and completion reporting for full-result searches."""

from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.repo_preview import build_repository_query
from nasa_eo_search.repo_search import collect_search_results, main
from nasa_eo_search.sourcegraph import ServerSentEvent, SourcegraphError


class FullSearchTests(unittest.TestCase):
    """Exercise multi-event collection and honest partial-run reporting."""

    def test_query_includes_all_results_forks_and_archives(self) -> None:
        """Remove the preview limit while retaining the bounded product pattern."""

        search_query = build_repository_query("ATL03", False, collect_all=True)
        self.assertIn("count:all", search_query)
        self.assertIn("fork:yes archived:yes", search_query)
        self.assertIn("timeout:60s", search_query)
        self.assertIn("content:(^|[^A-Za-z0-9])ATL03", search_query)
        self.assertNotIn("count:1", search_query)

    def test_saves_multiple_batches_and_counts_distinct_repositories(self) -> None:
        """Continue beyond the first match and preserve each raw match object."""

        matches_output = StringIO()
        events_output = StringIO()
        search_events = [
            ServerSentEvent("matches", json.dumps([
                {"repository": "github.com/a/a", "path": "one.py"},
                {"repository": "github.com/a/a", "path": "two.py"},
            ])),
            ServerSentEvent("matches", json.dumps([
                {"repository": "github.com/b/b", "path": "three.py"},
            ])),
            ServerSentEvent("progress", '{"done":true,"matchCount":3}'),
            ServerSentEvent("done", "{}"),
        ]
        run_summary = collect_search_results(
            search_events, "query", matches_output, events_output, StringIO(),
        )
        self.assertEqual(run_summary["status"], "finished_no_reported_limits")
        self.assertEqual(run_summary["repositories_with_matches"], 2)
        saved_records = [json.loads(record) for record in matches_output.getvalue().splitlines()]
        self.assertEqual(len(saved_records), 3)
        self.assertEqual(saved_records[-1]["match"]["path"], "three.py")
        self.assertIn('"done": true', events_output.getvalue())

    def test_limits_alerts_and_missing_completion_are_incomplete(self) -> None:
        """A done event alone does not establish an exhaustive search."""

        for search_events in (
            [],
            [ServerSentEvent("done", "{}")],
            [ServerSentEvent("progress", '{"done":true}')],
            [ServerSentEvent("progress", '{"done":true,"skipped":[{"reason":"timeout"}]}'),
             ServerSentEvent("done", "{}")],
            [ServerSentEvent("alert", '{"message":"backend unavailable"}'),
             ServerSentEvent("progress", '{"done":true}'), ServerSentEvent("done", "{}")],
        ):
            with self.subTest(search_events=search_events):
                run_summary = collect_search_results(
                    search_events, "query", StringIO(), StringIO(), StringIO(),
                )
                self.assertEqual(run_summary["status"], "incomplete")

    def test_interruptions_and_errors_preserve_partial_matches(self) -> None:
        """Retain streamed results and mark failures instead of claiming completion."""

        for search_error, expected_status in (
            (SourcegraphError("disconnected"), "failed"),
            (KeyboardInterrupt(), "interrupted"),
        ):
            def interrupted_events():
                """Emit one saved match before simulating an interrupted search.

                Yields:
                    A match event followed by an injected search exception.
                """

                yield ServerSentEvent("matches", '[{"repository":"github.com/a/a"}]')
                raise search_error

            matches_output = StringIO()
            run_summary = collect_search_results(
                interrupted_events(), "query", matches_output, StringIO(), StringIO(),
            )
            self.assertEqual(run_summary["status"], expected_status)
            self.assertEqual(run_summary["match_records_saved"], 1)
            self.assertIn("github.com/a/a", matches_output.getvalue())

    def test_command_persists_status_and_refuses_existing_directory(self) -> None:
        """Write a run summary and avoid overwriting prior collected data."""

        with TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "run"
            with (
                patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events",
                      return_value=iter(())) as stream_mock,
                redirect_stderr(StringIO()),
            ):
                # The real search iterator has close(); use an empty generator here.
                stream_mock.return_value = (event for event in [])
                self.assertEqual(main(["ATL03", "--output", str(output_directory)]), 4)
                run_summary = json.loads((output_directory / "summary.json").read_text())
                self.assertEqual(run_summary["status"], "incomplete")
                self.assertTrue((output_directory / "matches.jsonl").is_file())
                self.assertTrue((output_directory / "events.jsonl").is_file())
                self.assertEqual(main(["ATL03", "--output", str(output_directory)]), 1)
                stream_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()

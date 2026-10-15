"""Verify finite retries, durable checkpoints, and exact partition collection."""

from contextlib import redirect_stderr
import csv
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from io import BytesIO, StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from nasa_eo_search.batch_files import COMPLETED_SEARCH, list_search_attempts
from nasa_eo_search.batch_plan import build_batch_search_plan
from nasa_eo_search.batch_reports import write_batch_reports
from nasa_eo_search.partition_search import execute_partitioned_searches
from nasa_eo_search.search_partitions import partition_catalog_plan
from nasa_eo_search.sourcegraph import (
    DEFAULT_SOURCEGRAPH_ENDPOINT, ServerSentEvent, SourcegraphError,
    parse_retry_after_seconds, stream_sourcegraph_search_events,
)
from tests.test_repo_batch import create_batch_inputs


def create_partition_fixture(parent_directory: str, job_count: int = 1):
    """Create temporary catalog inputs and selected runner-unit job fixtures.

    Args:
        parent_directory: Test-owned temporary directory.
        job_count: Number of partition jobs to exercise without unrelated jobs.

    Returns:
        Existing output directory and schema-2 plan with selected fixture jobs.
    """

    catalog, selection, output_directory = create_batch_inputs(parent_directory)
    plan = partition_catalog_plan(build_batch_search_plan(catalog, selection, False, 20))
    plan["jobs"] = plan["jobs"][:job_count]
    output_directory.mkdir()
    return output_directory, plan


def partition_events(status: str = "complete", repository: str | None = None):
    """Yield mocked completion evidence with an optional file match.

    Args:
        status: complete, incomplete, failed, or interrupted request outcome.
        repository: Optional repository name for a raw match.

    Yields:
        Real collector event objects without contacting Sourcegraph.

    Raises:
        SourcegraphError: For a simulated transport failure.
        KeyboardInterrupt: For a simulated interruption.
    """

    if repository:
        yield ServerSentEvent("matches", json.dumps([{
            "type": "content", "repository": repository, "path": "example.py",
            "commit": "abc123", "lineMatches": [{"lineNumber": 4, "line": 'load("ATL03")'}],
        }]))
    if status == "failed":
        raise SourcegraphError("temporary network failure")
    if status == "interrupted":
        raise KeyboardInterrupt()
    warnings = [{"reason": "timeout", "message": "Search timed out."}] if status == "incomplete" else []
    yield ServerSentEvent("progress", json.dumps({"done": True, "skipped": warnings}))
    yield ServerSentEvent("done", "{}")


def http_failure_events(http_status: int, retry_after_seconds: float | None = None):
    """Raise a mocked HTTP error while remaining a closeable event generator.

    Args:
        http_status: HTTP response status to preserve in the attempt summary.
        retry_after_seconds: Optional server-provided retry delay.

    Yields:
        No events because the request fails before producing a stream.

    Raises:
        SourcegraphError: Always, with the requested HTTP metadata.
    """

    yield from ()
    raise SourcegraphError(
        "HTTP request failed", http_status=http_status,
        retry_after_seconds=retry_after_seconds,
    )


class PartitionSearchTests(unittest.TestCase):
    """Exercise the real collector against mocked sequential event streams."""

    def test_exact_queries_complete_zero_results_and_refresh_reports_twice(self) -> None:
        """Send saved queries unchanged and keep checkpoints small per request."""

        with TemporaryDirectory() as temporary_directory:
            output, plan = create_partition_fixture(temporary_directory, 9)
            observed_checkpoints = []

            def completed_stream(*arguments, **keyword_arguments):
                """Inspect the before-request checkpoint and emit complete events.

                Args:
                    arguments: Collector request arguments inspected by the mock.
                    keyword_arguments: Collector transport options.

                Returns:
                    A fresh complete event generator.
                """

                observed_checkpoints.append(json.loads((output / "progress.json").read_text()))
                return partition_events()

            with (
                patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=completed_stream) as stream_mock,
                patch("nasa_eo_search.partition_search.time.sleep") as sleep_mock,
                patch("nasa_eo_search.partition_search.write_batch_reports", wraps=write_batch_reports) as reports_mock,
                redirect_stderr(StringIO()),
            ):
                self.assertEqual(execute_partitioned_searches(output, plan, "private.pem"), 0)
            self.assertEqual(reports_mock.call_count, 2)
            self.assertEqual(stream_mock.call_count, 9)
            self.assertEqual(sleep_mock.call_count, 8)
            for index, (request_call, checkpoint) in enumerate(zip(stream_mock.call_args_list, observed_checkpoints)):
                self.assertEqual(request_call.args[1], plan["jobs"][index]["query"])
                self.assertEqual(request_call.args[3], 20.0)
                self.assertEqual(request_call.kwargs["ca_bundle_path"], "private.pem")
                self.assertEqual(checkpoint["searches_by_status"][COMPLETED_SEARCH], index)
                self.assertEqual(checkpoint["current_job"]["query"], plan["jobs"][index]["query"])
                self.assertFalse(checkpoint["combined_reports_current"])
                self.assertNotIn("jobs", checkpoint)
            with patch("nasa_eo_search.partition_search.collect_repository_query") as collector_mock, redirect_stderr(StringIO()):
                self.assertEqual(execute_partitioned_searches(output, plan, None), 0)
                collector_mock.assert_not_called()

    def test_request_cap_pauses_and_resume_skips_completed_partitions(self) -> None:
        """Resume the next pending partition without repeating completed work."""

        with TemporaryDirectory() as temporary_directory:
            output, plan = create_partition_fixture(temporary_directory, 3)
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=[partition_events(), partition_events(), partition_events()]) as stream_mock, patch("nasa_eo_search.partition_search.time.sleep"), redirect_stderr(StringIO()):
                self.assertEqual(execute_partitioned_searches(output, plan, None, max_requests=1), 4)
                progress = json.loads((output / "progress.json").read_text())
                self.assertEqual(progress["stop_reason"], "request_limit_reached")
                self.assertEqual(progress["searches_by_status"][COMPLETED_SEARCH], 1)
                self.assertEqual(execute_partitioned_searches(output, plan, None), 0)
                self.assertEqual(stream_mock.call_count, 3)

    def test_zero_results_with_warning_remain_incomplete_and_retry_budget_is_finite(self) -> None:
        """Do not turn timeout-empty responses or repeated resumes into success."""

        with TemporaryDirectory() as temporary_directory:
            output, plan = create_partition_fixture(temporary_directory)
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=[partition_events("incomplete"), partition_events("incomplete")]) as stream_mock, patch("nasa_eo_search.partition_search.time.sleep") as sleep_mock, redirect_stderr(StringIO()):
                self.assertEqual(execute_partitioned_searches(output, plan, None), 4)
                self.assertEqual(execute_partitioned_searches(output, plan, None), 4)
                self.assertEqual(stream_mock.call_count, 2)
                sleep_mock.assert_called_once_with(2.0)
            progress = json.loads((output / "progress.json").read_text())
            self.assertEqual(progress["searches_by_status"]["incomplete"], 1)
            self.assertEqual(progress["attempts_recorded"], 2)
            self.assertEqual(progress["stop_reason"], "retry_budget_exhausted")

    def test_explicit_retry_grant_survives_pause_without_expanding_on_resume(self) -> None:
        """Persist a finite extra allowance across an intentional request cap."""

        with TemporaryDirectory() as temporary_directory:
            output, plan = create_partition_fixture(temporary_directory)
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=[partition_events("incomplete") for _ in range(4)]) as stream_mock, patch("nasa_eo_search.partition_search.time.sleep"), redirect_stderr(StringIO()):
                self.assertEqual(execute_partitioned_searches(output, plan, None), 4)
                self.assertEqual(execute_partitioned_searches(output, plan, None, max_requests=1, retry_incomplete=True), 4)
                self.assertEqual(execute_partitioned_searches(output, plan, None), 4)
                self.assertEqual(execute_partitioned_searches(output, plan, None), 4)
                self.assertEqual(stream_mock.call_count, 4)
            self.assertEqual(len(list_search_attempts(output, plan["jobs"][0]["search_id"])), 4)

    def test_three_consecutive_failures_stop_before_remaining_jobs(self) -> None:
        """Stop globally rather than retrying every partition during an outage."""

        for outcome, expected_exit in (("failed", 1), ("incomplete", 4)):
            with self.subTest(outcome=outcome), TemporaryDirectory() as temporary_directory:
                output, plan = create_partition_fixture(temporary_directory, 5)
                with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=[partition_events(outcome) for _ in range(3)]) as stream_mock, patch("nasa_eo_search.partition_search.time.sleep"), redirect_stderr(StringIO()):
                    self.assertEqual(execute_partitioned_searches(output, plan, None), expected_exit)
                    self.assertEqual(stream_mock.call_count, 3)
                progress = json.loads((output / "progress.json").read_text())
                self.assertEqual(progress["stop_reason"], "three_consecutive_unsuccessful_requests")
                self.assertEqual(progress["searches_by_status"]["pending"], 3)

    def test_authentication_and_invalid_request_errors_stop_immediately(self) -> None:
        """Require attention instead of automatically repeating HTTP 400/401/403."""

        for http_status in (400, 401, 403):
            with self.subTest(http_status=http_status), TemporaryDirectory() as temporary_directory:
                output, plan = create_partition_fixture(temporary_directory, 3)
                with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", return_value=http_failure_events(http_status)) as stream_mock, redirect_stderr(StringIO()):
                    self.assertEqual(execute_partitioned_searches(output, plan, None), 1)
                    stream_mock.assert_called_once()
                progress = json.loads((output / "progress.json").read_text())
                self.assertEqual(progress["stop_reason"], f"http_{http_status}_requires_attention")

    def test_long_retry_after_is_durable_and_blocks_ordinary_resume(self) -> None:
        """Preserve Retry-After in attempt evidence and resume only after its deadline."""

        with TemporaryDirectory() as temporary_directory:
            output, plan = create_partition_fixture(temporary_directory)
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", return_value=http_failure_events(429, 120)) as stream_mock, patch("nasa_eo_search.partition_search.time.sleep") as sleep_mock, redirect_stderr(StringIO()):
                self.assertEqual(execute_partitioned_searches(output, plan, None), 4)
                self.assertEqual(execute_partitioned_searches(output, plan, None), 4)
                stream_mock.assert_called_once()
                sleep_mock.assert_not_called()
            progress = json.loads((output / "progress.json").read_text())
            self.assertEqual(progress["stop_reason"], "retry_after_not_before")
            attempt = list_search_attempts(output, plan["jobs"][0]["search_id"])[0]
            attempt_summary = json.loads((attempt / "summary.json").read_text())
            self.assertEqual(attempt_summary["http_status"], 429)
            self.assertEqual(attempt_summary["retry_after_seconds"], 120)
            # A misleading progress cache cannot authorize an early request.
            (output / "progress.json").write_text('{}', encoding="utf-8")
            with patch("nasa_eo_search.partition_search.time.time", return_value=progress["not_before_epoch"] + 1), patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", return_value=partition_events()), redirect_stderr(StringIO()):
                self.assertEqual(execute_partitioned_searches(output, plan, None), 0)

    def test_short_retry_after_is_observed_before_one_retry(self) -> None:
        """Honor server guidance without converting it into an unbounded loop."""

        with TemporaryDirectory() as temporary_directory:
            output, plan = create_partition_fixture(temporary_directory)
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=[http_failure_events(429, 5), partition_events()]), patch("nasa_eo_search.partition_search.time.sleep") as sleep_mock, redirect_stderr(StringIO()):
                self.assertEqual(execute_partitioned_searches(output, plan, None), 0)
            self.assertEqual(sleep_mock.call_count, 1)
            self.assertGreater(sleep_mock.call_args.args[0], 4)

    def test_service_unavailable_retry_after_also_prevents_early_requests(self) -> None:
        """Respect HTTP 503 server guidance as well as rate-limit responses."""

        with TemporaryDirectory() as temporary_directory:
            output, plan = create_partition_fixture(temporary_directory, 2)
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", return_value=http_failure_events(503, 120)) as stream_mock, patch("nasa_eo_search.partition_search.time.sleep") as sleep_mock, redirect_stderr(StringIO()):
                self.assertEqual(execute_partitioned_searches(output, plan, None), 4)
                self.assertEqual(execute_partitioned_searches(output, plan, None), 4)
                stream_mock.assert_called_once()
                sleep_mock.assert_not_called()

    def test_interrupt_during_delay_keeps_completed_counts(self) -> None:
        """Refresh reports after Ctrl+C between requests without creating an attempt."""

        with TemporaryDirectory() as temporary_directory:
            output, plan = create_partition_fixture(temporary_directory, 3)
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", return_value=partition_events()) as stream_mock, patch("nasa_eo_search.partition_search.time.sleep", side_effect=KeyboardInterrupt()), redirect_stderr(StringIO()):
                self.assertEqual(execute_partitioned_searches(output, plan, None), 130)
                stream_mock.assert_called_once()
            progress = json.loads((output / "progress.json").read_text())
            self.assertEqual(progress["searches_by_status"][COMPLETED_SEARCH], 1)
            self.assertEqual(progress["searches_by_status"]["pending"], 2)
            self.assertEqual(progress["requests_this_invocation"], 1)

    def test_interrupt_after_attempt_creation_reconciles_running_attempt(self) -> None:
        """Account for a collector interrupted before it can finalize its summary."""

        with TemporaryDirectory() as temporary_directory:
            output, plan = create_partition_fixture(temporary_directory)

            def interrupted_collector(query, attempt_directory, ca_bundle_path, timeout_seconds):
                """Write the initial attempt checkpoint and simulate process interruption.

                Args:
                    query: Saved partition query to preserve.
                    attempt_directory: New temporary attempt directory.
                    ca_bundle_path: Unused mocked certificate option.
                    timeout_seconds: Unused mocked timeout option.

                Raises:
                    KeyboardInterrupt: Before a final summary is available.
                """

                attempt_directory.mkdir(parents=True)
                (attempt_directory / "summary.json").write_text(json.dumps({
                    "query": query, "endpoint": DEFAULT_SOURCEGRAPH_ENDPOINT, "status": "running",
                }), encoding="utf-8")
                (attempt_directory / "matches.jsonl").write_text("", encoding="utf-8")
                (attempt_directory / "events.jsonl").write_text("", encoding="utf-8")
                raise KeyboardInterrupt()

            with patch("nasa_eo_search.partition_search.collect_repository_query", side_effect=interrupted_collector), redirect_stderr(StringIO()):
                self.assertEqual(execute_partitioned_searches(output, plan, None), 130)
            progress = json.loads((output / "progress.json").read_text())
            self.assertEqual(progress["searches_by_status"]["interrupted"], 1)
            self.assertEqual(progress["attempts_recorded"], 1)
            self.assertEqual(progress["current_job"]["status"], "interrupted")

    def test_success_resets_invocation_failure_streak(self) -> None:
        """Continue after separated failures rather than counting all failures together."""

        with TemporaryDirectory() as temporary_directory:
            output, plan = create_partition_fixture(temporary_directory, 3)
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=[partition_events("failed"), partition_events(), partition_events("failed"), partition_events(), partition_events()]) as stream_mock, patch("nasa_eo_search.partition_search.time.sleep"), redirect_stderr(StringIO()):
                self.assertEqual(execute_partitioned_searches(output, plan, None), 0)
                self.assertEqual(stream_mock.call_count, 5)

    def test_output_error_reconciles_attempt_before_final_reporting(self) -> None:
        """Keep final counts consistent when output failure interrupts accounting."""

        with TemporaryDirectory() as temporary_directory:
            output, plan = create_partition_fixture(temporary_directory)

            def failing_collector(query, attempt_directory, ca_bundle_path, timeout_seconds):
                """Preserve a started attempt before simulating a local output failure.

                Args:
                    query: Saved partition query to preserve.
                    attempt_directory: New temporary attempt directory.
                    ca_bundle_path: Unused mocked certificate option.
                    timeout_seconds: Unused mocked timeout option.

                Raises:
                    OSError: After initial durable evidence has been written.
                """

                attempt_directory.mkdir(parents=True)
                (attempt_directory / "summary.json").write_text(json.dumps({
                    "query": query, "endpoint": DEFAULT_SOURCEGRAPH_ENDPOINT, "status": "running",
                }), encoding="utf-8")
                (attempt_directory / "matches.jsonl").write_text("", encoding="utf-8")
                (attempt_directory / "events.jsonl").write_text("", encoding="utf-8")
                raise OSError("simulated output failure")

            with patch("nasa_eo_search.partition_search.collect_repository_query", side_effect=failing_collector), redirect_stderr(StringIO()):
                with self.assertRaisesRegex(OSError, "simulated output failure"):
                    execute_partitioned_searches(output, plan, None)
            progress = json.loads((output / "progress.json").read_text())
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(progress["attempts_recorded"], 1)
            self.assertEqual(progress["searches_by_status"]["interrupted"], 1)
            self.assertEqual(progress["current_job"]["status"], "interrupted")
            self.assertEqual(progress["stop_reason"], "local_output_or_state_error")
            self.assertEqual(progress["status"], "failed")
            self.assertEqual(summary["searches_by_status"], progress["searches_by_status"])
            with (output / "searches.csv").open(encoding="utf-8-sig", newline="") as report_input:
                self.assertEqual(next(csv.DictReader(report_input))["status"], "interrupted")

    def test_keyboard_interrupt_preserves_partial_results_and_current_counts(self) -> None:
        """Retain completed and partial partitions and refresh the final reports."""

        with TemporaryDirectory() as temporary_directory:
            output, plan = create_partition_fixture(temporary_directory, 3)
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=[partition_events(), partition_events("interrupted", "github.com/example/project")]), patch("nasa_eo_search.partition_search.time.sleep"), redirect_stderr(StringIO()):
                self.assertEqual(execute_partitioned_searches(output, plan, None), 130)
            progress = json.loads((output / "progress.json").read_text())
            self.assertEqual(progress["searches_by_status"][COMPLETED_SEARCH], 1)
            self.assertEqual(progress["searches_by_status"]["interrupted"], 1)
            self.assertEqual(progress["searches_by_status"]["pending"], 1)
            attempt = list_search_attempts(output, plan["jobs"][1]["search_id"])[0]
            self.assertIn("github.com/example/project", (attempt / "matches.jsonl").read_text())

    def test_running_and_corrupt_attempts_count_toward_budget_without_rewriting(self) -> None:
        """Reconcile killed or broken attempts from files rather than stale caches."""

        for saved_status in ("running", "corrupt"):
            with self.subTest(saved_status=saved_status), TemporaryDirectory() as temporary_directory:
                output, plan = create_partition_fixture(temporary_directory)
                job = plan["jobs"][0]
                attempt = output / "searches" / job["search_id"] / "attempt-000001"
                attempt.mkdir(parents=True)
                original_summary = "{" if saved_status == "corrupt" else json.dumps({
                    "query": job["query"], "endpoint": DEFAULT_SOURCEGRAPH_ENDPOINT, "status": "running",
                })
                (attempt / "summary.json").write_text(original_summary, encoding="utf-8")
                (attempt / "matches.jsonl").write_text("", encoding="utf-8")
                (attempt / "events.jsonl").write_text("", encoding="utf-8")
                (output / "progress.json").write_text('{"searches_by_status":{"pending":999}}', encoding="utf-8")
                with redirect_stderr(StringIO()):
                    self.assertEqual(execute_partitioned_searches(output, plan, None, max_requests=0), 4)
                progress = json.loads((output / "progress.json").read_text())
                expected_status = "failed" if saved_status == "corrupt" else "interrupted"
                self.assertEqual(progress["searches_by_status"][expected_status], 1)
                self.assertEqual(progress["attempts_recorded"], 1)
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual(summary["searches_by_status"], progress["searches_by_status"])
                with (output / "searches.csv").open(encoding="utf-8-sig", newline="") as report_input:
                    self.assertEqual(next(csv.DictReader(report_input))["status"], expected_status)
                with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", return_value=partition_events()) as stream_mock, redirect_stderr(StringIO()):
                    self.assertEqual(execute_partitioned_searches(output, plan, None), 0)
                    stream_mock.assert_called_once()
                self.assertEqual((attempt / "summary.json").read_text(), original_summary)
                self.assertEqual(len(list_search_attempts(output, job["search_id"])), 2)

    def test_scope_violation_preserves_raw_matches_but_cannot_complete(self) -> None:
        """Persist a warning when the service returns a different partition's match."""

        with TemporaryDirectory() as temporary_directory:
            output, plan = create_partition_fixture(temporary_directory)
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=[partition_events(repository="github.com/zebra/project"), partition_events(repository="github.com/zebra/project")]), patch("nasa_eo_search.partition_search.time.sleep"), redirect_stderr(StringIO()):
                self.assertEqual(execute_partitioned_searches(output, plan, None), 4)
            attempts = list_search_attempts(output, plan["jobs"][0]["search_id"])
            summary = json.loads((attempts[-1] / "summary.json").read_text())
            self.assertEqual(summary["status"], "incomplete")
            self.assertEqual(summary["warnings"][0]["reason"], "repository-partition-mismatch")
            self.assertIn("github.com/zebra/project", (attempts[-1] / "matches.jsonl").read_text())


class RetryAfterMetadataTests(unittest.TestCase):
    """Verify HTTP retry metadata without making network requests."""

    def test_retry_after_parses_seconds_dates_and_rejects_invalid_values(self) -> None:
        """Support both HTTP header forms without accepting negative or nonfinite waits."""

        self.assertEqual(parse_retry_after_seconds("120"), 120)
        for invalid_value in (None, "invalid", "-1", "nan", "inf"):
            self.assertIsNone(parse_retry_after_seconds(invalid_value))
        future_date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=300), usegmt=True)
        self.assertGreater(parse_retry_after_seconds(future_date), 298)

    def test_http_error_exposes_status_and_retry_delay(self) -> None:
        """Preserve response metadata through the Sourcegraph transport exception."""

        error = HTTPError(DEFAULT_SOURCEGRAPH_ENDPOINT, 429, "rate limit", {"Retry-After": "120"}, BytesIO(b"slow down"))
        with patch("nasa_eo_search.sourcegraph.build_tls_context"), patch("nasa_eo_search.sourcegraph.urlopen", side_effect=error):
            with self.assertRaises(SourcegraphError) as caught_error:
                list(stream_sourcegraph_search_events(DEFAULT_SOURCEGRAPH_ENDPOINT, "query", None, 20))
        self.assertEqual(caught_error.exception.http_status, 429)
        self.assertEqual(caught_error.exception.retry_after_seconds, 120)


if __name__ == "__main__":
    unittest.main()

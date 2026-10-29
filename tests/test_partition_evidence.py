"""Keep partition scope checks consistent across resume, reports, and evidence."""

from contextlib import redirect_stderr
import csv
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.batch_files import COMPLETED_SEARCH, EMPTY_REPOSITORY_SCOPE_NOTICE, inspect_search_attempt
from nasa_eo_search.batch_plan import build_batch_search_plan
from nasa_eo_search.batch_reports import write_batch_reports
from nasa_eo_search.evidence_inputs import read_product_evidence_groups
from nasa_eo_search.partition_search import execute_partitioned_searches
from nasa_eo_search.repo_search import collect_repository_query
from nasa_eo_search.search_partitions import partition_catalog_plan
from nasa_eo_search.sourcegraph import DEFAULT_SOURCEGRAPH_ENDPOINT
from tests.test_repo_batch import create_batch_inputs, example_search_events


def save_completed_partition_fixture(root: str, match_partition: str) -> tuple[Path, Path, dict]:
    """Save completed streams with a hit in one caller-selected repository bucket.

    The fixture repository is github.com/example/project, which belongs only to
    github-d-f. Other buckets intentionally receive no matches. Saving an apparent
    completion in the wrong bucket models a crash before runner scope validation.

    Args:
        root: Existing temporary directory owned by the test.
        match_partition: Partition in whose raw attempt to place the example hit.

    Returns:
        Catalog path, batch path, and one-term plan containing all nine partitions.
    """
    catalog, selection, output = create_batch_inputs(root)
    legacy_plan = build_batch_search_plan(catalog, selection, False, 20)
    legacy_plan["jobs"] = legacy_plan["jobs"][:1]
    legacy_plan["report_unit"] = "file"
    plan = partition_catalog_plan(legacy_plan)
    output.mkdir()
    (output / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    for job in plan["jobs"]:
        attempt = output / "searches" / job["search_id"] / "attempt-000001"
        with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events",
                   return_value=example_search_events(include_match=job["partition_id"] == match_partition)), redirect_stderr(StringIO()):
            if collect_repository_query(job["query"], attempt, None) != 0:
                raise AssertionError("Fixture search did not finish successfully.")
    return catalog, output, plan


def read_partition_report(path: Path) -> list[dict[str, str]]:
    """Read a generated CSV while retaining its text-valued report fields.

    Args:
        path: Test-owned report file.

    Returns:
        Ordered report rows keyed by column name.
    """
    with path.open(encoding="utf-8-sig", newline="") as report_input:
        return list(csv.DictReader(report_input))


def snapshot_attempt_files(output: Path) -> dict[Path, bytes]:
    """Capture raw attempt contents for source-immutability assertions.

    Args:
        output: Test-owned batch root.

    Returns:
        Every saved attempt file mapped to its exact bytes.
    """
    return {path: path.read_bytes() for path in (output / "searches").rglob("*") if path.is_file()}


def save_empty_scope_attempt(attempt: Path, query: str) -> tuple[dict, list[dict]]:
    """Replace a test-owned attempt with the observed empty-repository response.

    Args:
        attempt: Existing attempt directory owned by this test.
        query: Exact validated partition query recorded with the fixture.

    Returns:
        Raw incomplete summary and event records for further negative-case edits.
    """
    final_progress = {"done": True, "matchCount": 0, "skipped": [], "durationMs": 2248}
    summary = {
        "status": "incomplete", "query": query, "endpoint": DEFAULT_SOURCEGRAPH_ENDPOINT,
        "match_records_saved": 0, "repositories_with_matches": 0,
        "received_done_event": True, "received_final_progress": True,
        "warnings": [dict(EMPTY_REPOSITORY_SCOPE_NOTICE)], "final_progress": final_progress,
    }
    events = [
        {"event": "progress", "data": dict(final_progress)},
        {"event": "alert", "data": dict(EMPTY_REPOSITORY_SCOPE_NOTICE)},
        {"event": "done", "data": {}},
    ]
    (attempt / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (attempt / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    (attempt / "matches.jsonl").write_text("", encoding="utf-8")
    return summary, events


class PartitionEvidenceTests(unittest.TestCase):
    """Preserve unexpected hits without overstating completed partition coverage."""

    def test_scope_check_retains_raw_matches_and_legacy_inspection_behavior(self) -> None:
        """Downgrade a wrong-bucket completion without editing it or changing legacy reads."""
        with TemporaryDirectory() as temporary_directory:
            _, output, plan = save_completed_partition_fixture(temporary_directory, "github-0-9-a-c")
            job = plan["jobs"][0]
            attempt = output / "searches" / job["search_id"] / "attempt-000001"
            original_files = snapshot_attempt_files(output)
            legacy_summary, legacy_matches = inspect_search_attempt(attempt, job["query"])
            scoped_summary, scoped_matches = inspect_search_attempt(
                attempt, job["query"], partition_id=job["partition_id"], all_hosts=False,
            )
            self.assertEqual(legacy_summary["status"], COMPLETED_SEARCH)
            self.assertEqual(scoped_summary["status"], "incomplete")
            self.assertEqual(scoped_matches, legacy_matches)
            self.assertEqual(len(scoped_matches), 1)
            self.assertEqual(scoped_summary["warnings"][0]["reason"], "repository-partition-mismatch")
            self.assertEqual(snapshot_attempt_files(output), original_files)

    def test_reports_and_evidence_reject_false_completion_without_dropping_hits(self) -> None:
        """Include every partition and flag wrong-bucket evidence in CSV and classifier inputs."""
        with TemporaryDirectory() as temporary_directory:
            catalog, output, plan = save_completed_partition_fixture(temporary_directory, "github-0-9-a-c")
            original_files = snapshot_attempt_files(output)
            summary = write_batch_reports(output, plan)
            self.assertEqual(summary["status"], "incomplete")
            self.assertEqual(summary["searches_by_status"][COMPLETED_SEARCH], 8)
            self.assertEqual(summary["searches_by_status"]["incomplete"], 1)
            searches = read_partition_report(output / "searches.csv")
            self.assertEqual(len(searches), 9)
            self.assertEqual(searches[0]["status"], "incomplete")
            self.assertIn("repository-partition-mismatch", searches[0]["warnings"])
            matches = read_partition_report(output / "matches.csv")
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["search_complete"], "false")
            evidence = read_product_evidence_groups(output, catalog)
            self.assertEqual(len(evidence["groups"]), 1)
            group = evidence["groups"][0]
            self.assertFalse(group["search_complete"])
            self.assertEqual(len(group["searches"]), 9)
            self.assertEqual(group["searches"][0]["status"], "incomplete")
            self.assertEqual(len(group["files"]), 1)
            self.assertEqual(group["files"][0]["sources"][0]["status"], "incomplete")
            self.assertEqual(snapshot_attempt_files(output), original_files)

    def test_no_request_resume_keeps_runner_reports_and_evidence_in_agreement(self) -> None:
        """Catch a preexisting scope mismatch even when the request budget prevents repair."""
        with TemporaryDirectory() as temporary_directory:
            catalog, output, plan = save_completed_partition_fixture(temporary_directory, "github-0-9-a-c")
            original_files = snapshot_attempt_files(output)
            with patch("nasa_eo_search.partition_search.collect_repository_query") as requests, redirect_stderr(StringIO()):
                self.assertEqual(execute_partitioned_searches(output, plan, None, max_requests=0), 4)
                requests.assert_not_called()
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            searches = read_partition_report(output / "searches.csv")
            self.assertEqual(summary["searches_by_status"]["incomplete"], 1)
            self.assertEqual(sum(row["status"] == "incomplete" for row in searches), 1)
            self.assertFalse(read_product_evidence_groups(output, catalog)["groups"][0]["search_complete"])
            self.assertEqual(snapshot_attempt_files(output), original_files)

    def test_in_scope_hit_and_completed_zero_hit_partitions_remain_complete(self) -> None:
        """Avoid degrading valid partition results while keeping zero-hit scope coverage."""
        with TemporaryDirectory() as temporary_directory:
            catalog, output, plan = save_completed_partition_fixture(temporary_directory, "github-d-f")
            summary = write_batch_reports(output, plan)
            self.assertEqual(summary["status"], "complete")
            self.assertEqual(summary["searches_by_status"][COMPLETED_SEARCH], 9)
            matches = read_partition_report(output / "matches.csv")
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["search_complete"], "true")
            evidence = read_product_evidence_groups(output, catalog)
            self.assertTrue(evidence["groups"][0]["search_complete"])


class EmptyPartitionCompletionTests(unittest.TestCase):
    """Distinguish verified empty repository scopes from incomplete searches."""

    def test_empty_scope_is_a_retained_notice_only_for_partition_inspection(self) -> None:
        """Keep raw incomplete metadata intact while deriving completed empty-partition coverage."""
        with TemporaryDirectory() as temporary_directory:
            _, output, plan = save_completed_partition_fixture(temporary_directory, "github-d-f")
            job = plan["jobs"][-1]
            attempt = output / "searches" / job["search_id"] / "attempt-000001"
            save_empty_scope_attempt(attempt, job["query"])
            original_files = snapshot_attempt_files(output)
            legacy_summary, _ = inspect_search_attempt(attempt, job["query"])
            derived_summary, matches = inspect_search_attempt(
                attempt, job["query"], partition_id=job["partition_id"], all_hosts=False,
            )
            self.assertEqual(legacy_summary["status"], "incomplete")
            self.assertEqual(legacy_summary["warnings"], [EMPTY_REPOSITORY_SCOPE_NOTICE])
            self.assertEqual(derived_summary["status"], COMPLETED_SEARCH)
            self.assertEqual(derived_summary["raw_status"], "incomplete")
            self.assertEqual(derived_summary["completion_basis"], "empty_indexed_repository_scope")
            self.assertEqual(derived_summary["warnings"], [])
            self.assertEqual(derived_summary["notices"], [EMPTY_REPOSITORY_SCOPE_NOTICE])
            self.assertEqual(matches, [])
            self.assertEqual(snapshot_attempt_files(output), original_files)

    def test_resume_reports_and_classifier_agree_on_verified_empty_scope(self) -> None:
        """Do not retry an empty scope or make its product coverage incomplete."""
        with TemporaryDirectory() as temporary_directory:
            catalog, output, plan = save_completed_partition_fixture(temporary_directory, "github-d-f")
            job = plan["jobs"][-1]
            attempt = output / "searches" / job["search_id"] / "attempt-000001"
            save_empty_scope_attempt(attempt, job["query"])
            original_files = snapshot_attempt_files(output)
            with patch("nasa_eo_search.partition_search.collect_repository_query") as requests, redirect_stderr(StringIO()):
                self.assertEqual(execute_partitioned_searches(output, plan, None, max_requests=0), 0)
                requests.assert_not_called()
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["searches_by_status"][COMPLETED_SEARCH], 9)
            searches = read_partition_report(output / "searches.csv")
            self.assertTrue(all(row["status"] == COMPLETED_SEARCH for row in searches))
            self.assertEqual(searches[-1]["scope_note"], "No indexed repositories matched this partition.")
            self.assertEqual(read_partition_report(output / "matches.csv")[0]["search_complete"], "true")
            self.assertTrue(read_product_evidence_groups(output, catalog)["groups"][0]["search_complete"])
            self.assertEqual(snapshot_attempt_files(output), original_files)

    def test_limits_missing_completion_errors_and_nonzero_matches_are_not_promoted(self) -> None:
        """Require complete consistent zero-hit evidence rather than suppressing an alert title."""
        problems = (
            "done_flag", "progress_flag", "failed", "interrupted", "transport_error",
            "record_count", "boolean_record_count", "final_count", "final_skipped",
            "extra_warning", "changed_alert", "alert_severity", "missing_done_event",
            "missing_progress_event", "extra_alert_event", "skipped_event", "nonzero_progress",
            "changed_final_event", "progress_error", "done_error", "unknown_event", "malformed_events", "missing_events",
            "missing_matches", "malformed_match", "actual_match",
        )
        with TemporaryDirectory() as temporary_directory:
            _, output, plan = save_completed_partition_fixture(temporary_directory, "github-d-f")
            job = plan["jobs"][-1]
            attempt = output / "searches" / job["search_id"] / "attempt-000001"
            for problem in problems:
                with self.subTest(problem=problem):
                    summary, events = save_empty_scope_attempt(attempt, job["query"])
                    if problem == "done_flag":
                        summary["received_done_event"] = False
                    elif problem == "progress_flag":
                        summary["received_final_progress"] = False
                    elif problem in ("failed", "interrupted"):
                        summary["status"] = problem
                    elif problem == "transport_error":
                        summary["error"] = "connection closed"
                    elif problem == "record_count":
                        summary["match_records_saved"] = 1
                    elif problem == "boolean_record_count":
                        summary["match_records_saved"] = False
                    elif problem == "final_count":
                        summary["final_progress"]["matchCount"] = 1
                    elif problem == "final_skipped":
                        summary["final_progress"]["skipped"] = [{"reason": "shard-timeout"}]
                    elif problem == "extra_warning":
                        summary["warnings"].append({"reason": "shard-timeout"})
                    elif problem == "changed_alert":
                        summary["warnings"][0]["description"] = "Search failed"
                    elif problem == "alert_severity":
                        summary["warnings"][0]["severity"] = "error"
                    elif problem == "missing_done_event":
                        events.pop()
                    elif problem == "missing_progress_event":
                        events.pop(0)
                    elif problem == "extra_alert_event":
                        events.insert(0, {"event": "alert", "data": {"title": "Search failed"}})
                    elif problem == "skipped_event":
                        events[0]["data"]["skipped"] = [{"reason": "shard-timeout"}]
                    elif problem == "nonzero_progress":
                        events.insert(0, {"event": "progress", "data": {"done": False, "matchCount": 1, "skipped": []}})
                    elif problem == "changed_final_event":
                        events[0]["data"]["durationMs"] = 999
                    elif problem == "progress_error":
                        events[0]["data"]["error"] = "failed"
                    elif problem == "done_error":
                        events[-1]["data"] = {"error": "failed"}
                    elif problem == "unknown_event":
                        events.insert(0, {"event": "error", "data": "failed"})
                    (attempt / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
                    (attempt / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
                    if problem == "malformed_events":
                        (attempt / "events.jsonl").write_text("not json", encoding="utf-8")
                    elif problem == "missing_events":
                        (attempt / "events.jsonl").unlink()
                    elif problem == "missing_matches":
                        (attempt / "matches.jsonl").unlink()
                    elif problem == "malformed_match":
                        (attempt / "matches.jsonl").write_text("not json", encoding="utf-8")
                    elif problem == "actual_match":
                        raw_match = {"repository": "github.com/-owner/project", "path": "read.py", "lineMatches": []}
                        (attempt / "matches.jsonl").write_text(json.dumps({"query": job["query"], "match": raw_match}) + "\n", encoding="utf-8")
                    original_files = snapshot_attempt_files(output)
                    inspected, _ = inspect_search_attempt(
                        attempt, job["query"], partition_id=job["partition_id"], all_hosts=False,
                    )
                    self.assertNotEqual(inspected["status"], COMPLETED_SEARCH)
                    self.assertEqual(snapshot_attempt_files(output), original_files)


if __name__ == "__main__":
    unittest.main()

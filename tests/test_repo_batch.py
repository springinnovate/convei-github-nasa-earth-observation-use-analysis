"""Test reviewed selection, batch reporting, and restart-safe repository searches."""

from contextlib import redirect_stderr
from copy import deepcopy
import csv
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.batch_files import COMPLETED_SEARCH, atomic_batch_output, lock_batch_directory
from nasa_eo_search.batch_plan import build_batch_search_plan, validate_saved_batch_plan
from nasa_eo_search.repo_batch import main
from nasa_eo_search.sourcegraph import ServerSentEvent, SourcegraphError


def create_batch_inputs(parent_directory: str) -> tuple[Path, Path, Path]:
    """Write a two-product catalog and reviewed CSV selection for batch tests.

    Args:
        parent_directory: Existing temporary directory owned by the test.

    Returns:
        Catalog directory, selection file, and unused batch output path.
    """

    root = Path(parent_directory)
    catalog = root / "catalog"
    catalog.mkdir()
    products = []
    for index, term in enumerate(("ATL03", "ATL06"), start=1):
        concept_id = f"C{index}-NSIDC"
        products.append({
            "provider": "NSIDC", "short_name": term,
            "collections": [{"concept_id": concept_id, "title": term}],
            "candidate_signatures": [{"term": term, "review_status": "unreviewed",
                                       "sources": [{"concept_id": concept_id, "source_field": "short_name"}]}],
        })
    (catalog / "products.jsonl").write_text("".join(json.dumps(product) + "\n" for product in products), encoding="utf-8")
    (catalog / "summary.json").write_text(json.dumps({"schema_version": 1, "status": "complete", "products_saved": 2}), encoding="utf-8")
    selection = root / "selection.csv"
    selection.write_text("provider,short_name\nNSIDC,ATL03\nNSIDC,ATL06\n", encoding="utf-8")
    return catalog, selection, root / "batch"


def example_search_events(status: str = "complete", include_match: bool = True):
    """Emit deterministic Sourcegraph events for one attempt outcome.

    Args:
        status: complete, incomplete, failed, or interrupted.
        include_match: Whether to yield one file match before the outcome.

    Yields:
        Match and progress events using the real stream decoder's input format.

    Raises:
        SourcegraphError: When simulating a transport failure.
        KeyboardInterrupt: When simulating a user interruption.
    """

    if include_match:
        yield ServerSentEvent("matches", json.dumps([{
            "type": "content", "repository": "github.com/example/project", "path": "data/read file.py",
            "commit": "abc123", "repoStars": 42, "language": "Python",
            "lineMatches": [{"lineNumber": 4, "line": 'load("ATL03", "caf\u00e9")'}],
        }]))
    if status == "failed":
        raise SourcegraphError("offline")
    if status == "interrupted":
        raise KeyboardInterrupt()
    warnings = [{"reason": "timeout"}] if status == "incomplete" else []
    yield ServerSentEvent("progress", json.dumps({"done": True, "skipped": warnings}))
    yield ServerSentEvent("done", "{}")


class BatchSearchTests(unittest.TestCase):
    """Exercise the real single-query collector through the batch command."""

    def test_interrupt_between_queries_preserves_completed_search(self) -> None:
        """Stop during the pause and resume only the still-pending query."""

        with TemporaryDirectory() as temporary_directory:
            catalog, selection, output = create_batch_inputs(temporary_directory)
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", return_value=example_search_events()) as search_mock, patch("nasa_eo_search.repo_batch.time.sleep", side_effect=KeyboardInterrupt()), redirect_stderr(StringIO()):
                self.assertEqual(main(["start", str(catalog), "--selection", str(selection), "--output", str(output)]), 130)
                search_mock.assert_called_once()
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["searches_by_status"][COMPLETED_SEARCH], 1)
            self.assertEqual(summary["searches_by_status"]["pending"], 1)
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", return_value=example_search_events()) as search_mock, redirect_stderr(StringIO()):
                self.assertEqual(main(["resume", str(output)]), 0)
                search_mock.assert_called_once()
                self.assertIn("ATL06", search_mock.call_args.args[1])

    def test_resume_rejects_corrupt_plan_without_network(self) -> None:
        """Fail clearly on malformed saved plans without replacing existing reports."""

        with TemporaryDirectory() as temporary_directory:
            catalog, selection, output = create_batch_inputs(temporary_directory)
            output.mkdir()
            (output / "plan.json").write_text("{}")
            report = output / "summary.json"
            report.write_text("previous report")
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events") as search_mock, redirect_stderr(StringIO()):
                self.assertEqual(main(["resume", str(output)]), 1)
                search_mock.assert_not_called()
            self.assertEqual(report.read_text(), "previous report")

    def test_invalid_attempt_directory_is_not_used(self) -> None:
        """Refuse unexpected files in an attempt area and preserve them unchanged."""

        with TemporaryDirectory() as temporary_directory:
            catalog, selection, output = create_batch_inputs(temporary_directory)
            plan = build_batch_search_plan(catalog, selection, False, 20)
            search_directory = output / "searches" / plan["jobs"][0]["search_id"]
            search_directory.mkdir(parents=True)
            (output / "plan.json").write_text(json.dumps(plan))
            user_file = search_directory / "notes.txt"
            user_file.write_text("notes")
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events") as search_mock, redirect_stderr(StringIO()):
                self.assertEqual(main(["resume", str(output)]), 1)
                search_mock.assert_not_called()
            self.assertEqual(user_file.read_text(), "notes")

    def test_atomic_report_failure_preserves_existing_file(self) -> None:
        """Keep the old report when replacement contents cannot be completed."""

        with TemporaryDirectory() as temporary_directory:
            report = Path(temporary_directory) / "report.csv"
            report.write_text("original")
            with self.assertRaises(OSError):
                with atomic_batch_output(report) as report_output:
                    report_output.write("partial replacement")
                    raise OSError("disk full")
            self.assertEqual(report.read_text(), "original")
            self.assertEqual(list(Path(temporary_directory).iterdir()), [report])

    def test_scope_and_certificate_options_reach_each_search(self) -> None:
        """Keep all-host scope in resumed plans and apply the requested CA bundle."""

        with TemporaryDirectory() as temporary_directory:
            catalog, selection, output = create_batch_inputs(temporary_directory)
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=[example_search_events(), example_search_events()]) as search_mock, patch("nasa_eo_search.repo_batch.time.sleep"), redirect_stderr(StringIO()):
                self.assertEqual(main(["start", str(catalog), "--selection", str(selection), "--output", str(output), "--all-hosts", "--ca-bundle", "private.pem"]), 0)
            for search_call in search_mock.call_args_list:
                self.assertNotIn("repo:^github", search_call.args[1])
                self.assertEqual(search_call.kwargs["ca_bundle_path"], "private.pem")

    def test_crash_before_final_summary_is_retried(self) -> None:
        """Treat an abandoned running attempt as unfinished without deleting files."""

        with TemporaryDirectory() as temporary_directory:
            catalog, selection, output = create_batch_inputs(temporary_directory)
            selection.write_text("provider,short_name\nNSIDC,ATL03\n")
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", return_value=example_search_events()), redirect_stderr(StringIO()):
                self.assertEqual(main(["start", str(catalog), "--selection", str(selection), "--output", str(output)]), 0)
            attempt = next((output / "searches").glob("*/attempt-000001"))
            summary_path = attempt / "summary.json"
            summary = json.loads(summary_path.read_text())
            summary["status"] = "running"
            summary_path.write_text(json.dumps(summary))
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", return_value=example_search_events()) as search_mock, redirect_stderr(StringIO()):
                self.assertEqual(main(["resume", str(output)]), 0)
                search_mock.assert_called_once()
            self.assertEqual(json.loads(summary_path.read_text())["status"], "running")

    def test_plan_validation_rejects_changed_queries_paths_and_metadata(self) -> None:
        """Reject corrupted plans before using saved identifiers as output paths."""

        with TemporaryDirectory() as temporary_directory:
            catalog, selection, _ = create_batch_inputs(temporary_directory)
            plan = build_batch_search_plan(catalog, selection, False, 20)
            invalid_plans = [None, {**plan, "schema_version": True}, {**plan, "jobs": []},
                             {**plan, "endpoint": "https://other.test"}, {**plan, "catalog_summary": None},
                             {**plan, "scope": None}, {**plan, "all_hosts": "yes"}]
            for field, value in (("search_id", "../../outside"), ("query", "changed"),
                                 ("term", "ATL03\n"), ("products", [])):
                changed_plan = deepcopy(plan)
                changed_plan["jobs"][0][field] = value
                invalid_plans.append(changed_plan)
            changed_plan = deepcopy(plan)
            changed_plan["jobs"].append(changed_plan["jobs"][0])
            invalid_plans.append(changed_plan)
            for invalid_plan in invalid_plans:
                with self.subTest(plan=invalid_plan), self.assertRaises(ValueError):
                    validate_saved_batch_plan(invalid_plan)

    def test_existing_directories_are_preserved_and_missing_plans_fail(self) -> None:
        """Refuse to overwrite a batch and reject resume outside saved batches."""

        with TemporaryDirectory() as temporary_directory:
            catalog, selection, output = create_batch_inputs(temporary_directory)
            output.mkdir()
            marker = output / "notes.txt"
            marker.write_text("user notes")
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events") as search_mock, redirect_stderr(StringIO()):
                self.assertEqual(main(["start", str(catalog), "--selection", str(selection), "--output", str(output)]), 1)
                self.assertEqual(main(["resume", str(output)]), 1)
                search_mock.assert_not_called()
            self.assertEqual(marker.read_text(), "user notes")
            self.assertEqual(list(output.iterdir()), [marker])

    def test_exclusive_lock_is_released_after_exception(self) -> None:
        """Reject concurrent writers and permit the next run after lock release."""

        with TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            with self.assertRaisesRegex(RuntimeError, "simulate exit"):
                with lock_batch_directory(output):
                    with self.assertRaisesRegex(ValueError, "Another process"):
                        with lock_batch_directory(output):
                            self.fail("A second writer acquired the lock")
                    raise RuntimeError("simulate exit")
            with lock_batch_directory(output):
                self.assertTrue((output / "batch.lock").is_file())


    def test_failed_and_incomplete_jobs_retry_while_completed_jobs_stay_saved(self) -> None:
        """Retry each unsuccessful query once per resume and preserve other attempts."""

        for first_status, exit_code in (("failed", 1), ("incomplete", 4)):
            with self.subTest(first_status=first_status), TemporaryDirectory() as temporary_directory:
                catalog, selection, output = create_batch_inputs(temporary_directory)
                with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=[example_search_events(first_status), example_search_events()]), patch("nasa_eo_search.repo_batch.time.sleep"), redirect_stderr(StringIO()):
                    self.assertEqual(main(["start", str(catalog), "--selection", str(selection), "--output", str(output)]), exit_code)
                with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", return_value=example_search_events()) as search_mock, redirect_stderr(StringIO()):
                    self.assertEqual(main(["resume", str(output)]), 0)
                    search_mock.assert_called_once()
                self.assertEqual(len(list((output / "searches").glob("*/attempt-*"))), 3)

    def test_corrupt_completed_evidence_is_retried(self) -> None:
        """Detect damaged summary, match, and event files instead of skipping them."""

        for evidence_name in ("summary.json", "matches.jsonl", "events.jsonl"):
            with self.subTest(evidence_name=evidence_name), TemporaryDirectory() as temporary_directory:
                catalog, selection, output = create_batch_inputs(temporary_directory)
                selection.write_text("provider,short_name\nNSIDC,ATL03\n")
                with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", return_value=example_search_events()), redirect_stderr(StringIO()):
                    self.assertEqual(main(["start", str(catalog), "--selection", str(selection), "--output", str(output)]), 0)
                attempt = next((output / "searches").glob("*/attempt-000001"))
                (attempt / evidence_name).write_text("broken")
                with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", return_value=example_search_events()) as search_mock, redirect_stderr(StringIO()):
                    self.assertEqual(main(["resume", str(output)]), 0)
                    search_mock.assert_called_once()
                self.assertEqual((attempt / evidence_name).read_text(), "broken")

    def test_shared_terms_are_searched_once_with_all_product_associations(self) -> None:
        """Combine identical terms and retain each product's candidate provenance."""

        with TemporaryDirectory() as temporary_directory:
            catalog, selection, output = create_batch_inputs(temporary_directory)
            products_path = catalog / "products.jsonl"
            products = [json.loads(line) for line in products_path.read_text().splitlines()]
            products[1]["candidate_signatures"][0]["term"] = "ATL03"
            products_path.write_text("".join(json.dumps(product) + "\n" for product in products))
            selection.write_text("provider,short_name,search_term\nNSIDC,ATL03,ATL03\nNSIDC,ATL06,ATL03\nNSIDC,ATL03,ATL03\n")
            plan = build_batch_search_plan(catalog, selection, False, 20)
            self.assertEqual(len(plan["jobs"]), 1)
            self.assertEqual(len(plan["jobs"][0]["products"]), 2)
            self.assertEqual(plan["jobs"][0]["products"][1]["candidate"]["sources"][0]["concept_id"], "C2-NSIDC")
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", return_value=example_search_events()) as search_mock, redirect_stderr(StringIO()):
                self.assertEqual(main(["start", str(catalog), "--selection", str(selection), "--output", str(output)]), 0)
                search_mock.assert_called_once()
            with (output / "matches.csv").open(encoding="utf-8-sig", newline="") as report_input:
                self.assertEqual({row["short_name"] for row in csv.DictReader(report_input)}, {"ATL03", "ATL06"})

    def test_invalid_selection_makes_no_output_or_requests(self) -> None:
        """Reject unknown products/terms, malformed CSV rows, and empty selections."""

        for selection_text in (
            "wrong,columns\na,b\n", "provider,short_name\n", "provider,short_name\nNSIDC,UNKNOWN\n",
            "provider,short_name,search_term\nNSIDC,ATL03,MATL03\n",
            "provider,short_name\n,ATL03\n", "provider,short_name\nNSIDC,ATL03,extra\n",
            "provider,provider,short_name\nNSIDC,NSIDC,ATL03\n",
        ):
            with self.subTest(selection_text=selection_text), TemporaryDirectory() as temporary_directory:
                catalog, selection, output = create_batch_inputs(temporary_directory)
                selection.write_text(selection_text)
                with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events") as search_mock, redirect_stderr(StringIO()):
                    self.assertEqual(main(["start", str(catalog), "--selection", str(selection), "--output", str(output)]), 1)
                    search_mock.assert_not_called()
                self.assertFalse(output.exists())

    def test_query_limit_and_catalog_counts_are_checked(self) -> None:
        """Enforce the pilot limit and validate the saved catalog before planning."""

        with TemporaryDirectory() as temporary_directory:
            catalog, selection, _ = create_batch_inputs(temporary_directory)
            with self.assertRaisesRegex(ValueError, "query limit"):
                build_batch_search_plan(catalog, selection, False, 1)
            self.assertEqual(len(build_batch_search_plan(catalog, selection, True, 2)["jobs"]), 2)
            summary_path = catalog / "summary.json"
            summary = json.loads(summary_path.read_text())
            summary["products_saved"] = 3
            summary_path.write_text(json.dumps(summary))
            with self.assertRaisesRegex(ValueError, "count"):
                build_batch_search_plan(catalog, selection, False, 20)


    def test_complete_batch_reports_products_and_skips_on_resume(self) -> None:
        """Save associations, clickable files, and zero-match completion without repeats."""

        with TemporaryDirectory() as temporary_directory:
            catalog, selection, output = create_batch_inputs(temporary_directory)
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=[
                example_search_events(), example_search_events(include_match=False),
            ]) as search_mock, patch("nasa_eo_search.repo_batch.time.sleep"), redirect_stderr(StringIO()):
                self.assertEqual(main(["start", str(catalog), "--selection", str(selection), "--output", str(output)]), 0)
                self.assertEqual(search_mock.call_count, 2)
            with (output / "matches.csv").open(encoding="utf-8-sig", newline="") as report_input:
                matches = list(csv.DictReader(report_input))
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["short_name"], "ATL03")
            self.assertEqual(matches[0]["owner"], "example")
            self.assertEqual(matches[0]["file_type"], ".py")
            self.assertEqual(matches[0]["stars"], "42")
            self.assertIn("read%20file.py", matches[0]["file_url"])
            self.assertTrue(matches[0]["matching_text"].startswith("5: "))
            self.assertEqual(matches[0]["latest_attempt"], "true")
            catalog.rename(catalog.with_name("moved-catalog"))
            selection.unlink()
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events") as search_mock, redirect_stderr(StringIO()):
                self.assertEqual(main(["resume", str(output)]), 0)
                search_mock.assert_not_called()
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["searches_by_status"][COMPLETED_SEARCH], 2)

    def test_interrupt_keeps_pending_jobs_and_resumes_new_attempts(self) -> None:
        """Stop on Ctrl+C, retain partial evidence, and retry unfinished queries."""

        with TemporaryDirectory() as temporary_directory:
            catalog, selection, output = create_batch_inputs(temporary_directory)
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", return_value=example_search_events("interrupted")) as search_mock, redirect_stderr(StringIO()):
                self.assertEqual(main(["start", str(catalog), "--selection", str(selection), "--output", str(output)]), 130)
                search_mock.assert_called_once()
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["searches_by_status"]["pending"], 1)
            plan_bytes = (output / "plan.json").read_bytes()
            first_attempt = next((output / "searches").glob("*/attempt-000001"))
            old_matches = (first_attempt / "matches.jsonl").read_bytes()
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=[example_search_events(), example_search_events()]) as search_mock, patch("nasa_eo_search.repo_batch.time.sleep"), redirect_stderr(StringIO()):
                self.assertEqual(main(["resume", str(output)]), 0)
                self.assertEqual(search_mock.call_count, 2)
            self.assertEqual((output / "plan.json").read_bytes(), plan_bytes)
            self.assertEqual((first_attempt / "matches.jsonl").read_bytes(), old_matches)
            self.assertTrue((first_attempt.parent / "attempt-000002").is_dir())
            with (output / "matches.csv").open(encoding="utf-8-sig", newline="") as report_input:
                rows = list(csv.DictReader(report_input))
            self.assertEqual(len(rows), 3)
            self.assertEqual(sum(row["latest_attempt"] == "false" for row in rows), 1)

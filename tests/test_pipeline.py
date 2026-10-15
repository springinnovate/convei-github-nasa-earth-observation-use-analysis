"""Exercise the three-command workflow using synthetic catalog, code, and GPT data."""

from contextlib import redirect_stderr
import csv
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.catalog import CollectionPage
from nasa_eo_search.evidence_reports import save_evidence_json
from nasa_eo_search.pipeline import main
from nasa_eo_search.pipeline_catalog import build_catalog_search_plan
from nasa_eo_search.pipeline_tables import FILE_MATCH_COLUMNS, collapse_batch_file_rows
from nasa_eo_search.repo_batch import main as run_legacy_batch_command
from nasa_eo_search.search_partitions import (
    REPOSITORY_PARTITIONS, repository_matches_partition,
)
from nasa_eo_search.sourcegraph import ServerSentEvent
from test_catalog import EXAMPLE_COLLECTION
from test_evidence_classify import make_model_response
from test_pipeline_catalog import make_pipeline_product, save_pipeline_catalog
from test_repo_batch import example_search_events


def read_pipeline_csv(path: Path) -> list[dict[str, str]]:
    """Read a generated spreadsheet without changing its source files.

    Args:
        path: CSV report created in a test-owned directory.

    Returns:
        Ordered rows with their original column names and text values.
    """
    with path.open(encoding="utf-8-sig", newline="") as csv_input:
        return list(csv.DictReader(csv_input))


def stream_pipeline_matches(*arguments, **options):
    """Return repeatable matches for two files, including two revisions of one file.

    Args:
        *arguments: Search endpoint and exact query used to select the fixture scope.
        **options: Search transport settings, unused by the fake service.

    Yields:
        Match and completion events; only the fixture's repository group has hits.
    """
    query = arguments[1]
    partition = next((partition for partition in REPOSITORY_PARTITIONS
                      if partition["filter"] in query), None)
    matches = [
        {"type": "content", "repository": "github.com/example/project", "path": path,
         "commit": revision, "repoStars": 42, "language": "Python",
         "lineMatches": [{"lineNumber": 4, "line": text}]}
        for path, revision, text in (
            ("load.py", "abc123", 'load("ATL03_006")'),
            ("load.py", "def456", 'load("ATL03_007")'),
            ("sites.py", "abc123", 'site_name = "atl03"'),
        )
    ]
    if partition and not repository_matches_partition(
        "github.com/example/project", False, partition["id"],
    ):
        matches = []
    yield ServerSentEvent("matches", json.dumps(matches))
    yield ServerSentEvent("progress", json.dumps({"done": True, "skipped": []}))
    yield ServerSentEvent("done", "{}")


def stream_single_pipeline_match(*arguments, **options):
    """Return a single fixture hit only inside its planned repository partition.

    Args:
        *arguments: Endpoint and query supplied to the search transport.
        **options: Transport settings unused by this offline responder.

    Returns:
        A fresh generator with one in-scope file match or a completed zero-hit search.
    """
    query = arguments[1]
    partition = next((partition for partition in REPOSITORY_PARTITIONS
                      if partition["filter"] in query), None)
    include_match = partition is None or repository_matches_partition(
        "github.com/example/project", False, partition["id"],
    )
    return example_search_events(include_match=include_match)


def classify_pipeline_file(request_body: dict, api_key: str, ca_bundle_path: str | None) -> dict:
    """Judge each mock file separately so a relevant file cannot hide a false hit.

    Args:
        request_body: Actual structured request containing one file's evidence.
        api_key: Synthetic credential, unused by the responder.
        ca_bundle_path: Mock transport setting, unused by the responder.

    Returns:
        A schema-valid model response with an intentionally file-specific judgment.
    """
    packet = json.loads(request_body["input"])
    response = make_model_response(request_body)
    judgment = json.loads(response["output"][0]["content"][0]["text"])
    judgment["category"] = "unrelated" if packet["assessment_file"] == "sites.py" else "direct_use"
    response["output"][0]["content"][0]["text"] = json.dumps(judgment)
    return response


class SimplePipelineTests(unittest.TestCase):
    """Check usable command boundaries, table continuity, and safe resume behavior."""

    def test_three_commands_produce_matching_file_rows_and_resume_without_calls(self) -> None:
        """Run the complete workflow with no real NASA, search, or model requests."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            catalog, search, classified = root / "catalog", root / "search", root / "classified"
            page = CollectionPage([EXAMPLE_COLLECTION], 1, None, "now")
            with patch("nasa_eo_search.catalog.fetch_cmr_collection_page", return_value=page), redirect_stderr(StringIO()):
                self.assertEqual(main(["catalog", "--output", str(catalog)]), 0)
            datasets = read_pipeline_csv(catalog / "datasets.csv")
            self.assertEqual(len(datasets), 1)
            self.assertEqual(datasets[0]["short_name"], "ATL03")
            search_arguments = ["search", str(catalog), "--output", str(search)]
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=stream_pipeline_matches) as requests, patch("nasa_eo_search.repo_batch.time.sleep"), redirect_stderr(StringIO()):
                self.assertEqual(main(search_arguments), 0)
                self.assertEqual(requests.call_count, 27)
                self.assertEqual(main(search_arguments), 0)
                self.assertEqual(requests.call_count, 27)
            matches = read_pipeline_csv(search / "matches.csv")
            self.assertEqual(len(matches), 3)
            self.assertEqual(len({row["match_id"] for row in matches}), 3)
            self.assertTrue(all(len(row["search_terms"].split("; ")) == 3 for row in matches))
            classify_arguments = ["classify", str(search), "--output", str(classified),
                                  "--model", "test-model", "--offline"]
            with patch("nasa_eo_search.evidence_classify.load_openai_api_key", return_value="fake-key"), patch("nasa_eo_search.evidence_classify.build_tls_context"), patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=classify_pipeline_file) as model_calls, redirect_stderr(StringIO()):
                self.assertEqual(main([*classify_arguments, "--max-requests", "1"]), 4)
                self.assertEqual(model_calls.call_count, 1)
                with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events") as search_calls:
                    self.assertEqual(main(search_arguments), 0)
                    search_calls.assert_not_called()
                original_sources = {path: path.read_bytes() for directory in (catalog, search)
                                    for path in directory.rglob("*") if path.is_file()}
                self.assertEqual(main(classify_arguments), 0)
                self.assertEqual(model_calls.call_count, 3)
                self.assertEqual(main(classify_arguments), 0)
                self.assertEqual(model_calls.call_count, 3)
            judgments = read_pipeline_csv(classified / "classified-matches.csv")
            self.assertEqual([{column: row[column] for column in FILE_MATCH_COLUMNS} for row in judgments], matches)
            self.assertEqual([row["category"] for row in judgments], ["direct_use", "direct_use", "unrelated"])
            self.assertTrue(all(row["status"] == "complete" and row["gpt_model"] for row in judgments))
            self.assertEqual({path: path.read_bytes() for directory in (catalog, search)
                              for path in directory.rglob("*") if path.is_file()}, original_sources)

    def test_export_existing_catalog_and_preview_do_not_call_services(self) -> None:
        """Reuse a saved catalog and inspect query counts without creating a run."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            original_files = {path: path.read_bytes() for path in catalog.iterdir()}
            output, preview = root / "datasets.csv", root / "preview"
            with patch("nasa_eo_search.catalog.fetch_cmr_collection_page") as nasa, patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events") as search, redirect_stderr(StringIO()) as terminal:
                self.assertEqual(main(["catalog", "--from", str(catalog), "--output", str(output)]), 0)
                self.assertEqual(main(["search", str(catalog), "--output", str(preview), "--dry-run"]), 0)
                self.assertIn("3 distinct terms; 27 search queries", terminal.getvalue())
                nasa.assert_not_called()
                search.assert_not_called()
            self.assertFalse(preview.exists())
            self.assertEqual({path: path.read_bytes() for path in catalog.iterdir()}, original_files)
            self.assertEqual(len(read_pipeline_csv(output)), 1)

    def test_interrupted_search_resumes_only_pending_queries_and_merges_repeated_hits(self) -> None:
        """Retain the first file hit and complete the other terms on the next run."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            output = root / "search"
            arguments = ["search", str(catalog), "--output", str(output)]
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=stream_single_pipeline_match) as requests, patch("nasa_eo_search.partition_search.time.sleep", side_effect=[None, KeyboardInterrupt()]), redirect_stderr(StringIO()):
                self.assertEqual(main(arguments), 130)
                self.assertEqual(requests.call_count, 2)
            first_rows = read_pipeline_csv(output / "matches.csv")
            self.assertEqual(len(first_rows), 1)
            self.assertEqual(first_rows[0]["search_complete"], "false")
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=stream_single_pipeline_match) as requests, patch("nasa_eo_search.partition_search.time.sleep"), redirect_stderr(StringIO()):
                self.assertEqual(main(arguments), 0)
                self.assertEqual(requests.call_count, 25)
            final_rows = read_pipeline_csv(output / "matches.csv")
            self.assertEqual(len(final_rows), 1)
            self.assertEqual(final_rows[0]["search_complete"], "true")
            self.assertEqual(final_rows[0]["match_id"], first_rows[0]["match_id"])
            self.assertEqual(len(final_rows[0]["snippet"].splitlines()), 1)

    def test_search_rejects_changed_selection_without_replacing_saved_output(self) -> None:
        """Require a new search directory when products or host scope change."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            catalog = save_pipeline_catalog(root, [make_pipeline_product(), make_pipeline_product("ATL06")])
            output = root / "search"
            arguments = ["search", str(catalog), "--output", str(output), "--short-name", "ATL03"]
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=stream_pipeline_matches), patch("nasa_eo_search.repo_batch.time.sleep"), redirect_stderr(StringIO()):
                self.assertEqual(main(arguments), 0)
            saved_files = {path: path.read_bytes() for path in output.rglob("*") if path.is_file()}
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events") as requests, redirect_stderr(StringIO()):
                self.assertEqual(main(arguments[:-2]), 1)
                self.assertEqual(main([*arguments, "--all-hosts"]), 1)
                requests.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in output.rglob("*") if path.is_file()}, saved_files)

    def test_request_cap_checkpoints_and_resumes_saved_partition_queries(self) -> None:
        """Pause after one call and finish only remaining partitions on resume."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            output = root / "search"
            arguments = ["search", str(catalog), "--output", str(output)]
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=stream_single_pipeline_match) as requests, patch("nasa_eo_search.partition_search.time.sleep"), redirect_stderr(StringIO()):
                self.assertEqual(main([*arguments, "--max-requests", "1"]), 4)
                self.assertEqual(requests.call_count, 1)
                checkpoint = json.loads((output / "progress.json").read_text(encoding="utf-8"))
                self.assertEqual(checkpoint["stop_reason"], "request_limit_reached")
                self.assertEqual(checkpoint["searches_by_status"]["finished_no_reported_limits"], 1)
                self.assertEqual(checkpoint["searches_by_status"]["pending"], 26)
                self.assertTrue(checkpoint["combined_reports_current"])
                self.assertEqual(main([*arguments, "--max-requests", "26"]), 0)
                self.assertEqual(requests.call_count, 27)
            search_rows = read_pipeline_csv(output / "searches.csv")
            self.assertEqual(len(search_rows), 27)
            self.assertEqual({row["repository_partition"] for row in search_rows},
                             {partition["id"] for partition in REPOSITORY_PARTITIONS})
            self.assertTrue(all(row["search_complete"] == "true"
                                for row in read_pipeline_csv(output / "matches.csv")))

    def test_legacy_resume_dispatches_saved_partition_queries_without_widening_scope(self) -> None:
        """Keep exact partition filters when the older resume command opens a new run."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            output = root / "search"
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=stream_single_pipeline_match) as requests, patch("nasa_eo_search.partition_search.time.sleep"), redirect_stderr(StringIO()):
                self.assertEqual(main(["search", str(catalog), "--output", str(output), "--max-requests", "1"]), 4)
                self.assertEqual(run_legacy_batch_command(["resume", str(output)]), 0)
            saved_plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual([request.args[1] for request in requests.call_args_list],
                             [job["query"] for job in saved_plan["jobs"]])

    def test_old_global_output_retains_strategy_and_rejects_partition_controls(self) -> None:
        """Resume original outputs unchanged and require new storage for new controls."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            output = root / "search"
            output.mkdir()
            legacy_plan = build_catalog_search_plan(catalog, None, False)
            save_evidence_json(output / "plan.json", legacy_plan)
            arguments = ["search", str(catalog), "--output", str(output)]
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=stream_pipeline_matches) as requests, patch("nasa_eo_search.repo_batch.time.sleep"), redirect_stderr(StringIO()) as terminal:
                self.assertEqual(main(arguments), 0)
                self.assertEqual(requests.call_count, 3)
                self.assertIn("original global-query strategy", terminal.getvalue())
            saved_files = {path: path.read_bytes() for path in output.rglob("*") if path.is_file()}
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events") as requests, redirect_stderr(StringIO()):
                for controls in (["--max-requests", "1"], ["--search-timeout", "20"], ["--retry-incomplete"]):
                    self.assertEqual(main([*arguments, *controls]), 1)
                requests.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in output.rglob("*") if path.is_file()}, saved_files)

    def test_changed_timeout_is_rejected_without_touching_saved_attempts(self) -> None:
        """Freeze the server timeout with the query instead of changing it on resume."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            output = root / "search"
            arguments = ["search", str(catalog), "--output", str(output)]
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=stream_single_pipeline_match), redirect_stderr(StringIO()):
                self.assertEqual(main([*arguments, "--max-requests", "1"]), 4)
            saved_files = {path: path.read_bytes() for path in output.rglob("*") if path.is_file()}
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events") as requests, redirect_stderr(StringIO()):
                self.assertEqual(main([*arguments, "--search-timeout", "20"]), 1)
                requests.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in output.rglob("*") if path.is_file()}, saved_files)


class FileTableTests(unittest.TestCase):
    """Keep retry provenance and zero-hit query limitations visible in file rows."""

    def test_retry_union_keeps_conflicting_lines_and_product_associations(self) -> None:
        """Merge exact repeats but retain conflicting snippets and distinct datasets."""
        completed = "finished_no_reported_limits"
        base_row = {
            "repository": "github.com/example/project", "provider": "NSIDC", "short_name": "ATL03",
            "dataset_name": "Photon data", "file": "read.py", "commit": "abc123",
            "file_url": "https://github.com/example/project/blob/abc123/read.py",
            "search_term": "ATL03", "attempt_status": completed, "latest_attempt": "true",
            "matching_lines": [{"line_number": 5, "text": 'load("ATL03")'}],
        }
        rows = collapse_batch_file_rows([
            base_row, base_row,
            {**base_row, "search_term": "ATL03_007", "attempt_status": "incomplete", "latest_attempt": "false",
             "matching_lines": [{"line_number": 5, "text": 'old_load("ATL03_007")'}]},
            {**base_row, "short_name": "ATL06"},
            {**base_row, "commit": "older", "latest_attempt": "false"},
        ], [
            {"provider": "NSIDC", "short_name": "ATL03", "status": completed},
            {"provider": "NSIDC", "short_name": "ATL03", "status": "incomplete"},
            {"provider": "NSIDC", "short_name": "ATL06", "status": completed},
        ], catalog_complete=False)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row["match_id"] for row in rows}), 3)
        first_row = rows[0]
        self.assertEqual(first_row["line_numbers"], "5")
        self.assertEqual(len(first_row["snippet"].splitlines()), 2)
        self.assertIn("disagree on text", first_row["warnings"])
        self.assertIn("historical search attempts", first_row["warnings"])
        self.assertIn("multiple commits", first_row["warnings"])
        self.assertIn("source catalog is partial", first_row["warnings"])
        self.assertEqual(first_row["search_complete"], "false")
        self.assertEqual(rows[-1]["search_complete"], "true")


if __name__ == "__main__":
    unittest.main()

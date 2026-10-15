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
from nasa_eo_search.pipeline import main
from nasa_eo_search.pipeline_tables import FILE_MATCH_COLUMNS, collapse_batch_file_rows
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
        *arguments: Search transport positional inputs, unused by the fake service.
        **options: Search transport settings, unused by the fake service.

    Yields:
        Sourcegraph match and completion events; each query repeats these hits.
    """
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
    yield ServerSentEvent("matches", json.dumps(matches))
    yield ServerSentEvent("progress", json.dumps({"done": True, "skipped": []}))
    yield ServerSentEvent("done", "{}")


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
                self.assertEqual(requests.call_count, 3)
                self.assertEqual(main(search_arguments), 0)
                self.assertEqual(requests.call_count, 3)
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
                self.assertIn("3 distinct search queries", terminal.getvalue())
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
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", return_value=example_search_events()) as requests, patch("nasa_eo_search.repo_batch.time.sleep", side_effect=KeyboardInterrupt()), redirect_stderr(StringIO()):
                self.assertEqual(main(arguments), 130)
                requests.assert_called_once()
            first_rows = read_pipeline_csv(output / "matches.csv")
            self.assertEqual(len(first_rows), 1)
            self.assertEqual(first_rows[0]["search_complete"], "false")
            with patch("nasa_eo_search.repo_search.stream_sourcegraph_search_events", side_effect=[example_search_events(), example_search_events()]) as requests, patch("nasa_eo_search.repo_batch.time.sleep"), redirect_stderr(StringIO()):
                self.assertEqual(main(arguments), 0)
                self.assertEqual(requests.call_count, 2)
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

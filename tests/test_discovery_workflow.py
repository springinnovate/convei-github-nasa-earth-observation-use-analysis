"""Exercise discovery modes, saved aliases, and report semantics without network calls."""

import argparse
import copy
import csv
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search import github_search, pipeline
from test_github_search import (
    make_github_arguments, make_github_page_response, read_saved_json, write_github_fixture_zip,
)
from test_pipeline_catalog import make_pipeline_product, save_pipeline_catalog


def make_empty_code_discovery(term: str, max_pages: int = 1, *, start_page: int = 1) -> dict:
    """Build a completed code-search response with no matching files.

    Args:
        term: Requested dataset name or documented alias.
        max_pages: Number of pages represented by the fixture.
        start_page: One-based page requested by the workflow.

    Returns:
        Empty search receipt using the existing code-search fixture schema.
    """
    return {**make_github_page_response(term, max_pages, start_page=start_page),
            "results": [], "total_count": 0}


def make_empty_repository_discovery(term: str, max_pages: int = 1, *, start_page: int = 1) -> dict:
    """Build a completed repository-search response with no candidates.

    Args:
        term: Requested dataset name or documented alias.
        max_pages: Number of pages represented by the fixture.
        start_page: One-based page requested by the workflow.

    Returns:
        Empty receipt distinguishing repository discovery from code search.
    """
    return {**make_empty_code_discovery(term, max_pages, start_page=start_page),
            "provider": "github-repositories"}


def make_repository_discovery_hit(term: str, max_pages: int = 1, *, start_page: int = 1) -> dict:
    """Build a public repository candidate without claiming a matching file.

    Args:
        term: Requested dataset name or documented alias.
        max_pages: Number of pages represented by the fixture.
        start_page: One-based page requested by the workflow.

    Returns:
        Receipt with one public repository and validated selection metadata.
    """
    return {**make_empty_repository_discovery(term, max_pages, start_page=start_page),
            "total_count": 1, "results": [{
                "repository": "github.com/example/project", "file": "",
                "url": "https://github.com/example/project",
                "repository_metadata": {"fork": False, "archived": False, "size_kb": 1, "stars": 2},
            }]}


def read_workflow_csv(path: Path) -> list[dict[str, str]]:
    """Read a generated workflow report using its exported column names.

    Args:
        path: CSV report written inside the temporary search output.

    Returns:
        String-valued rows decoded with any UTF-8 byte-order mark removed.
    """
    with path.open(encoding="utf-8-sig", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def write_workflow_alias_csv(path: Path, source_url: str = "https://earthdata.nasa.gov/atl03") -> None:
    """Write one cited alias for checking saved query provenance.

    Args:
        path: Temporary CSV file to create or replace.
        source_url: Source citation saved alongside the additional identifier.
    """
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["short_name", "term", "source_url"])
        writer.writerow(["ATL03", "ATL03_PHOTONS", source_url])


class DiscoveryWorkflowTests(unittest.TestCase):
    """Verify that complementary discovery remains bounded and reproducible."""

    def setUp(self) -> None:
        """Replace every discovery, commit-resolution, and download service call."""
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.catalog = save_pipeline_catalog(self.root, [make_pipeline_product(), make_pipeline_product("ATL06")])
        self.output = self.root / "output"
        service_patches = {
            "code_search": patch.object(github_search, "search_github_code", side_effect=make_empty_code_discovery),
            "repository_search": patch.object(github_search, "search_github_repositories", side_effect=make_empty_repository_discovery),
            "grep_search": patch.object(github_search, "search_grep_app", side_effect=AssertionError("Unexpected grep.app request")),
            "resolve_commit": patch.object(github_search, "resolve_public_repository_commit", return_value="a" * 40),
            "download_archive": patch.object(github_search, "download_github_repository_zip", side_effect=write_github_fixture_zip),
        }
        for attribute, service_patch in service_patches.items():
            setattr(self, attribute, service_patch.start())
            self.addCleanup(service_patch.stop)

    def run_search(self, arguments: argparse.Namespace) -> int:
        """Run the search workflow while suppressing progress text in test output.

        Args:
            arguments: Search options built with the existing fixture helper.

        Returns:
            Workflow exit code; validation exceptions propagate to the test.
        """
        with redirect_stderr(StringIO()):
            return github_search.search_github_catalog(arguments)

    def assert_services_not_called(self) -> None:
        """Check that validation or completed resume has made no external requests."""
        for service in (self.code_search, self.repository_search, self.grep_search,
                        self.resolve_commit, self.download_archive):
            service.assert_not_called()

    def reset_service_history(self) -> None:
        """Clear mock call counts while preserving all offline response behavior."""
        for service in (self.code_search, self.repository_search, self.grep_search,
                        self.resolve_commit, self.download_archive):
            service.reset_mock()

    def test_cli_default_uses_both_sources_and_resumes_after_request_cap(self) -> None:
        """Use the real CLI defaults and resume the second source without repeating the first."""
        command = ["search", str(self.catalog), "--short-name", "ATL03", "--github-pages", "1",
                   "--max-requests", "1", "--output", str(self.output)]
        with redirect_stderr(StringIO()):
            self.assertEqual(pipeline.main(command), 4)
        self.assertEqual(self.repository_search.call_count, 1)
        self.code_search.assert_not_called()
        first_receipt = next((self.output / "discovery").glob("*.json"))
        receipt_before = first_receipt.read_bytes()
        plan = read_saved_json(self.output / "plan.json")
        self.assertEqual(plan["github_discovery"], "code-and-readme")
        self.assertEqual(read_saved_json(self.output / "summary.json")["requests_this_run"], 1)
        with redirect_stderr(StringIO()):
            self.assertEqual(pipeline.main(command), 0)
            self.assertEqual(pipeline.main(command), 0)
        self.assertEqual(self.repository_search.call_count, 1)
        self.assertEqual(self.code_search.call_count, 1)
        self.assertEqual(first_receipt.read_bytes(), receipt_before)
        self.assertEqual(len(list((self.output / "discovery").glob("*.json"))), 2)
        self.resolve_commit.assert_not_called()
        self.download_archive.assert_not_called()

    def test_request_budget_interleaves_datasets_sources_and_pages_across_resume(self) -> None:
        """Give each dataset both sources before fetching later pages and honor each run's cap."""
        request_order = []

        def record_code_page(term: str, max_pages: int = 1, *, start_page: int = 1) -> dict:
            """Record an offline code page and advertise one additional page when needed.

            Args:
                term: Dataset requested by the scheduler.
                max_pages: Page count forwarded by the scheduler.
                start_page: Page number being requested.

            Returns:
                Empty bounded response, capped on page one and returned on page two.
            """
            request_order.append(("github", term, start_page))
            return {**make_empty_code_discovery(term, max_pages, start_page=start_page),
                    "status": "capped" if start_page == 1 else "returned",
                    "has_more": start_page == 1, "total_count": 150}

        def record_repository_page(term: str, max_pages: int = 1, *, start_page: int = 1) -> dict:
            """Record an offline repository page using the same two-page fixture.

            Args:
                term: Dataset requested by the scheduler.
                max_pages: Page count forwarded by the scheduler.
                start_page: Page number being requested.

            Returns:
                Empty repository response carrying the expected pagination flags.
            """
            response = record_code_page(term, max_pages, start_page=start_page)
            request_order[-1] = ("github-repositories", term, start_page)
            return {**response, "provider": "github-repositories"}

        self.code_search.side_effect = record_code_page
        self.repository_search.side_effect = record_repository_page
        arguments = make_github_arguments(self.catalog, self.output, short_name=["ATL03", "ATL06"],
                                          github_discovery=None, max_requests=3)
        for expected_status, expected_requests in ((4, 3), (4, 3), (0, 2)):
            self.assertEqual(self.run_search(arguments), expected_status)
            self.assertEqual(read_saved_json(self.output / "summary.json")["requests_this_run"], expected_requests)
        self.assertEqual(request_order, [(provider, term, page) for page in (1, 2)
                                        for term in ("ATL03", "ATL06")
                                        for provider in ("github-repositories", "github")])
        self.assertEqual(len(list((self.output / "discovery").glob("*.json"))), 8)

    def test_repository_hits_report_candidates_without_inventing_file_evidence(self) -> None:
        """Keep repository discovery rows separate from matching files established by local scanning."""
        self.repository_search.side_effect = make_repository_discovery_hit
        arguments = make_github_arguments(self.catalog, self.output, github_discovery=None)
        self.assertEqual(self.run_search(arguments), 0)
        discovery_rows = read_workflow_csv(self.output / "discovery.csv")
        self.assertEqual(len(discovery_rows), 1)
        self.assertEqual(discovery_rows[0]["file"], "")
        self.assertEqual(discovery_rows[0]["url"], "https://github.com/example/project")
        repository_row = next(row for row in read_workflow_csv(self.output / "searches.csv")
                              if row["provider"] == "github-repositories")
        self.assertEqual(repository_row["candidate_files"], "0")
        self.assertEqual(repository_row["candidate_repositories"], "1")
        matching_file_rows = read_workflow_csv(self.output / "matches.csv")
        self.assertEqual(len(matching_file_rows), 2)
        self.assertTrue(all(row["file"] == "example.py" for row in matching_file_rows))
        self.assertEqual(self.resolve_commit.call_count, 1)
        self.assertEqual(self.download_archive.call_count, 1)
        self.assertFalse(read_saved_json(self.output / "summary.json")["search_complete"])

    def test_saved_repository_metadata_is_validated_before_resume_requests(self) -> None:
        """Reject malformed saved metadata even when its receipt checksum is recomputed."""
        self.repository_search.side_effect = make_repository_discovery_hit
        arguments = make_github_arguments(self.catalog, self.output, github_discovery=None)
        self.assertEqual(self.run_search(arguments), 0)
        receipt_path = github_search.discovery_receipt_path(self.output, "github-repositories", "ATL03", 1, 1)
        original = read_saved_json(receipt_path)
        summary_before = (self.output / "summary.json").read_bytes()
        self.reset_service_history()
        for field, invalid_value in (("fork", 0), ("archived", "false"), ("size_kb", -1),
                                     ("stars", True), ("stars", "2")):
            with self.subTest(field=field, value=invalid_value):
                changed = copy.deepcopy(original)
                changed["results"][0]["repository_metadata"][field] = invalid_value
                github_search.save_discovery_receipt(receipt_path, changed)
                with self.assertRaises(ValueError):
                    self.run_search(arguments)
                self.assert_services_not_called()
                self.assertEqual((self.output / "summary.json").read_bytes(), summary_before)

    def test_saved_repository_receipt_cannot_claim_a_file_or_external_url(self) -> None:
        """Reject modified repository discovery locations before they become file evidence."""
        self.repository_search.side_effect = make_repository_discovery_hit
        arguments = make_github_arguments(self.catalog, self.output, github_discovery=None)
        self.assertEqual(self.run_search(arguments), 0)
        receipt_path = github_search.discovery_receipt_path(self.output, "github-repositories", "ATL03", 1, 1)
        original = read_saved_json(receipt_path)
        self.reset_service_history()
        for field, invalid_value in (("file", "README.md"), ("url", "https://example.org/private")):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed["results"][0][field] = invalid_value
                github_search.save_discovery_receipt(receipt_path, changed)
                with self.assertRaises(ValueError):
                    self.run_search(arguments)
                self.assert_services_not_called()

    def test_malformed_saved_snapshot_metadata_fails_before_reports_or_requests(self) -> None:
        """Reject invalid optional snapshot fields before changing reports or cached evidence."""
        self.repository_search.side_effect = make_repository_discovery_hit
        arguments = make_github_arguments(self.catalog, self.output, github_discovery=None)
        self.assertEqual(self.run_search(arguments), 0)
        manifest_path = self.output / "snapshots.json"
        original = read_saved_json(manifest_path)
        report_paths = [self.output / name for name in
                        ("summary.json", "plan.json", "discovery.csv", "searches.csv", "repositories.csv", "matches.csv")]
        report_bytes = {path: path.read_bytes() for path in report_paths}
        self.reset_service_history()
        invalid_metadata = [None, [], "invalid", 7, {"fork": 0}, {"archived": "false"},
                            {"size_kb": -1}, {"size_kb": True}, {"stars": "2"}, {"stars": None},
                            {"repository": "github.com/untrusted/override"}]
        for metadata in invalid_metadata:
            with self.subTest(metadata=metadata):
                changed = copy.deepcopy(original)
                changed["repositories"][0]["discovery_metadata"] = metadata
                github_search.save_github_json(manifest_path, changed)
                manifest_bytes = manifest_path.read_bytes()
                with (patch.object(github_search, "publish_github_reports") as publish_reports,
                      self.assertRaisesRegex(ValueError, "snapshot discovery metadata")):
                    self.run_search(arguments)
                publish_reports.assert_not_called()
                self.assert_services_not_called()
                self.assertEqual(manifest_path.read_bytes(), manifest_bytes)
                self.assertEqual({path: path.read_bytes() for path in report_paths}, report_bytes)

    def test_snapshot_metadata_validation_preserves_legacy_and_partial_public_fields(self) -> None:
        """Accept absent, empty, partial, and complete public discovery metadata without mutation."""
        self.repository_search.side_effect = make_repository_discovery_hit
        arguments = make_github_arguments(self.catalog, self.output, github_discovery=None)
        self.assertEqual(self.run_search(arguments), 0)
        original = read_saved_json(self.output / "snapshots.json")["repositories"]
        plan = read_saved_json(self.output / "plan.json")
        for metadata in (None, {}, {"fork": False}, {"archived": True}, {"size_kb": 0, "stars": 0},
                         {"fork": False, "archived": False, "size_kb": 10, "stars": 2}):
            with self.subTest(metadata=metadata):
                records = copy.deepcopy(original)
                if metadata is None:
                    records[0].pop("discovery_metadata")
                else:
                    records[0]["discovery_metadata"] = metadata
                frozen = copy.deepcopy(records)
                github_search.validate_snapshot_manifest(self.output, records, plan)
                self.assertEqual(records, frozen)

    def test_legacy_plan_resumes_code_only_without_rewriting_saved_plan(self) -> None:
        """Preserve pre-feature saved settings and continue only their pending code page."""
        first_page = {**make_empty_code_discovery("ATL03"), "status": "capped", "has_more": True, "total_count": 150}
        second_page = {**make_empty_code_discovery("ATL03", start_page=2), "total_count": 150}
        self.code_search.side_effect = [first_page, second_page]
        arguments = make_github_arguments(self.catalog, self.output, max_requests=1)
        self.assertEqual(self.run_search(arguments), 4)
        plan_path = self.output / "plan.json"
        legacy_plan = read_saved_json(plan_path)
        for key in ("github_discovery", "discovery_aliases", "discovery_queries", "selection_policy"):
            legacy_plan.pop(key)
        github_search.save_github_json(plan_path, legacy_plan)
        legacy_bytes = plan_path.read_bytes()
        arguments.github_discovery = None
        self.assertEqual(self.run_search(arguments), 0)
        self.assertEqual([call.kwargs["start_page"] for call in self.code_search.call_args_list], [1, 2])
        self.repository_search.assert_not_called()
        self.assertEqual(plan_path.read_bytes(), legacy_bytes)
        self.assertEqual(read_saved_json(self.output / "summary.json")["discovery_mode"], "code")

    def test_alias_provenance_is_frozen_and_resume_does_not_need_original_csv(self) -> None:
        """Retain a cited alias in the saved plan and reports after its input CSV is removed."""
        alias_path = self.root / "aliases.csv"
        write_workflow_alias_csv(alias_path)
        arguments = make_github_arguments(self.catalog, self.output, github_discovery=None,
                                          discovery_aliases=alias_path)
        self.assertEqual(self.run_search(arguments), 0)
        plan_path = self.output / "plan.json"
        plan_before = plan_path.read_bytes()
        plan = read_saved_json(plan_path)
        self.assertEqual(plan["discovery_aliases"], [{"short_name": "ATL03", "term": "ATL03_PHOTONS",
                                                     "source_url": "https://earthdata.nasa.gov/atl03"}])
        self.assertEqual(plan["discovery_queries"][1], plan["discovery_aliases"][0])
        for search in (self.code_search, self.repository_search):
            self.assertEqual([call.args[0] for call in search.call_args_list], ["ATL03", "ATL03_PHOTONS"])
        alias_rows = [row for row in read_workflow_csv(self.output / "searches.csv") if row["term"] == "ATL03_PHOTONS"]
        self.assertEqual(len(alias_rows), 2)
        self.assertTrue(all(row["alias_source_urls"] == "https://earthdata.nasa.gov/atl03" for row in alias_rows))
        alias_path.unlink()
        arguments.discovery_aliases = None
        self.reset_service_history()
        self.assertEqual(self.run_search(arguments), 0)
        self.assert_services_not_called()
        self.assertEqual(plan_path.read_bytes(), plan_before)

    def test_changed_aliases_or_discovery_mode_are_rejected_before_requests(self) -> None:
        """Keep changed query settings from mixing into an existing search output."""
        alias_path = self.root / "aliases.csv"
        write_workflow_alias_csv(alias_path)
        arguments = make_github_arguments(self.catalog, self.output, github_discovery=None,
                                          discovery_aliases=alias_path)
        self.assertEqual(self.run_search(arguments), 0)
        plan_before = (self.output / "plan.json").read_bytes()
        self.reset_service_history()
        write_workflow_alias_csv(alias_path, "https://earthdata.nasa.gov/different-source")
        with self.assertRaises(ValueError):
            self.run_search(arguments)
        arguments.discovery_aliases = None
        arguments.github_discovery = "code"
        with self.assertRaises(ValueError):
            self.run_search(arguments)
        self.assert_services_not_called()
        self.assertEqual((self.output / "plan.json").read_bytes(), plan_before)

    def test_corrupt_saved_query_or_selection_policy_cannot_resume(self) -> None:
        """Reject altered frozen query mappings and unsupported selection policies before requests."""
        arguments = make_github_arguments(self.catalog, self.output, github_discovery=None)
        self.assertEqual(self.run_search(arguments), 0)
        plan_path = self.output / "plan.json"
        original = read_saved_json(plan_path)
        self.reset_service_history()
        corruptions = [
            ("discovery_queries", [{"short_name": "ATL03", "term": "unreviewed", "source_url": ""}]),
            ("discovery_queries", "not-a-query-list"),
            ("selection_policy", "unknown-policy"),
            ("github_discovery", "unknown-mode"),
            ("discovery_aliases", [{"term": "missing-dataset-and-source"}]),
        ]
        for field, value in corruptions:
            with self.subTest(field=field, value=value):
                changed = {**original, field: value}
                github_search.save_github_json(plan_path, changed)
                with self.assertRaises(ValueError):
                    self.run_search(arguments)
                self.assert_services_not_called()

    def test_malformed_saved_discovery_mode_fails_before_reports_or_requests(self) -> None:
        """Reject nontext or blank saved modes with a validation error, preserving existing files."""
        arguments = make_github_arguments(self.catalog, self.output, github_discovery=None)
        self.assertEqual(self.run_search(arguments), 0)
        plan_path = self.output / "plan.json"
        original = read_saved_json(plan_path)
        summary_before = (self.output / "summary.json").read_bytes()
        self.reset_service_history()
        for mode in ([], {}, 1, False, None, ""):
            with self.subTest(mode=mode):
                github_search.save_github_json(plan_path, {**original, "github_discovery": mode})
                plan_bytes = plan_path.read_bytes()
                with (patch.object(github_search, "publish_github_reports") as publish_reports,
                      self.assertRaisesRegex(ValueError, "GitHub discovery must be")):
                    self.run_search(arguments)
                publish_reports.assert_not_called()
                self.assert_services_not_called()
                self.assertEqual(plan_path.read_bytes(), plan_bytes)
                self.assertEqual((self.output / "summary.json").read_bytes(), summary_before)


if __name__ == "__main__":
    unittest.main()

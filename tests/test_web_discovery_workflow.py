"""Check optional paid web discovery, saved evidence, and free resume offline."""

import copy
from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search import github_search, pipeline
from test_discovery_workflow import (
    make_empty_code_discovery, make_empty_repository_discovery,
    read_workflow_csv, write_workflow_alias_csv,
)
from test_github_search import (
    make_github_arguments, make_github_page_response, read_saved_json, write_github_fixture_zip,
)
from test_pipeline_catalog import make_pipeline_product, save_pipeline_catalog


def make_empty_web_discovery(term: str, *, queries: list[str], model: str,
                             api_key: str, ca_bundle_path: str | None = None) -> dict:
    """Return a bounded web receipt without contacting a model or GitHub.

    Args:
        term: Canonical selected dataset identifier.
        queries: Frozen queries passed to the web provider.
        model: Requested model, retained in the receipt.
        api_key: Fake credential accepted by the offline provider fixture.
        ca_bundle_path: Optional certificate path, unused in this fixture.

    Returns:
        Limited discovery receipt with request provenance and no candidates.
    """
    return {"provider": "openai-web", "term": term, "query": "\n".join(queries),
            "requested_queries": queries, "actual_queries": queries, "model": model,
            "status": "limited", "results": [], "sources": [], "has_more": False,
            "pages_returned": 1, "start_page": 1, "web_search_calls": len(queries),
            "response_id": "response_offline", "usage": {"input_tokens": 30, "output_tokens": 10},
            "warnings": ["Web sources are a bounded sample, not complete repository coverage."]}


def make_web_repository_hit(term: str, *, queries: list[str], model: str,
                            api_key: str, ca_bundle_path: str | None = None) -> dict:
    """Return one publicly verified repository lead without claiming a matching file.

    Args:
        term: Canonical selected dataset identifier.
        queries: Frozen queries passed to the web provider.
        model: Requested model recorded in the response.
        api_key: Fake credential used only by the fixture signature.
        ca_bundle_path: Optional certificate path, unused in this fixture.

    Returns:
        Web receipt containing a repository candidate and its consulted source URL.
    """
    receipt = make_empty_web_discovery(term, queries=queries, model=model,
                                       api_key=api_key, ca_bundle_path=ca_bundle_path)
    source_url = "https://github.com/example/project/blob/main/README.md"
    receipt["results"] = [{"repository": "github.com/example/project", "file": "",
                           "url": "https://github.com/example/project", "source_urls": [source_url],
                           "repository_metadata": {"fork": False, "archived": False,
                                                   "size_kb": 1, "stars": 2}}]
    receipt["sources"] = [{"url": source_url}]
    return receipt


class WebDiscoveryWorkflowTests(unittest.TestCase):
    """Verify web discovery through the real planner, scheduler, and report pipeline."""

    def setUp(self) -> None:
        """Replace every external operation and supply a temporary two-product catalog."""
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.catalog = save_pipeline_catalog(self.root, [make_pipeline_product(), make_pipeline_product("ATL06")])
        self.output = self.root / "output"
        service_patches = {
            "web_search": patch.object(github_search, "search_openai_web", side_effect=make_empty_web_discovery),
            "load_key": patch.object(github_search, "load_openai_api_key", return_value="fake-test-credential"),
            "code_search": patch.object(github_search, "search_github_code", side_effect=make_empty_code_discovery),
            "repository_search": patch.object(github_search, "search_github_repositories", side_effect=make_empty_repository_discovery),
            "grep_search": patch.object(github_search, "search_grep_app", side_effect=AssertionError("Unexpected grep.app request")),
            "resolve_commit": patch.object(github_search, "resolve_public_repository_commit", return_value="a" * 40),
            "download_archive": patch.object(github_search, "download_github_repository_zip", side_effect=write_github_fixture_zip),
        }
        for attribute, service_patch in service_patches.items():
            setattr(self, attribute, service_patch.start())
            self.addCleanup(service_patch.stop)

    def run_search(self, **options) -> int:
        """Run the GitHub workflow with optional web settings and suppress progress text.

        Args:
            **options: Overrides for the existing bounded argument fixture.

        Returns:
            Workflow exit status. Saved-state validation errors propagate.
        """
        arguments = make_github_arguments(self.catalog, self.output, github_discovery=None, **options)
        with redirect_stderr(StringIO()):
            return github_search.search_github_catalog(arguments)

    def reset_service_history(self) -> None:
        """Clear mocked operation counts while preserving their offline responses."""
        for service in (self.web_search, self.load_key, self.code_search, self.repository_search,
                        self.grep_search, self.resolve_commit, self.download_archive):
            service.reset_mock()

    def assert_no_external_operations(self) -> None:
        """Check that no credentials, discovery services, or snapshots were requested."""
        for service in (self.web_search, self.load_key, self.code_search, self.repository_search,
                        self.grep_search, self.resolve_commit, self.download_archive):
            service.assert_not_called()

    def test_default_and_existing_runs_never_enable_paid_web_search_implicitly(self) -> None:
        """Keep ordinary and saved GitHub searches free of web requests or key reads."""
        self.assertEqual(self.run_search(), 0)
        original_plan = (self.output / "plan.json").read_bytes()
        self.web_search.assert_not_called()
        self.load_key.assert_not_called()
        self.reset_service_history()
        self.assertEqual(self.run_search(), 0)
        self.assert_no_external_operations()
        self.assertEqual((self.output / "plan.json").read_bytes(), original_plan)
        with self.assertRaises(ValueError):
            self.run_search(web_search_model="gpt-6.1-sol")
        self.assert_no_external_operations()
        self.assertEqual((self.output / "plan.json").read_bytes(), original_plan)

    def test_cli_web_dry_run_neither_reads_key_nor_creates_output(self) -> None:
        """Accept the opt-in CLI flags while leaving the filesystem and services untouched."""
        with redirect_stderr(StringIO()):
            status = pipeline.main(["search", str(self.catalog), "--short-name", "ATL03",
                                    "--web-search-model", "gpt-6.1-sol", "--api-key-file",
                                    str(self.root / "nonexistent.key"), "--dry-run", "--output", str(self.output)])
        self.assertEqual(status, 0)
        self.assertFalse(self.output.exists())
        self.assert_no_external_operations()

    def test_paid_request_uses_global_cap_and_cached_resume_needs_no_key(self) -> None:
        """Spend one discovery slot on web search and resume remaining free sources without recharging."""
        key_file = self.root / "explicit.key"
        self.assertEqual(self.run_search(web_search_model="gpt-6.1-sol", api_key_file=key_file,
                                         max_requests=1), 4)
        self.web_search.assert_called_once()
        self.load_key.assert_called_once_with(key_file)
        self.code_search.assert_not_called()
        self.repository_search.assert_not_called()
        receipt_path = github_search.discovery_receipt_path(self.output, "openai-web", "ATL03", 1, 1)
        saved_receipt = receipt_path.read_bytes()
        plan = read_saved_json(self.output / "plan.json")
        self.assertEqual(plan["web_discovery"]["model"], "gpt-6.1-sol")
        serialized_plan = json.dumps(plan)
        self.assertNotIn("fake-test-credential", serialized_plan)
        self.assertNotIn(str(key_file), serialized_plan)
        self.assertEqual(read_saved_json(self.output / "summary.json")["requests_this_run"], 1)
        self.reset_service_history()
        self.load_key.side_effect = AssertionError("Cached web search must not read credentials")
        self.assertEqual(self.run_search(), 4)
        self.web_search.assert_not_called()
        self.load_key.assert_not_called()
        self.assertEqual(self.code_search.call_count, 1)
        self.assertEqual(self.repository_search.call_count, 1)
        self.assertEqual(receipt_path.read_bytes(), saved_receipt)
        self.reset_service_history()
        self.assertEqual(self.run_search(), 4)
        self.assert_no_external_operations()
        self.assertEqual(read_saved_json(self.output / "summary.json")["requests_this_run"], 0)

    def test_web_search_runs_once_per_canonical_dataset_and_never_paginates_aliases(self) -> None:
        """Keep aliases and GitHub page limits from multiplying paid web requests."""
        alias_path = self.root / "aliases.csv"
        write_workflow_alias_csv(alias_path)
        self.assertEqual(self.run_search(short_name=["ATL03", "ATL06"], discovery_aliases=alias_path,
                                         web_search_model="gpt-6.1-sol", github_pages=5,
                                         max_requests=20), 4)
        self.assertEqual([call.args[0] for call in self.web_search.call_args_list], ["ATL03", "ATL06"])
        web_receipts = [read_saved_json(path) for path in (self.output / "discovery").glob("*.json")
                        if read_saved_json(path)["provider"] == "openai-web"]
        self.assertEqual(len(web_receipts), 2)
        self.assertTrue(all(receipt["page"] == 1 for receipt in web_receipts))
        web_rows = [row for row in read_workflow_csv(self.output / "searches.csv") if row["provider"] == "openai-web"]
        self.assertEqual({row["term"] for row in web_rows}, {"ATL03", "ATL06"})
        self.assertTrue(all(row["status"] == "limited" for row in web_rows))
        self.assertEqual(read_saved_json(self.output / "summary.json")["requests_this_run"], 8)

    def test_web_candidates_keep_source_provenance_and_obtain_file_evidence_from_scan(self) -> None:
        """Report source URLs separately from actual matching files in the pinned snapshot."""
        self.web_search.side_effect = make_web_repository_hit
        self.code_search.side_effect = make_github_page_response
        self.assertEqual(self.run_search(web_search_model="gpt-6.1-sol"), 4)
        discovery_rows = read_workflow_csv(self.output / "discovery.csv")
        self.assertEqual(len(discovery_rows), 2)
        web_discovery = next(row for row in discovery_rows if row["provider"] == "openai-web")
        self.assertEqual(web_discovery["file"], "")
        self.assertEqual(web_discovery["url"], "https://github.com/example/project")
        self.assertIn("https://github.com/example/project/blob/main/README.md",
                      web_discovery["source_urls"])
        web_row = next(row for row in read_workflow_csv(self.output / "searches.csv")
                       if row["provider"] == "openai-web")
        self.assertEqual(web_row["candidate_files"], "0")
        self.assertEqual(web_row["candidate_repositories"], "1")
        matches = read_workflow_csv(self.output / "matches.csv")
        self.assertEqual(len(matches), 2)
        self.assertTrue(all(row["file"] == "example.py" for row in matches))
        self.assertEqual(self.download_archive.call_count, 1)
        self.assertEqual(self.resolve_commit.call_count, 1)
        repository_rows = read_workflow_csv(self.output / "repositories.csv")
        self.assertEqual(len(repository_rows), 1)
        self.assertIn("openai-web:ATL03", repository_rows[0]["discovered_by"])
        self.assertIn("github:ATL03", repository_rows[0]["discovered_by"])
        self.assertFalse(read_saved_json(self.output / "summary.json")["search_complete"])

    def test_unreadable_key_fails_before_sending_or_recording_paid_request(self) -> None:
        """Preserve a zero-call failure when credentials cannot be loaded."""
        self.load_key.side_effect = ValueError("API key file could not be read.")
        self.assertEqual(self.run_search(web_search_model="gpt-6.1-sol"), 1)
        self.web_search.assert_not_called()
        self.code_search.assert_not_called()
        self.repository_search.assert_not_called()
        self.assertEqual(list((self.output / "discovery").glob("*.json")), [])
        summary = read_saved_json(self.output / "summary.json")
        self.assertEqual(summary["requests_this_run"], 0)
        self.assertEqual(summary["discovery_requests_saved"], 0)

    def test_web_failure_requires_explicit_retry_and_preserves_failed_receipt(self) -> None:
        """Keep unsuccessful paid requests visible and avoid automatic repeat charges."""
        def fail_web_request(term: str, **request_options) -> dict:
            """Return an unsuccessful attempt with its exact frozen request provenance.

            Args:
                term: Canonical dataset requested by the scheduler.
                **request_options: Provider arguments forwarded unchanged to the fixture.

            Returns:
                Error receipt that can be safely validated and retried on resume.
            """
            failure = make_empty_web_discovery(term, **request_options)
            return {**failure, "status": "error", "warnings": ["Web request failed."],
                    "web_search_calls": 0}

        self.web_search.side_effect = fail_web_request
        self.assertEqual(self.run_search(web_search_model="gpt-6.1-sol"), 1)
        first_receipt = github_search.discovery_receipt_path(self.output, "openai-web", "ATL03", 1, 1)
        original = first_receipt.read_bytes()
        self.reset_service_history()
        self.assertEqual(self.run_search(), 1)
        self.assert_no_external_operations()
        self.web_search.side_effect = make_empty_web_discovery
        self.assertEqual(self.run_search(retry_incomplete=True), 4)
        self.web_search.assert_called_once()
        self.assertEqual(first_receipt.read_bytes(), original)
        self.assertTrue(github_search.discovery_receipt_path(self.output, "openai-web", "ATL03", 1, 2).exists())

    def test_changed_saved_model_or_query_rules_fail_before_key_reads_or_publication(self) -> None:
        """Reject incompatible or edited web settings without modifying previous reports."""
        self.assertEqual(self.run_search(web_search_model="gpt-6.1-sol"), 4)
        plan_path = self.output / "plan.json"
        original_plan = plan_path.read_bytes()
        original_summary = (self.output / "summary.json").read_bytes()
        self.reset_service_history()
        with self.assertRaises(ValueError):
            self.run_search(web_search_model="another-model")
        self.assert_no_external_operations()
        self.assertEqual((self.output / "summary.json").read_bytes(), original_summary)
        changed = copy.deepcopy(read_saved_json(plan_path))
        changed["web_discovery"]["queries"][0]["queries"] = ['site:github.com "different-product"']
        plan_path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_search()
        self.assert_no_external_operations()
        self.assertEqual((self.output / "summary.json").read_bytes(), original_summary)
        plan_path.write_bytes(original_plan)

    def test_malformed_saved_web_usage_fails_before_report_changes(self) -> None:
        """Validate saved usage fields before report aggregation or external operations."""
        self.assertEqual(self.run_search(web_search_model="gpt-6.1-sol"), 4)
        receipt_path = github_search.discovery_receipt_path(self.output, "openai-web", "ATL03", 1, 1)
        changed_receipt = read_saved_json(receipt_path)
        changed_receipt["usage"] = None
        github_search.save_discovery_receipt(receipt_path, changed_receipt)
        report_names = ("summary.json", "searches.csv", "discovery.csv", "repositories.csv", "matches.csv")
        original_reports = {name: (self.output / name).read_bytes() for name in report_names}
        self.reset_service_history()
        with self.assertRaises(ValueError):
            self.run_search()
        self.assert_no_external_operations()
        self.assertEqual({name: (self.output / name).read_bytes() for name in report_names}, original_reports)

    def test_sourcegraph_rejects_web_flags_before_any_search(self) -> None:
        """Keep a GitHub discovery option from silently affecting the Sourcegraph backend."""
        with patch.object(pipeline, "search_sourcegraph_catalog") as sourcegraph_search, redirect_stderr(StringIO()):
            status = pipeline.main(["search", str(self.catalog), "--backend", "sourcegraph",
                                    "--web-search-model", "gpt-6.1-sol", "--output", str(self.output)])
        self.assertEqual(status, 1)
        sourcegraph_search.assert_not_called()
        self.assert_no_external_operations()


if __name__ == "__main__":
    unittest.main()

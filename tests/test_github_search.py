"""Exercise durable GitHub discovery and pinned archive scans without network."""

import argparse
from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from nasa_eo_search import github_search
from test_pipeline_catalog import make_pipeline_product, save_pipeline_catalog


def make_github_arguments(catalog: Path, output: Path, **overrides) -> argparse.Namespace:
    """Create command options with explicit small offline test budgets.

    Args:
        catalog: Saved validated catalog directory.
        output: New or resumable search output directory.
        **overrides: Options replacing the small default fixture configuration.

    Returns:
        Namespace accepted by the GitHub search backend.
    """

    return argparse.Namespace(**{
        "catalog": catalog, "output": output, "short_name": ["ATL03"], "dry_run": False,
        "max_requests": 10, "ca_bundle": None, "github_pages": 2, "max_repositories": 2,
        "max_archive_mb": 1, "max_total_mb": 2, "include_grep_app": False,
        "retry_incomplete": False, **overrides,
    })


def make_page_receipt(term: str, max_pages: int = 1, *, start_page: int = 1) -> dict:
    """Return one final public result page without relying on seeded controls.

    Args:
        term: Requested catalog short name.
        max_pages: Page count requested by the backend, expected to be one.
        start_page: Actual one-based page independently persisted by the backend.

    Returns:
        Public code-discovery receipt with exact pagination metadata.
    """

    repository = "github.com/example/project"
    return {"provider": "github", "term": term, "query": term, "status": "returned",
            "has_more": False, "start_page": start_page, "pages_returned": max_pages,
            "total_count": 1, "elapsed_seconds": 0.1, "warnings": ["Index scope is limited."], "raw": [],
            "results": [{"repository": repository, "file": "example.py",
                         "url": "https://github.com/example/project/blob/main/example.py"}]}


def write_github_fixture_archive(repository: str, commit: str, destination: Path,
                                  maximum_bytes: int, ca_bundle: str | None = None) -> int:
    """Write a tiny safe snapshot containing terms from two catalog products.

    Args:
        repository: Public repository, unused in the synthetic archive.
        commit: Immutable commit, unused in the synthetic archive.
        destination: Test-owned archive destination.
        maximum_bytes: Download bound, larger than the synthetic fixture.
        ca_bundle: Optional certificate path, unused in this offline fixture.

    Returns:
        Number of compressed bytes written.
    """

    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr("root/example.py", 'read("ATL03_007")\n# ATL06\n# MATL03 is not an ATL03 match inside that word\n')
    return destination.stat().st_size


def read_saved_json(path: Path) -> dict:
    """Read a test output JSON object.

    Args:
        path: Saved plan, summary, receipt, or manifest.

    Returns:
        Parsed JSON object.
    """

    return json.loads(path.read_text(encoding="utf-8"))


class GitHubSearchTests(unittest.TestCase):
    """Keep requests bounded, snapshots immutable, and resumes evidence-driven."""

    def test_dry_run_has_no_output_or_service_calls(self) -> None:
        """Preview selected discovery and all-catalog scanning without mutations."""

        with TemporaryDirectory() as temporary, patch.object(github_search, "search_github_code") as search:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            arguments = make_github_arguments(catalog, root / "output", dry_run=True)
            with redirect_stderr(StringIO()):
                self.assertEqual(github_search.search_github_catalog(arguments), 0)
            self.assertFalse(arguments.output.exists())
            search.assert_not_called()

    def test_scan_all_catalog_terms_and_resume_without_calls(self) -> None:
        """Scan all products in discovered snapshots and reuse verified cached work."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product(), make_pipeline_product("ATL06")])
            arguments = make_github_arguments(catalog, root / "output")
            with (patch.object(github_search, "search_github_code", side_effect=make_page_receipt) as search,
                  patch.object(github_search, "resolve_public_repository_commit", return_value="a" * 40) as pin,
                  patch.object(github_search, "download_github_snapshot", side_effect=write_github_fixture_archive) as download,
                  redirect_stderr(StringIO())):
                self.assertEqual(github_search.search_github_catalog(arguments), 0)
                self.assertEqual(search.call_count, 1)
                self.assertEqual(pin.call_count, 1)
                self.assertEqual(download.call_count, 1)
                self.assertEqual(github_search.search_github_catalog(arguments), 0)
                self.assertEqual(search.call_count, 1)
                self.assertEqual(download.call_count, 1)
            frozen = read_saved_json(arguments.output / "catalog.json")
            self.assertEqual(len(frozen["products"]), 2)
            summary = read_saved_json(arguments.output / "summary.json")
            self.assertFalse(summary["search_complete"])
            self.assertEqual(summary["match_rows"], 2)
            snapshots = read_saved_json(arguments.output / "snapshots.json")["repositories"]
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["repository"], "github.com/example/project")
            self.assertNotIn("pydap", json.dumps(snapshots))

    def test_page_cap_resumes_at_next_page(self) -> None:
        """Persist page one and request only page two on a later invocation."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            arguments = make_github_arguments(catalog, root / "output", max_requests=1)
            first = {**make_page_receipt("ATL03"), "status": "capped", "has_more": True, "total_count": 150}
            second = {**make_page_receipt("ATL03", start_page=2), "total_count": 150}
            with (patch.object(github_search, "search_github_code", side_effect=[first, second]) as search,
                  patch.object(github_search, "resolve_public_repository_commit", return_value="a" * 40),
                  patch.object(github_search, "download_github_snapshot", side_effect=write_github_fixture_archive) as download,
                  redirect_stderr(StringIO())):
                self.assertEqual(github_search.search_github_catalog(arguments), 4)
                self.assertEqual(github_search.search_github_catalog(arguments), 0)
                self.assertEqual([call.kwargs["start_page"] for call in search.call_args_list], [1, 2])
                self.assertEqual(download.call_count, 1)
            receipts = list((arguments.output / "discovery").glob("*.json"))
            self.assertEqual(len(receipts), 2)

    def test_failed_request_requires_explicit_retry_and_preserves_attempt(self) -> None:
        """Do not automatically repeat a saved failure; retain it after recovery."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            arguments = make_github_arguments(catalog, root / "output")
            failure = {**make_page_receipt("ATL03"), "status": "error", "results": []}
            with (patch.object(github_search, "search_github_code", return_value=failure) as search,
                  redirect_stderr(StringIO())):
                self.assertEqual(github_search.search_github_catalog(arguments), 1)
                self.assertEqual(github_search.search_github_catalog(arguments), 1)
                self.assertEqual(search.call_count, 1)
            arguments.retry_incomplete = True
            recovered = {**make_page_receipt("ATL03"), "results": [], "total_count": 0}
            with patch.object(github_search, "search_github_code", return_value=recovered), redirect_stderr(StringIO()):
                self.assertEqual(github_search.search_github_catalog(arguments), 0)
            receipts = [read_saved_json(path) for path in (arguments.output / "discovery").glob("*.json")]
            self.assertEqual({row["attempt"] for row in receipts}, {1, 2})
            self.assertEqual({row["status"] for row in receipts}, {"error", "returned"})

    def test_discovery_is_round_robin_and_repository_selection_has_no_controls(self) -> None:
        """Spread a bounded run across products before requesting their later pages."""

        plan = {"discovery_terms": ["ATL03", "ATL06"], "include_grep_app": False,
                "github_pages": 2, "max_repositories": 2}
        first = {**make_page_receipt("ATL03"), "page": 1, "attempt": 1,
                 "results": [{"repository": f"github.com/first/repo{index}", "file": "x", "url": "x"} for index in range(3)]}
        second = {**make_page_receipt("ATL06"), "page": 1, "attempt": 1,
                  "results": [{"repository": "github.com/second/repo", "file": "x", "url": "x"}]}
        rows = github_search.reconcile_snapshot_repositories([first, second], [], plan)
        self.assertEqual({row["repository"] for row in rows if row["selected"]},
                         {"github.com/first/repo0", "github.com/second/repo"})
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "discovery").mkdir()
            first["has_more"], second["has_more"] = True, True
            with patch.object(github_search, "search_github_code", side_effect=[first, second]) as search, redirect_stderr(StringIO()):
                self.assertEqual(github_search.collect_github_discovery(output, plan, [], 2, False, None), 2)
            self.assertEqual([call.args[0] for call in search.call_args_list], ["ATL03", "ATL06"])

    def test_corrupt_archive_or_receipt_fails_before_network(self) -> None:
        """Refuse to redownload or overwrite damaged cached evidence."""

        for damage in ("archive", "receipt"):
            with self.subTest(damage=damage), TemporaryDirectory() as temporary:
                root = Path(temporary)
                catalog = save_pipeline_catalog(root, [make_pipeline_product()])
                arguments = make_github_arguments(catalog, root / "output")
                with (patch.object(github_search, "search_github_code", side_effect=make_page_receipt),
                      patch.object(github_search, "resolve_public_repository_commit", return_value="a" * 40),
                      patch.object(github_search, "download_github_snapshot", side_effect=write_github_fixture_archive),
                      redirect_stderr(StringIO())):
                    github_search.search_github_catalog(arguments)
                if damage == "archive":
                    path = next((arguments.output / "snapshots").glob("*.zip"))
                else:
                    path = next((arguments.output / "discovery").glob("*.json"))
                path.write_bytes(b"damaged evidence")
                summary_before = (arguments.output / "summary.json").read_bytes()
                with patch.object(github_search, "search_github_code") as search, redirect_stderr(StringIO()):
                    with self.assertRaises(ValueError):
                        github_search.search_github_catalog(arguments)
                search.assert_not_called()
                self.assertEqual(path.read_bytes(), b"damaged evidence")
                self.assertEqual((arguments.output / "summary.json").read_bytes(), summary_before)

    def test_saved_limits_are_retained_and_changed_selection_refused(self) -> None:
        """Infer omitted resume options without silently changing frozen scope."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product(), make_pipeline_product("ATL06")])
            arguments = make_github_arguments(catalog, root / "output")
            empty = {**make_page_receipt("ATL03"), "results": [], "total_count": 0}
            with patch.object(github_search, "search_github_code", return_value=empty), redirect_stderr(StringIO()):
                self.assertEqual(github_search.search_github_catalog(arguments), 0)
            arguments.short_name = None
            for name in github_search.PLAN_DEFAULTS:
                setattr(arguments, name, None)
            with patch.object(github_search, "search_github_code") as search, redirect_stderr(StringIO()):
                self.assertEqual(github_search.search_github_catalog(arguments), 0)
                arguments.short_name = ["ATL06"]
                with self.assertRaises(ValueError):
                    github_search.search_github_catalog(arguments)
                search.assert_not_called()

    def test_interrupted_discovery_is_not_automatically_repeated(self) -> None:
        """Keep a running request receipt after Ctrl+C until explicitly retried."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            arguments = make_github_arguments(catalog, root / "output")
            with patch.object(github_search, "search_github_code", side_effect=KeyboardInterrupt), redirect_stderr(StringIO()):
                self.assertEqual(github_search.search_github_catalog(arguments), 130)
            self.assertEqual(read_saved_json(arguments.output / "summary.json")["requests_this_run"], 1)
            with patch.object(github_search, "search_github_code") as search, redirect_stderr(StringIO()):
                self.assertEqual(github_search.search_github_catalog(arguments), 1)
            search.assert_not_called()
            self.assertEqual(read_saved_json(arguments.output / "summary.json")["status"], "failed")

    def test_download_interrupt_is_resumable_only_with_explicit_retry(self) -> None:
        """Keep the pinned revision and report Ctrl+C without silently redownloading."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            arguments = make_github_arguments(catalog, root / "output")
            with (patch.object(github_search, "search_github_code", side_effect=make_page_receipt),
                  patch.object(github_search, "resolve_public_repository_commit", return_value="a" * 40),
                  patch.object(github_search, "download_github_snapshot", side_effect=KeyboardInterrupt),
                  redirect_stderr(StringIO())):
                self.assertEqual(github_search.search_github_catalog(arguments), 130)
            self.assertEqual(read_saved_json(arguments.output / "summary.json")["status"], "interrupted")
            record = read_saved_json(arguments.output / "snapshots.json")["repositories"][0]
            self.assertEqual(record["status"], "downloading")
            self.assertEqual(record["commit"], "a" * 40)
            with patch.object(github_search, "download_github_snapshot") as download, redirect_stderr(StringIO()):
                self.assertEqual(github_search.search_github_catalog(arguments), 4)
                download.assert_not_called()
            arguments.retry_incomplete = True
            with (patch.object(github_search, "download_github_snapshot", side_effect=write_github_fixture_archive),
                  patch.object(github_search, "resolve_public_repository_commit") as pin,
                  redirect_stderr(StringIO())):
                self.assertEqual(github_search.search_github_catalog(arguments), 0)
                pin.assert_not_called()

    def test_local_scan_interrupt_reuses_downloaded_archive(self) -> None:
        """Resume local work after interruption without another pin or download."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            arguments = make_github_arguments(catalog, root / "output")
            with (patch.object(github_search, "search_github_code", side_effect=make_page_receipt),
                  patch.object(github_search, "resolve_public_repository_commit", return_value="a" * 40),
                  patch.object(github_search, "download_github_snapshot", side_effect=write_github_fixture_archive),
                  patch.object(github_search, "scan_repository_archive", side_effect=KeyboardInterrupt),
                  redirect_stderr(StringIO())):
                self.assertEqual(github_search.search_github_catalog(arguments), 130)
            with patch.object(github_search, "download_github_snapshot") as download, redirect_stderr(StringIO()):
                self.assertEqual(github_search.search_github_catalog(arguments), 0)
                download.assert_not_called()

    def test_invalid_archive_is_failed_not_an_empty_success(self) -> None:
        """Retain the invalid archive and scan diagnosis in a failed run."""

        def write_invalid_archive(repository, commit, destination, maximum_bytes, ca_bundle):
            """Create invalid downloaded bytes for the corruption-handling test.

            Args:
                repository: Unused public repository identifier.
                commit: Unused pinned revision.
                destination: Test-owned download path.
                maximum_bytes: Unused compressed budget.
                ca_bundle: Unused certificate bundle.

            Returns:
                Number of invalid bytes saved.
            """

            destination.write_bytes(b"not a ZIP archive")
            return destination.stat().st_size

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            arguments = make_github_arguments(catalog, root / "output")
            with (patch.object(github_search, "search_github_code", side_effect=make_page_receipt),
                  patch.object(github_search, "resolve_public_repository_commit", return_value="a" * 40),
                  patch.object(github_search, "download_github_snapshot", side_effect=write_invalid_archive),
                  redirect_stderr(StringIO())):
                self.assertEqual(github_search.search_github_catalog(arguments), 1)
            record = read_saved_json(arguments.output / "snapshots.json")["repositories"][0]
            self.assertEqual(record["status"], "error")
            self.assertIn("invalid_archive", record["error"])
            self.assertEqual(read_saved_json(arguments.output / "summary.json")["status"], "failed")
            self.assertTrue(record["scan_sha256"])

    def test_download_budget_uses_decimal_megabytes(self) -> None:
        """Apply the documented exact byte conversion to downloader limits."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            arguments = make_github_arguments(catalog, root / "output", max_archive_mb=1)
            with (patch.object(github_search, "search_github_code", side_effect=make_page_receipt),
                  patch.object(github_search, "resolve_public_repository_commit", return_value="a" * 40),
                  patch.object(github_search, "download_github_snapshot", side_effect=write_github_fixture_archive) as download,
                  redirect_stderr(StringIO())):
                github_search.search_github_catalog(arguments)
            self.assertEqual(download.call_args.args[3], 1_000_000)

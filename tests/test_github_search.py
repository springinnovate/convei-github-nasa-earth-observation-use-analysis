"""Test search-page resume, repository ZIP downloads, and scans without network calls."""

import argparse
from contextlib import redirect_stderr
from io import BytesIO, StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from nasa_eo_search import github_search
from test_pipeline_catalog import make_pipeline_product, save_pipeline_catalog


def make_github_arguments(catalog: Path, output: Path, **overrides) -> argparse.Namespace:
    """Build search command arguments with small limits for offline tests.

    Args:
        catalog: Directory containing the test catalog.
        output: New or resumable search output directory.
        **overrides: Options that replace the defaults in this helper.

    Returns:
        Command arguments accepted by ``search_github_catalog``.
    """

    return argparse.Namespace(**{
        "catalog": catalog, "output": output, "short_name": ["ATL03"], "dry_run": False,
        "max_requests": 10, "ca_bundle": None, "github_pages": 2, "max_repositories": 2,
        "max_archive_mb": 1, "max_total_mb": 2, "include_grep_app": False,
        "retry_incomplete": False, **overrides,
    })


def make_github_page_response(term: str, max_pages: int = 1, *, start_page: int = 1) -> dict:
    """Build a GitHub response containing one public matching file and no next page.

    Args:
        term: Requested catalog short name.
        max_pages: Page count requested by the backend, expected to be one.
        start_page: One-based page number recorded in the response.

    Returns:
        Response dictionary with one ``example/project`` match and page counts.
    """

    repository = "github.com/example/project"
    return {"provider": "github", "term": term, "query": term, "status": "returned",
            "has_more": False, "start_page": start_page, "pages_returned": max_pages,
            "total_count": 1, "elapsed_seconds": 0.1, "warnings": ["Index scope is limited."], "raw": [],
            "results": [{"repository": repository, "file": "example.py",
                         "url": "https://github.com/example/project/blob/main/example.py"}]}


def write_github_fixture_zip(repository: str, commit: str, zip_destination_path: Path,
                                  maximum_download_bytes: int, ca_bundle: str | None = None) -> int:
    """Write a repository ZIP containing ATL03 and ATL06 instead of downloading it.

    Args:
        repository: Public repository, unused by this test helper.
        commit: Recorded commit identifier, unused by this test helper.
        zip_destination_path: ZIP file to create inside the test directory.
        maximum_download_bytes: Download limit, larger than this small test ZIP.
        ca_bundle: Optional certificate path, unused in this offline fixture.

    Returns:
        Number of compressed bytes written.
    """

    with zipfile.ZipFile(zip_destination_path, "w") as repository_zip:
        repository_zip.writestr("root/example.py", 'read("ATL03_007")\n# ATL06\n# MATL03 is not an ATL03 match inside that word\n')
    return zip_destination_path.stat().st_size


def read_saved_json(path: Path) -> dict:
    """Read a test output JSON object.

    Args:
        path: Saved settings, summary, request record, or repository progress file.

    Returns:
        Parsed JSON object.
    """

    return json.loads(path.read_text(encoding="utf-8"))


class GitHubSearchTests(unittest.TestCase):
    """Check request limits and reuse of saved search pages and repository ZIPs."""

    def test_dry_run_has_no_output_or_service_calls(self) -> None:
        """Check that previewing a search creates no files and makes no requests."""

        with TemporaryDirectory() as temporary, patch.object(github_search, "search_github_code") as search:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            arguments = make_github_arguments(catalog, root / "output", dry_run=True)
            with redirect_stderr(StringIO()):
                self.assertEqual(github_search.search_github_catalog(arguments), 0)
            self.assertFalse(arguments.output.exists())
            search.assert_not_called()

    def test_scan_all_catalog_terms_and_resume_without_calls(self) -> None:
        """Check that scans include unqueried products and resume reuses saved files."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product(), make_pipeline_product("ATL06")])
            arguments = make_github_arguments(catalog, root / "output")
            with (patch.object(github_search, "search_github_code", side_effect=make_github_page_response) as search,
                  patch.object(github_search, "resolve_public_repository_commit", return_value="a" * 40) as pin,
                  patch.object(github_search, "download_github_repository_zip", side_effect=write_github_fixture_zip) as download,
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
        """Check that resuming a one-request run fetches page two without page one."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            arguments = make_github_arguments(catalog, root / "output", max_requests=1)
            first = {**make_github_page_response("ATL03"), "status": "capped", "has_more": True, "total_count": 150}
            second = {**make_github_page_response("ATL03", start_page=2), "total_count": 150}
            with (patch.object(github_search, "search_github_code", side_effect=[first, second]) as search,
                  patch.object(github_search, "resolve_public_repository_commit", return_value="a" * 40),
                  patch.object(github_search, "download_github_repository_zip", side_effect=write_github_fixture_zip) as download,
                  redirect_stderr(StringIO())):
                self.assertEqual(github_search.search_github_catalog(arguments), 4)
                self.assertEqual(github_search.search_github_catalog(arguments), 0)
                self.assertEqual([call.kwargs["start_page"] for call in search.call_args_list], [1, 2])
                self.assertEqual(download.call_count, 1)
            receipts = list((arguments.output / "discovery").glob("*.json"))
            self.assertEqual(len(receipts), 2)

    def test_failed_request_requires_explicit_retry_and_preserves_attempt(self) -> None:
        """Check that retry needs an explicit flag and retains the failed attempt."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            arguments = make_github_arguments(catalog, root / "output")
            failure = {**make_github_page_response("ATL03"), "status": "error", "results": []}
            with (patch.object(github_search, "search_github_code", return_value=failure) as search,
                  redirect_stderr(StringIO())):
                self.assertEqual(github_search.search_github_catalog(arguments), 1)
                self.assertEqual(github_search.search_github_catalog(arguments), 1)
                self.assertEqual(search.call_count, 1)
            arguments.retry_incomplete = True
            recovered = {**make_github_page_response("ATL03"), "results": [], "total_count": 0}
            with patch.object(github_search, "search_github_code", return_value=recovered), redirect_stderr(StringIO()):
                self.assertEqual(github_search.search_github_catalog(arguments), 0)
            receipts = [read_saved_json(path) for path in (arguments.output / "discovery").glob("*.json")]
            self.assertEqual({row["attempt"] for row in receipts}, {1, 2})
            self.assertEqual({row["status"] for row in receipts}, {"error", "returned"})

    def test_discovery_is_round_robin_and_repository_selection_has_no_controls(self) -> None:
        """Check that each product gets a page and a repository before later choices."""

        plan = {"discovery_terms": ["ATL03", "ATL06"], "include_grep_app": False,
                "github_pages": 2, "max_repositories": 2}
        first = {**make_github_page_response("ATL03"), "page": 1, "attempt": 1,
                 "results": [{"repository": f"github.com/first/repo{index}", "file": "x", "url": "x"} for index in range(3)]}
        second = {**make_github_page_response("ATL06"), "page": 1, "attempt": 1,
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
        """Check that changed ZIPs or saved responses stop resume before any request."""

        for damage in ("archive", "receipt"):
            with self.subTest(damage=damage), TemporaryDirectory() as temporary:
                root = Path(temporary)
                catalog = save_pipeline_catalog(root, [make_pipeline_product()])
                arguments = make_github_arguments(catalog, root / "output")
                with (patch.object(github_search, "search_github_code", side_effect=make_github_page_response),
                      patch.object(github_search, "resolve_public_repository_commit", return_value="a" * 40),
                      patch.object(github_search, "download_github_repository_zip", side_effect=write_github_fixture_zip),
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
        """Check that resume retains omitted options and rejects changed dataset names."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product(), make_pipeline_product("ATL06")])
            arguments = make_github_arguments(catalog, root / "output")
            empty = {**make_github_page_response("ATL03"), "results": [], "total_count": 0}
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
        """Check that an interrupted page request is saved and not retried by default."""

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
        """Check that an interrupted ZIP download retains its commit and needs retry."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            arguments = make_github_arguments(catalog, root / "output")
            with (patch.object(github_search, "search_github_code", side_effect=make_github_page_response),
                  patch.object(github_search, "resolve_public_repository_commit", return_value="a" * 40),
                  patch.object(github_search, "download_github_repository_zip", side_effect=KeyboardInterrupt),
                  redirect_stderr(StringIO())):
                self.assertEqual(github_search.search_github_catalog(arguments), 130)
            self.assertEqual(read_saved_json(arguments.output / "summary.json")["status"], "interrupted")
            record = read_saved_json(arguments.output / "snapshots.json")["repositories"][0]
            self.assertEqual(record["status"], "downloading")
            self.assertEqual(record["commit"], "a" * 40)
            with patch.object(github_search, "download_github_repository_zip") as download, redirect_stderr(StringIO()):
                self.assertEqual(github_search.search_github_catalog(arguments), 4)
                download.assert_not_called()
            arguments.retry_incomplete = True
            with (patch.object(github_search, "download_github_repository_zip", side_effect=write_github_fixture_zip),
                  patch.object(github_search, "resolve_public_repository_commit") as pin,
                  redirect_stderr(StringIO())):
                self.assertEqual(github_search.search_github_catalog(arguments), 0)
                pin.assert_not_called()

    def test_local_scan_interrupt_reuses_downloaded_archive(self) -> None:
        """Check that restarting an interrupted scan reuses the downloaded ZIP."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            arguments = make_github_arguments(catalog, root / "output")
            with (patch.object(github_search, "search_github_code", side_effect=make_github_page_response),
                  patch.object(github_search, "resolve_public_repository_commit", return_value="a" * 40),
                  patch.object(github_search, "download_github_repository_zip", side_effect=write_github_fixture_zip),
                  patch.object(github_search, "scan_repository_zip", side_effect=KeyboardInterrupt),
                  redirect_stderr(StringIO())):
                self.assertEqual(github_search.search_github_catalog(arguments), 130)
            with patch.object(github_search, "download_github_repository_zip") as download, redirect_stderr(StringIO()):
                self.assertEqual(github_search.search_github_catalog(arguments), 0)
                download.assert_not_called()

    def test_invalid_archive_is_failed_not_an_empty_success(self) -> None:
        """Check that invalid ZIP bytes produce a saved error, not a zero-match success."""

        def write_invalid_repository_zip(repository, commit, zip_destination_path, maximum_download_bytes, ca_bundle):
            """Write non-ZIP bytes where the downloader would save a repository ZIP.

            Args:
                repository: Unused public repository identifier.
                commit: Unused pinned revision.
                zip_destination_path: ZIP download path inside the test directory.
                maximum_download_bytes: Unused download-size limit.
                ca_bundle: Unused certificate bundle.

            Returns:
                Number of invalid bytes saved.
            """

            zip_destination_path.write_bytes(b"not a ZIP archive")
            return zip_destination_path.stat().st_size

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            arguments = make_github_arguments(catalog, root / "output")
            with (patch.object(github_search, "search_github_code", side_effect=make_github_page_response),
                  patch.object(github_search, "resolve_public_repository_commit", return_value="a" * 40),
                  patch.object(github_search, "download_github_repository_zip", side_effect=write_invalid_repository_zip),
                  redirect_stderr(StringIO())):
                self.assertEqual(github_search.search_github_catalog(arguments), 1)
            record = read_saved_json(arguments.output / "snapshots.json")["repositories"][0]
            self.assertEqual(record["status"], "error")
            self.assertIn("invalid_archive", record["error"])
            self.assertEqual(read_saved_json(arguments.output / "summary.json")["status"], "failed")
            self.assertTrue(record["scan_sha256"])

    def test_download_budget_uses_decimal_megabytes(self) -> None:
        """Check that a one-megabyte ZIP limit is passed as exactly 1,000,000 bytes."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            arguments = make_github_arguments(catalog, root / "output", max_archive_mb=1)
            with (patch.object(github_search, "search_github_code", side_effect=make_github_page_response),
                  patch.object(github_search, "resolve_public_repository_commit", return_value="a" * 40),
                  patch.object(github_search, "download_github_repository_zip", side_effect=write_github_fixture_zip) as download,
                  redirect_stderr(StringIO())):
                github_search.search_github_catalog(arguments)
            self.assertEqual(download.call_args.args[3], 1_000_000)

    def test_zip_download_retains_partial_bytes_after_size_limit(self) -> None:
        """Check that an oversized response keeps partial bytes but creates no ZIP."""

        response = MagicMock()
        response.headers = {}
        response.read.side_effect = BytesIO(b"123456789").read
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with TemporaryDirectory() as temporary, patch.object(github_search, "build_opener", return_value=opener):
            zip_destination_path = Path(temporary) / "repository.zip"
            with self.assertRaisesRegex(ValueError, "compressed download budget"):
                github_search.download_github_repository_zip(
                    "github.com/example/project", "a" * 40, zip_destination_path, 5,
                )
            self.assertFalse(zip_destination_path.exists())
            self.assertEqual(zip_destination_path.with_suffix(".partial").read_bytes(), b"123456")
            self.assertEqual(
                opener.open.call_args.args[0].full_url,
                "https://codeload.github.com/example/project/zip/" + "a" * 40,
            )

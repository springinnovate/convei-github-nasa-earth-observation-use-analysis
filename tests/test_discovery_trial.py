"""Exercise the bounded comparison workflow without external services."""

from contextlib import redirect_stderr, redirect_stdout
import csv
from io import BytesIO, StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from nasa_eo_search import discovery_trial
from test_pipeline_catalog import make_pipeline_product, save_pipeline_catalog


def make_trial_receipt(term: str, provider: str = "github", repository: str = "github.com/example/project") -> dict:
    """Build a one-file provider receipt with stable candidate provenance.

    Args:
        term: Product short name searched.
        provider: Search engine name.
        repository: Public candidate repository name.

    Returns:
        A bounded receipt accepted by the trial orchestrator.
    """
    return {"provider": provider, "term": term, "query": term, "status": "limited",
            "elapsed_seconds": 1.0, "warnings": ["Sample only."], "raw": {},
            "results": [{"repository": repository, "file": "example.py",
                         "url": f"https://{repository}/blob/main/example.py"}]}


def write_trial_archive(repository: str, commit: str, destination: Path, maximum_bytes: int) -> int:
    """Write a synthetic fixed snapshot instead of making a network request.

    Args:
        repository: Public name, unused by this fixture.
        commit: Commit identifier, unused by this fixture.
        destination: Test-owned ZIP path.
        maximum_bytes: Download budget, unused because the fixture is tiny.

    Returns:
        Actual fixture ZIP size.
    """
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr("root/example.py", 'load("ATL03_007")\n# MATL03 must not match\n# ATL06\n')
        archive.writestr("root/image.bin", b"\x00binary")
    return destination.stat().st_size


def read_trial_rows(path: Path) -> list[dict]:
    """Read spreadsheet-protected reports for assertions.

    Args:
        path: CSV output from the trial.

    Returns:
        Rows with string values.
    """
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


class DiscoveryTrialTests(unittest.TestCase):
    """Keep discovery, controls, local matching, and execution status separate."""

    def test_dry_run_validates_without_authentication_or_writes(self) -> None:
        """Allow a full-catalog local preview without contacting any service."""
        with TemporaryDirectory() as temporary, patch.object(discovery_trial, "search_github_code") as search:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product(), make_pipeline_product("ATL06")])
            with redirect_stdout(StringIO()):
                result = discovery_trial.main([str(catalog), "--short-name", "ATL03", "--output", str(root / "trial"), "--dry-run"])
            self.assertEqual(result, 0)
            self.assertFalse((root / "trial").exists())
            search.assert_not_called()

    def test_existing_output_and_unknown_product_preserve_user_files(self) -> None:
        """Refuse unsafe inputs before remote work or replacement of saved results."""
        with TemporaryDirectory() as temporary, patch.object(discovery_trial, "search_github_code") as search:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            for term, output in (("missing", root / "new"), ("ATL03", catalog)):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    result = discovery_trial.main([str(catalog), "--short-name", term, "--output", str(output)])
                self.assertEqual(result, 1)
            self.assertTrue((catalog / "products.jsonl").exists())
            self.assertFalse((root / "new").exists())
            search.assert_not_called()

    def test_controls_are_not_credited_as_discoveries_and_names_are_deduplicated(self) -> None:
        """Add known controls independently and enforce a fair repository cap."""
        receipts = [make_trial_receipt("ATL03"),
                    make_trial_receipt("ATL03", "grep.app", "github.com/EXAMPLE/PROJECT"),
                    make_trial_receipt("ATL03", "sourcegraph", "github.com/another/project")]
        rows = discovery_trial.choose_trial_repositories(receipts, ["ATL03"], 2)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["repository"], "github.com/pydap/pydap")
        self.assertEqual(rows[0]["discovered_by"], [])
        self.assertEqual(len(rows[1]["discovered_by"]), 2)
        self.assertEqual(rows[2]["status"], "not_selected_repository_cap")

    def test_end_to_end_scans_all_catalog_terms_without_claiming_global_coverage(self) -> None:
        """Produce one row per dataset/file with pinned links and explicit gaps."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product(), make_pipeline_product("ATL06")])
            output = root / "trial"
            with (patch.object(discovery_trial, "search_github_code", side_effect=make_trial_receipt),
                  patch.object(discovery_trial, "resolve_public_repository_commit", return_value="a" * 40),
                  patch.object(discovery_trial, "download_public_archive", side_effect=write_trial_archive),
                  redirect_stdout(StringIO())):
                result = discovery_trial.main([str(catalog), "--short-name", "ATL03", "--providers", "github",
                                               "--max-repositories", "1", "--output", str(output)])
            self.assertEqual(result, 0)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["status"], "finished_bounded_trial")
            self.assertEqual(summary["local_product_count"], 2)
            self.assertEqual(summary["repositories_scanned"], 1)
            rows = read_trial_rows(output / "matches.csv")
            self.assertEqual({row["short_name"] for row in rows}, {"ATL03", "ATL06"})
            atl03 = next(row for row in rows if row["short_name"] == "ATL03")
            self.assertEqual(atl03["line_numbers"], "1")
            self.assertEqual(atl03["search_terms"], "ATL03; ATL03_007")
            self.assertEqual(atl03["search_complete"], "false")
            self.assertEqual(atl03["snapshot_scan_status"], "incomplete")
            self.assertIn("a" * 40, atl03["source_url"])
            comparison = read_trial_rows(output / "comparison.csv")[0]
            self.assertEqual(comparison["controls_discovered"], "")
            self.assertEqual(comparison["controls_missing"], "github.com/pydap/pydap")
            self.assertEqual(read_trial_rows(output / "skipped-files.csv")[0]["reason"], "binary_file")
            receipt = json.loads((output / "snapshots/repository-001.json").read_text())
            self.assertEqual(len(receipt["archive_sha256"]), 64)

    def test_selected_only_does_not_expand_local_scan_to_other_products(self) -> None:
        """Retain a smaller optional scan scope in the saved summary."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product(), make_pipeline_product("ATL06")])
            with (patch.object(discovery_trial, "search_github_code", side_effect=make_trial_receipt),
                  patch.object(discovery_trial, "resolve_public_repository_commit", return_value="a" * 40),
                  patch.object(discovery_trial, "download_public_archive", side_effect=write_trial_archive),
                  redirect_stdout(StringIO())):
                result = discovery_trial.main([str(catalog), "--short-name", "ATL03", "--selected-only", "--providers", "github",
                                               "--max-repositories", "1", "--output", str(root / "trial")])
            self.assertEqual(result, 0)
            self.assertEqual({row["short_name"] for row in read_trial_rows(root / "trial/matches.csv")}, {"ATL03"})

    def test_repository_failure_and_provider_failure_are_not_empty_success(self) -> None:
        """Keep independent discovery evidence when a public snapshot is unavailable."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            receipt = {**make_trial_receipt("ATL03"), "status": "error", "results": []}
            with (patch.object(discovery_trial, "search_github_code", return_value=receipt),
                  patch.object(discovery_trial, "resolve_public_repository_commit", side_effect=RuntimeError("not public")),
                  patch.object(discovery_trial, "download_public_archive") as download,
                  redirect_stdout(StringIO()), redirect_stderr(StringIO())):
                result = discovery_trial.main([str(catalog), "--short-name", "ATL03", "--providers", "github", "--output", str(root / "trial")])
            self.assertEqual(result, 1)
            download.assert_not_called()
            self.assertEqual(json.loads((root / "trial/summary.json").read_text())["status"], "finished_with_errors")
            self.assertEqual(read_trial_rows(root / "trial/comparison.csv")[0]["status"], "error")

    def test_interrupt_saves_an_unfinished_summary(self) -> None:
        """Preserve a report rather than presenting an interrupted trial as finished."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            with patch.object(discovery_trial, "search_github_code", side_effect=KeyboardInterrupt), redirect_stdout(StringIO()):
                result = discovery_trial.main([str(catalog), "--short-name", "ATL03", "--providers", "github", "--output", str(root / "trial")])
            self.assertEqual(result, 130)
            self.assertEqual(json.loads((root / "trial/summary.json").read_text())["status"], "interrupted")

    def test_download_is_credential_free_immutable_and_size_bounded(self) -> None:
        """Reject oversized archives and retain only a bounded partial response."""
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.read.side_effect = BytesIO(b"1234567890").read
        opener = MagicMock()
        opener.open.return_value = response
        with TemporaryDirectory() as temporary, patch.object(discovery_trial, "build_opener", return_value=opener):
            destination = Path(temporary) / "archive.zip"
            with self.assertRaisesRegex(ValueError, "budget"):
                discovery_trial.download_public_archive("github.com/example/project", "a" * 40, destination, 5)
            self.assertFalse(destination.exists())
            self.assertEqual(destination.with_suffix(".partial").stat().st_size, 6)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://codeload.github.com/example/project/zip/" + "a" * 40)
            self.assertNotIn("Authorization", request.headers)

    def test_invalid_archive_is_operational_failure_not_successful_empty_scan(self) -> None:
        """Distinguish damaged downloads from intentional binary/large-file exclusions."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            output = root / "trial"
            with (patch.object(discovery_trial, "search_github_code", side_effect=make_trial_receipt),
                  patch.object(discovery_trial, "resolve_public_repository_commit", return_value="a" * 40),
                  patch.object(discovery_trial, "download_public_archive", side_effect=write_trial_archive),
                  patch.object(discovery_trial, "scan_repository_archive", return_value={
                      "status": "incomplete", "matches": [], "files_scanned": 0, "bytes_scanned": 0,
                      "skipped": [{"file": "", "reason": "invalid_archive"}]}),
                  redirect_stdout(StringIO())):
                result = discovery_trial.main([str(catalog), "--short-name", "ATL03", "--providers", "github",
                                               "--max-repositories", "1", "--output", str(output)])
            self.assertEqual(result, 1)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["status"], "finished_with_errors")
            self.assertEqual(summary["repositories_scanned"], 0)
            self.assertEqual(read_trial_rows(output / "repositories.csv")[0]["status"], "error")

    def test_partial_download_consumes_the_next_repository_budget(self) -> None:
        """Count retained partial bytes, but not JSON reports, against downloads."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = save_pipeline_catalog(root, [make_pipeline_product()])
            output = root / "trial"

            def fail_partial_then_write(repository: str, commit: str, destination: Path, maximum_bytes: int) -> int:
                """Fail the control download after retaining a known byte count.

                Args:
                    repository: Candidate repository used to select fixture behavior.
                    commit: Immutable revision passed to the successful fixture.
                    destination: Test-owned archive path.
                    maximum_bytes: Remaining download allowance to verify.

                Returns:
                    Size of the second successfully generated archive.

                Raises:
                    ValueError: For the first synthetic interrupted download.
                """
                if repository == "github.com/pydap/pydap":
                    destination.with_suffix(".partial").write_bytes(b"x" * 100)
                    raise ValueError("bounded partial download")
                self.assertEqual(maximum_bytes, 1_000_000 - 100)
                return write_trial_archive(repository, commit, destination, maximum_bytes)

            with (patch.object(discovery_trial, "search_github_code", side_effect=make_trial_receipt),
                  patch.object(discovery_trial, "resolve_public_repository_commit", return_value="a" * 40),
                  patch.object(discovery_trial, "download_public_archive", side_effect=fail_partial_then_write),
                  redirect_stdout(StringIO()), redirect_stderr(StringIO())):
                result = discovery_trial.main([str(catalog), "--short-name", "ATL03", "--providers", "github",
                                               "--max-total-mb", "1", "--output", str(output)])
            self.assertEqual(result, 1)
            self.assertEqual(read_trial_rows(output / "repositories.csv")[0]["archive_bytes"], "100")


if __name__ == "__main__":
    unittest.main()

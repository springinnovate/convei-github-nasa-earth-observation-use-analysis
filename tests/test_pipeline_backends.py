"""Keep backend selection explicit for new runs and stable for saved searches."""

from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.pipeline import main


class PipelineBackendTests(unittest.TestCase):
    """Route existing commands without reinterpreting legacy output directories."""

    def test_new_search_defaults_to_github_and_keeps_resume_options_unspecified(self) -> None:
        """Pass new runs to GitHub while letting its frozen-plan defaults apply."""
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "new-search"
            with patch("nasa_eo_search.github_search.search_github_catalog", return_value=0) as search:
                result = main(["search", "catalog", "--output", str(output), "--dry-run"])
            self.assertEqual(result, 0)
            arguments = search.call_args.args[0]
            self.assertIsNone(arguments.github_pages)
            self.assertIsNone(arguments.include_grep_app)
            self.assertIsNone(arguments.max_repositories)
            self.assertTrue(arguments.dry_run)
            self.assertFalse(output.exists())

    def test_existing_sourcegraph_plan_auto_selects_legacy_backend(self) -> None:
        """Preserve earlier output formats when no backend is supplied."""
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "plan.json").write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
            with patch("nasa_eo_search.pipeline.search_sourcegraph_catalog", return_value=4) as search:
                result = main(["search", "catalog", "--output", str(output)])
            self.assertEqual(result, 4)
            search.assert_called_once()

    def test_backend_changes_and_unknown_saved_backends_fail_without_writes(self) -> None:
        """Avoid silently migrating or overwriting another backend's evidence."""
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            plan_path = output / "plan.json"
            for backend in ("sourcegraph", "unknown"):
                original = json.dumps({"backend": backend})
                plan_path.write_text(original, encoding="utf-8")
                with redirect_stderr(StringIO()):
                    result = main(["search", "catalog", "--backend", "github", "--output", str(output)])
                self.assertEqual(result, 1)
                self.assertEqual(plan_path.read_text(encoding="utf-8"), original)

    def test_cross_backend_options_fail_before_requests(self) -> None:
        """Reject unsupported host filters and irrelevant download settings."""
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "new-search"
            for options in (("--all-hosts",), ("--search-timeout", "20"),
                            ("--backend", "sourcegraph", "--github-pages", "2")):
                with redirect_stderr(StringIO()):
                    result = main(["search", "catalog", "--output", str(output), *options])
                self.assertEqual(result, 1)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

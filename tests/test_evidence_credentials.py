"""Verify key-file lookup, secret-safe errors, and credential-free previews."""

from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.evidence_classify import main
from nasa_eo_search.evidence_credentials import MAX_API_KEY_BYTES, load_openai_api_key
from test_evidence_classify import make_model_response, prepare_test_evidence


class EvidenceCredentialTests(unittest.TestCase):
    """Exercise credential sources exclusively with fake keys in temporary files."""

    def setUp(self) -> None:
        """Isolate the environment and default path from real user credentials."""
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.key_path = Path(self.temporary_directory.name) / "openai.key"
        environment_patch = patch.dict(os.environ, {"OPENAI_API_KEY": ""})
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        default_path_patch = patch("nasa_eo_search.evidence_credentials.DEFAULT_OPENAI_KEY_FILE", self.key_path)
        default_path_patch.start()
        self.addCleanup(default_path_patch.stop)

    def test_explicit_file_overrides_environment_and_accepts_windows_text(self) -> None:
        """Read a UTF-8 byte-order mark and trailing Windows newline safely."""
        self.key_path.write_bytes(b"\xef\xbb\xbf  fake-file-key\r\n")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-environment-key"}):
            self.assertEqual(load_openai_api_key(self.key_path), "fake-file-key")

    def test_environment_precedes_default_file_without_reading_it(self) -> None:
        """Preserve existing environment-based usage when no file is explicit."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": " fake-environment-key \n"}), patch.object(Path, "open") as key_input:
            self.assertEqual(load_openai_api_key(), "fake-environment-key")
            key_input.assert_not_called()

    def test_default_file_is_relative_to_working_directory(self) -> None:
        """Find secrets/openai.key from the launch directory without an environment key."""
        original_directory = Path.cwd()
        root = Path(self.temporary_directory.name)
        (root / "secrets").mkdir()
        (root / "secrets" / "openai.key").write_text("fake-default-key\n", encoding="utf-8")
        try:
            os.chdir(root)
            with patch("nasa_eo_search.evidence_credentials.DEFAULT_OPENAI_KEY_FILE", Path("secrets/openai.key")):
                self.assertEqual(load_openai_api_key(), "fake-default-key")
        finally:
            os.chdir(original_directory)

    def test_explicit_missing_file_never_falls_back_to_environment(self) -> None:
        """Fail visibly on a misspelled explicit path instead of using another key."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-environment-key"}), self.assertRaises(ValueError) as caught:
            load_openai_api_key(self.key_path)
        self.assertIn("Cannot read", str(caught.exception))
        self.assertNotIn("fake-environment-key", str(caught.exception))

    def test_invalid_file_contents_do_not_appear_in_errors(self) -> None:
        """Reject blank, multiline, non-ASCII, control, and oversized input privately."""
        invalid_contents = (
            b"", b" \r\n\t", b"fake-private-key\nsecond-key", b"fake-private-key\x00",
            b"fake-private-key\x1b", b"fake-private-key\x7f", b"fake-private-key\xff",
            "fake-private-key\u2603".encode("utf-8"), b"k" * (MAX_API_KEY_BYTES + 1),
        )
        for contents in invalid_contents:
            with self.subTest(length=len(contents)):
                self.key_path.write_bytes(contents)
                with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-environment-key"}), self.assertRaises(ValueError) as caught:
                    load_openai_api_key(self.key_path)
                self.assertNotIn("fake-private-key", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__ or caught.exception.__context__ is None)

    def test_unreadable_and_directory_paths_report_safe_errors(self) -> None:
        """Avoid printing filesystem exception details that might contain secrets."""
        with self.assertRaises(ValueError):
            load_openai_api_key(Path(self.temporary_directory.name))
        self.key_path.write_text("fake-private-key", encoding="utf-8")
        with patch.object(Path, "open", side_effect=PermissionError("fake-private-key")), self.assertRaises(ValueError) as caught:
            load_openai_api_key(self.key_path)
        self.assertNotIn("fake-private-key", str(caught.exception))

    def test_invalid_environment_value_does_not_fall_back_or_leak(self) -> None:
        """Reject header-injection characters in environment credentials too."""
        self.key_path.write_text("valid-default-key", encoding="utf-8")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-private-key\nInjected: header"}), self.assertRaises(ValueError) as caught:
            load_openai_api_key()
        self.assertNotIn("fake-private-key", str(caught.exception))

    def test_key_file_is_used_without_persisting_key_or_path(self) -> None:
        """Pass only the loaded key to the adapter and leave reports credential-free."""
        root = Path(self.temporary_directory.name)
        output = prepare_test_evidence(root)
        self.key_path.write_text("fake-private-key\n", encoding="utf-8")
        diagnostics = StringIO()
        with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response) as request, redirect_stderr(diagnostics):
            self.assertEqual(main(["run", str(output), "--model", "test-model", "--api-key-file", str(self.key_path)]), 0)
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(call.args[1] == "fake-private-key" for call in request.call_args_list))
        self.assertNotIn("fake-private-key", diagnostics.getvalue())
        for saved_file in output.rglob("*"):
            if saved_file.is_file():
                saved_text = saved_file.read_text(encoding="utf-8-sig")
                self.assertNotIn("fake-private-key", saved_text)
                self.assertNotIn("openai.key", saved_text)

    def test_missing_explicit_key_fails_before_network_or_receipts(self) -> None:
        """Validate a selected file before creating request receipts or model settings."""
        output = prepare_test_evidence(Path(self.temporary_directory.name))
        with patch("nasa_eo_search.evidence_classify.request_evidence_classification") as request, redirect_stderr(StringIO()):
            self.assertEqual(main(["run", str(output), "--model", "test-model", "--api-key-file", str(self.key_path)]), 1)
            request.assert_not_called()
        self.assertFalse((output / "requests").exists())
        self.assertFalse((output / "settings.json").exists())

    def test_dry_run_does_not_read_an_explicit_key_file(self) -> None:
        """Preview successfully even when the explicitly supplied key file is absent."""
        output = prepare_test_evidence(Path(self.temporary_directory.name))
        with patch("nasa_eo_search.evidence_classify.load_openai_api_key") as key_loader, patch("nasa_eo_search.evidence_classify.request_evidence_classification") as request, redirect_stderr(StringIO()):
            self.assertEqual(main(["run", str(output), "--model", "test-model", "--api-key-file", str(self.key_path), "--dry-run"]), 0)
            key_loader.assert_not_called()
            request.assert_not_called()

    def test_completed_resume_does_not_require_a_key_file(self) -> None:
        """Reuse cached judgments after a key has been removed or moved."""
        output = prepare_test_evidence(Path(self.temporary_directory.name))
        self.key_path.write_text("fake-private-key", encoding="utf-8")
        arguments = ["run", str(output), "--model", "test-model", "--api-key-file", str(self.key_path)]
        with patch("nasa_eo_search.evidence_classify.request_evidence_classification", side_effect=make_model_response), redirect_stderr(StringIO()):
            self.assertEqual(main(arguments), 0)
        self.key_path.unlink()
        with patch("nasa_eo_search.evidence_classify.load_openai_api_key") as key_loader, patch("nasa_eo_search.evidence_classify.request_evidence_classification") as request, redirect_stderr(StringIO()):
            self.assertEqual(main(arguments), 0)
            key_loader.assert_not_called()
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()

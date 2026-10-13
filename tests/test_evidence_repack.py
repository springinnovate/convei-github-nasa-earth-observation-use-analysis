"""Check immutable, credential-free migration of saved classification evidence."""

from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.evidence_classify import main
from nasa_eo_search.evidence_inputs import identify_evidence, read_evidence_json
from nasa_eo_search.evidence_model import PROMPT_VERSION
from test_evidence_classify import prepare_test_evidence


class EvidenceRepackTests(unittest.TestCase):
    """Preserve original evidence and paid receipts while rebuilding model inputs."""

    def test_repack_saved_v1_uses_no_network_or_credentials_and_preserves_source(self) -> None:
        """Convert old raw evidence without editing or reusing paid response files."""
        with TemporaryDirectory() as temporary_directory:
            root_directory = Path(temporary_directory)
            source_directory = prepare_test_evidence(root_directory)
            source_envelope = read_evidence_json(source_directory / "plan.json")
            source_envelope["schema_version"] = 1
            source_envelope["data"]["prompt_version"] = "repository-product-v1"
            for evidence_group in source_envelope["data"]["groups"]:
                evidence_group.pop("evidence_blocks", None)
                evidence_group["packets"] = [{
                    "repository": evidence_group["repository"],
                    "product": evidence_group["product"],
                    "evidence": evidence_group["evidence"],
                }]
            source_envelope["plan_id"] = identify_evidence(source_envelope["data"])
            (source_directory / "plan.json").write_text(json.dumps(source_envelope), encoding="utf-8")
            (source_directory / "settings.json").write_text('{"model":"old-model"}', encoding="utf-8")
            receipt_directory = source_directory / "requests" / "old-response"
            receipt_directory.mkdir(parents=True)
            (receipt_directory / "attempt-000001.json").write_text('{"status":"complete"}', encoding="utf-8")
            original_files = {
                str(source_file.relative_to(source_directory)): source_file.read_bytes()
                for source_file in source_directory.rglob("*") if source_file.is_file()
            }
            output_directory = root_directory / "repacked"
            with patch("nasa_eo_search.evidence_classify.load_openai_api_key") as key_loader, \
                    patch("nasa_eo_search.evidence_context.read_bounded_https") as network, \
                    patch("nasa_eo_search.evidence_classify.request_evidence_classification") as classify, \
                    redirect_stderr(StringIO()):
                self.assertEqual(main(["repack", str(source_directory), "--output", str(output_directory)]), 0)
                key_loader.assert_not_called()
                network.assert_not_called()
                classify.assert_not_called()
            self.assertEqual(original_files, {
                str(source_file.relative_to(source_directory)): source_file.read_bytes()
                for source_file in source_directory.rglob("*") if source_file.is_file()
            })
            repacked_envelope = read_evidence_json(output_directory / "plan.json")
            self.assertEqual(repacked_envelope["schema_version"], 2)
            self.assertEqual(repacked_envelope["data"]["prompt_version"], PROMPT_VERSION)
            self.assertEqual(repacked_envelope["data"]["repacked_from"]["plan_id"], source_envelope["plan_id"])
            self.assertEqual(repacked_envelope["data"]["groups"][0]["evidence"], source_envelope["data"]["groups"][0]["evidence"])
            self.assertFalse((output_directory / "settings.json").exists())
            self.assertFalse((output_directory / "requests").exists())
            self.assertTrue((output_directory / "audit.json").is_file())

    def test_repack_rejects_changed_hash_and_existing_destinations(self) -> None:
        """Fail before replacing source files or creating output for damaged inputs."""
        with TemporaryDirectory() as temporary_directory:
            root_directory = Path(temporary_directory)
            source_directory = prepare_test_evidence(root_directory)
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["repack", str(source_directory), "--output", str(source_directory)]), 1)
            source_envelope = read_evidence_json(source_directory / "plan.json")
            source_envelope["data"]["groups"][0]["repository"] = "github.com/changed/repository"
            (source_directory / "plan.json").write_text(json.dumps(source_envelope), encoding="utf-8")
            output_directory = root_directory / "repacked"
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["repack", str(source_directory), "--output", str(output_directory)]), 1)
            self.assertFalse(output_directory.exists())

    def test_v2_repack_is_deterministic_and_dry_run_remains_free(self) -> None:
        """Preserve packet content when no source evidence or bounds have changed."""
        with TemporaryDirectory() as temporary_directory:
            root_directory = Path(temporary_directory)
            source_directory = prepare_test_evidence(root_directory)
            output_directory = root_directory / "repacked"
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["repack", str(source_directory), "--output", str(output_directory)]), 0)
            original_groups = read_evidence_json(source_directory / "plan.json")["data"]["groups"]
            repacked_groups = read_evidence_json(output_directory / "plan.json")["data"]["groups"]
            self.assertEqual([group["packets"] for group in original_groups], [group["packets"] for group in repacked_groups])
            self.assertEqual([group["evidence_blocks"] for group in original_groups], [group["evidence_blocks"] for group in repacked_groups])
            with patch("nasa_eo_search.evidence_classify.load_openai_api_key") as key_loader, \
                    patch("nasa_eo_search.evidence_classify.request_evidence_classification") as classify, \
                    redirect_stderr(StringIO()):
                self.assertEqual(main(["run", str(output_directory), "--model", "test-model", "--dry-run"]), 0)
                key_loader.assert_not_called()
                classify.assert_not_called()


if __name__ == "__main__":
    unittest.main()

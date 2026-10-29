"""Preserve historical requests while new evidence uses the clarified taxonomy."""

from contextlib import redirect_stderr
import csv
import hashlib
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.evidence_classify import main, save_prepared_classification_plan
from nasa_eo_search.evidence_inputs import (
    identify_evidence, read_evidence_json, read_product_evidence_groups, serialize_evidence,
)
from nasa_eo_search.evidence_model import (
    CLASSIFICATION_PROMPT, PROMPT_VERSION, build_classification_request, select_classification_prompt,
)
from nasa_eo_search.evidence_reports import load_evidence_plan, save_evidence_json, write_evidence_reports
from nasa_eo_search.file_classify import build_file_classification_plan
from test_evidence_classify import create_saved_evidence_inputs, make_model_response
from test_evidence_reports_v2 import create_report_plan


def save_legacy_classification_run(
    output_directory: Path, prompt_version: str = "repository-product-v2",
) -> dict:
    """Create a historical run with raw paid-response receipts for resume tests.

    Args:
        output_directory: New destination inside the test's temporary directory.
        prompt_version: Supported historical instructions to freeze in the plan.

    Returns:
        Frozen historical plan whose responses cite its original source blocks.
    """
    plan = create_report_plan(packet_count=1)
    plan["prompt_version"] = prompt_version
    with redirect_stderr(StringIO()):
        save_prepared_classification_plan(output_directory, plan)
    settings = {"model": "test-model", "max_output_tokens": 2048, "prompt_version": plan["prompt_version"]}
    save_evidence_json(output_directory / "settings.json", settings)
    for group in plan["groups"]:
        for packet in group["packets"]:
            request_body = build_classification_request(packet, "test-model", 2048, plan["prompt_version"])
            request_id = identify_evidence(request_body)
            receipt_directory = output_directory / "requests" / request_id
            receipt_directory.mkdir(parents=True)
            save_evidence_json(receipt_directory / "attempt-000001.json", {
                "request_id": request_id, "prompt_version": plan["prompt_version"],
                "request": request_body, "response": make_model_response(request_body), "status": "complete",
            })
    write_evidence_reports(output_directory, plan, settings)
    return plan


class ClassificationPromptVersionTests(unittest.TestCase):
    """Keep saved instructions and citations intact across a prompt upgrade."""

    def test_v2_request_retains_exact_pre_upgrade_hash(self) -> None:
        """Match fixed hashes measured from the original v2 instructions and request."""
        instructions = select_classification_prompt("repository-product-v2")
        self.assertEqual(hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
                         "b01526121a5e946e3543858197b8757432965755fdb1a8e8dfbc90b6247f46de")
        request_body = build_classification_request(
            {"evidence": [], "product": {"short_name": "ATL03"}}, "test-model", 2048, "repository-product-v2",
        )
        self.assertEqual(identify_evidence(request_body),
                         "bac42f3d613e52185566a717801ee4a8df452d1379f69c3921870f61b03257f7")

    def test_v3_request_retains_exact_pre_upgrade_hash(self) -> None:
        """Keep the original v3 prompt and request identity for cached assessments."""
        instructions = select_classification_prompt("repository-product-v3")
        self.assertEqual(hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
                         "abff2a1c646d0659d7c45262012e2891fb9c93e7f29aff73fc0958557163b245")
        request_body = build_classification_request(
            {"evidence": [], "product": {"short_name": "ATL03"}}, "test-model", 2048, "repository-product-v3",
        )
        self.assertEqual(identify_evidence(request_body),
                         "ee97840f3c5d191f7378ffb853b3b84aa43bbc15c6d3658e3c1702593e1b5d1f")

    def test_cached_legacy_resume_preserves_reports_without_keys_or_calls(self) -> None:
        """Resume v2 and v3 conclusions without relabeling them or requesting a key."""
        for prompt_version in ("repository-product-v2", "repository-product-v3"):
            with self.subTest(prompt_version=prompt_version), TemporaryDirectory() as temporary_directory:
                self.assert_legacy_resume_preserves_saved_outputs(Path(temporary_directory), prompt_version)

    def assert_legacy_resume_preserves_saved_outputs(self, temporary_directory: Path, prompt_version: str) -> None:
        """Check a complete historical run keeps every durable result and request.

        Args:
            temporary_directory: Test-owned directory for the saved run.
            prompt_version: Historical instructions used by its cached responses.
        """
        with redirect_stderr(StringIO()):
            output_directory = Path(temporary_directory) / "legacy"
            save_legacy_classification_run(output_directory, prompt_version)
            original_files = {
                str(path.relative_to(output_directory)): path.read_bytes()
                for path in output_directory.rglob("*") if path.is_file() and path.name != "summary.json"
            }
            with patch("nasa_eo_search.evidence_classify.load_openai_api_key") as key_loader, \
                    patch("nasa_eo_search.evidence_classify.request_evidence_classification") as classify, \
                    patch("nasa_eo_search.evidence_classify.build_tls_context") as connection_setup, \
                    redirect_stderr(StringIO()) as diagnostics:
                self.assertEqual(main(["run", str(output_directory), "--model", "test-model"]), 0)
            key_loader.assert_not_called()
            classify.assert_not_called()
            connection_setup.assert_not_called()
            self.assertIn("1 cached requests; 0 eligible", diagnostics.getvalue())
            self.assertEqual(original_files, {
                str(path.relative_to(output_directory)): path.read_bytes()
                for path in output_directory.rglob("*") if path.is_file() and path.name != "summary.json"
            })
            with (output_directory / "review.csv").open(encoding="utf-8-sig", newline="") as report:
                row = next(csv.DictReader(report))
            self.assertEqual(row["prompt_version"], prompt_version)
            self.assertEqual(row["category"], "supporting_software")
            self.assertEqual(read_evidence_json(output_directory / "audit.json")["prompt_version"], prompt_version)

    def test_new_requests_cannot_reuse_legacy_response_identity(self) -> None:
        """Change the request identity even when source evidence and model are identical."""
        packet = create_report_plan(packet_count=1)["groups"][0]["packets"][0]
        current_request = build_classification_request(packet, "test-model", 2048)
        self.assertEqual(PROMPT_VERSION, "repository-product-v4")
        self.assertEqual(current_request["instructions"], CLASSIFICATION_PROMPT)
        for prompt_version in ("repository-product-v2", "repository-product-v3"):
            with self.subTest(prompt_version=prompt_version):
                legacy_request = build_classification_request(packet, "test-model", 2048, prompt_version)
                self.assertEqual(current_request["input"], legacy_request["input"])
                self.assertNotEqual(identify_evidence(current_request), identify_evidence(legacy_request))

    def test_repack_upgrades_legacy_prompt_without_touching_paid_receipts(self) -> None:
        """Write a new current-version plan while every historical source byte stays intact."""
        with TemporaryDirectory() as temporary_directory:
            root_directory = Path(temporary_directory)
            source_directory = root_directory / "legacy"
            save_legacy_classification_run(source_directory)
            original_files = {str(path.relative_to(source_directory)): path.read_bytes()
                              for path in source_directory.rglob("*") if path.is_file()}
            destination = root_directory / "repacked"
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["repack", str(source_directory), "--output", str(destination)]), 0)
            self.assertEqual(load_evidence_plan(destination)["prompt_version"], PROMPT_VERSION)
            self.assertFalse((destination / "requests").exists())
            self.assertEqual(original_files, {str(path.relative_to(source_directory)): path.read_bytes()
                                             for path in source_directory.rglob("*") if path.is_file()})

    def test_file_plans_resume_and_repack_all_supported_prompts(self) -> None:
        """Preserve file scope and byte caps when upgrading every supported file plan."""
        for prompt_version in ("repository-product-v2", "repository-product-v3", PROMPT_VERSION):
            with self.subTest(prompt_version=prompt_version), TemporaryDirectory() as temporary_directory:
                root_directory = Path(temporary_directory)
                batch_directory, catalog_directory = create_saved_evidence_inputs(root_directory)
                source_plan = read_product_evidence_groups(batch_directory, catalog_directory)
                with redirect_stderr(StringIO()):
                    file_plan = build_file_classification_plan(source_plan, True, 10, 4096, None)
                    file_plan["prompt_version"] = prompt_version
                    source_directory = root_directory / "files"
                    save_prepared_classification_plan(source_directory, file_plan)
                    destination = root_directory / "repacked"
                    self.assertEqual(main(["repack", str(source_directory), "--output", str(destination),
                                           "--packet-bytes", "4096"]), 0)
                self.assertEqual(load_evidence_plan(source_directory)["prompt_version"], prompt_version)
                repacked_plan = load_evidence_plan(destination)
                self.assertEqual(repacked_plan["prompt_version"], PROMPT_VERSION)
                self.assertEqual(repacked_plan["assessment_unit"], "file")
                self.assertEqual([group["match_row"] for group in repacked_plan["groups"]],
                                 [group["match_row"] for group in file_plan["groups"]])
                self.assertTrue(all(len(serialize_evidence(packet).encode("utf-8")) <= 4096
                                    for group in repacked_plan["groups"] for packet in group["packets"]))
                self.assertTrue((destination / "classified-matches.csv").is_file())

    def test_unknown_prompt_versions_fail_without_creating_output(self) -> None:
        """Reject unsupported versions instead of silently applying current instructions."""
        with TemporaryDirectory() as temporary_directory:
            root_directory = Path(temporary_directory)
            source_directory = root_directory / "invalid"
            source_directory.mkdir()
            plan = create_report_plan(packet_count=1)
            plan["prompt_version"] = "repository-product-v99"
            save_evidence_json(source_directory / "plan.json", {
                "schema_version": 2, "plan_id": identify_evidence(plan), "data": plan,
            })
            with self.assertRaises(ValueError):
                load_evidence_plan(source_directory)
            with self.assertRaises(ValueError):
                build_classification_request({}, "test-model", 2048, plan["prompt_version"])
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["repack", str(source_directory), "--output", str(root_directory / "new")]), 1)
            self.assertFalse((root_directory / "new").exists())


if __name__ == "__main__":
    unittest.main()

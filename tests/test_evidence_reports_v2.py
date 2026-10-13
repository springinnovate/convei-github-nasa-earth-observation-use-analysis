"""Exercise compact evidence judgments and readable, fully auditable reports."""

from contextlib import redirect_stderr
import copy
import csv
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.evidence_classify import main
from nasa_eo_search.evidence_inputs import identify_evidence, read_evidence_json
from nasa_eo_search.evidence_model import PROMPT_VERSION, validate_classification
from nasa_eo_search.evidence_reports import (
    MAX_SUMMARY_CHARACTERS, collect_group_review_reasons, combine_packet_classifications,
    load_evidence_plan, save_evidence_json, summarize_group_judgments, write_evidence_reports,
)


def create_report_plan(packet_count: int = 2) -> dict:
    """Create a tiny version-two plan with reversible compact citation mappings.

    Args:
        packet_count: Number of distinct matching blocks and model packets.

    Returns:
        A complete source plan with no genuine evidence gaps.
    """
    repository = "github.com/example/reader"
    product = {"provider": "NSIDC", "short_name": "ATL03"}
    group = {
        "repository": repository, "product": product, "group_id": identify_evidence([repository, "NSIDC", "ATL03"]),
        "warnings": [], "search_complete": True, "files": [], "evidence": [], "evidence_blocks": [], "packets": [],
    }
    for number in range(1, packet_count + 1):
        raw_line = {
            "repository": repository, "file": f"test_{number}.py", "commit": "a" * 40,
            "line_number": number, "text": 'dataset = open("ATL03_20200101000000_01230101_003_01.h5")',
            "kind": "matching_line", "character_offset": 0,
        }
        raw_line["evidence_id"] = identify_evidence(raw_line)
        group["evidence"].append(raw_line)
        group["files"].append({"file": raw_line["file"]})
        block = {
            **raw_line, "evidence_id": f"E{number}", "end_line": number,
            "source_ids": [raw_line["evidence_id"]],
            "locations": [{field: raw_line[field] for field in ("repository", "file", "commit", "line_number")}],
        }
        group["evidence_blocks"].append(block)
        group["packets"].append({
            "format_version": 2, "repository": repository, "product": product,
            "evidence": [{field: block[field] for field in (
                "evidence_id", "file", "commit", "line_number", "end_line", "text", "kind",
            )}],
        })
    return {
        "prompt_version": PROMPT_VERSION, "groups": [group], "catalog_status": "complete",
        "search_scope": "Sourcegraph indexed public content", "selected_products": 1, "source_product_queries": [],
    }


def create_packet_judgment(evidence_id: str = "E1", category: str = "supporting_software") -> dict:
    """Create a schema-complete, clear judgment citing supplied test evidence.

    Args:
        evidence_id: Compact block ID to cite.
        category: Supported relationship label for this test.

    Returns:
        Classification fields without request or reporting metadata.
    """
    return {
        "category": category, "explanation": "Tests open an ATL03 dataset.", "evidence_ids": [evidence_id],
        "needs_review": False, "evidence_types": ["test"], "observed_versions": [], "review_reasons": [],
    }


def read_review_rows(output_directory: Path) -> list[dict]:
    """Read spreadsheet-safe CSV output for focused report assertions.

    Args:
        output_directory: Temporary directory containing a generated review CSV.

    Returns:
        Parsed review rows.
    """
    with (output_directory / "review.csv").open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


class EvidenceModelVersionTwoTests(unittest.TestCase):
    """Require cited product versions and actionable, internally consistent flags."""

    def test_older_product_version_is_accepted_with_citation(self) -> None:
        """Accept version 003 in code even when catalog metadata says version 007."""
        packet = create_report_plan(1)["groups"][0]["packets"][0]
        packet["product"]["catalog_versions"] = ["007"]
        judgment = create_packet_judgment()
        judgment["observed_versions"] = [{"version": "003", "evidence_ids": ["E1"]}]
        result = validate_classification(judgment, packet)
        self.assertFalse(result["needs_review"])
        self.assertEqual(result["observed_versions"], judgment["observed_versions"])

    def test_invented_metadata_only_and_unrelated_versions_are_rejected(self) -> None:
        """Reject absent versions, substring matches, and unrelated software versions."""
        packet = create_report_plan(1)["groups"][0]["packets"][0]
        packet["product"]["catalog_versions"] = ["007"]
        for version, citation in (("007", "E1"), ("03", "E1"), ("003", "unknown"), ("3\n", "E1")):
            judgment = create_packet_judgment()
            judgment["observed_versions"] = [{"version": version, "evidence_ids": [citation]}]
            with self.subTest(version=version), self.assertRaises(ValueError):
                validate_classification(judgment, packet)
        packet["evidence"][0]["text"] = "Tool version 003. No product information."
        judgment["observed_versions"] = [{"version": "003", "evidence_ids": ["E1"]}]
        with self.assertRaises(ValueError):
            validate_classification(judgment, packet)
        packet["evidence"][0]["text"] = "ATL03 is supported by library version 003."
        with self.assertRaises(ValueError):
            validate_classification(judgment, packet)
        packet = create_report_plan(1)["groups"][0]["packets"][0]
        judgment["category"] = "unrelated"
        with self.assertRaises(ValueError):
            validate_classification(judgment, packet)

    def test_review_flags_require_actionable_reasons_and_unclear_is_enforced(self) -> None:
        """Reject inconsistent model review flags and always flag ambiguous evidence."""
        packet = create_report_plan(1)["groups"][0]["packets"][0]
        judgment = create_packet_judgment()
        for changes in (
            {"needs_review": True}, {"review_reasons": ["Implementation not collected."]},
            {"review_reasons": [""]}, {"evidence_types": ["unknown"]},
            {"observed_versions": "003"}, {"unexpected": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_classification({**judgment, **changes}, packet)
        result = validate_classification({**judgment, "category": "unclear", "evidence_ids": []}, packet)
        self.assertTrue(result["needs_review"])
        self.assertTrue(result["review_reasons"])


class EvidenceReportVersionTwoTests(unittest.TestCase):
    """Keep compact summaries separate from complete audit and coverage context."""

    def test_multiple_clear_packets_and_bounded_readme_do_not_force_review(self) -> None:
        """Avoid automatically flagging clear classifications because of packaging."""
        plan = create_report_plan()
        plan["groups"][0]["warnings"] = ["README context at abc is limited to its first 80 lines."]
        receipts = [([], {"status": "complete", "classification": create_packet_judgment(f"E{number}")})
                    for number in (1, 2)]
        with TemporaryDirectory() as directory, patch(
            "nasa_eo_search.evidence_reports.inspect_classification_receipts", side_effect=receipts,
        ):
            output = Path(directory)
            summary = write_evidence_reports(output, plan, {"model": "test-model", "max_output_tokens": 2048})
            row = read_review_rows(output)[0]
            self.assertEqual(row["needs_review"], "false")
            self.assertEqual(row["review_reasons"], "")
            self.assertIn("first 80 lines", row["evidence_limitations"])
            self.assertNotIn("Packet", row["explanation"])
            self.assertEqual(summary["completed_requests"], 2)
            self.assertEqual(summary["pending_requests"], 0)

    def test_missing_matched_context_partial_search_and_catalog_are_review_reasons(self) -> None:
        """Surface material evidence omissions independently of model confidence."""
        group = create_report_plan(1)["groups"][0]
        judgment = {**create_packet_judgment(), "packet_number": 1}
        group["warnings"] = ["test_1.py: Context unavailable (OSError)."]
        group["search_complete"] = False
        group["evidence_gaps"] = ["Notebook cell could not be decoded."]
        reasons = collect_group_review_reasons(group, [judgment], "complete", "supporting_software", "partial")
        self.assertIn(group["warnings"][0], reasons)
        self.assertIn(group["evidence_gaps"][0], reasons)
        self.assertTrue(any("searches are incomplete" in reason for reason in reasons))
        self.assertIn("The source catalog is partial.", reasons)

    def test_conflicting_and_context_only_packets_remain_conservative(self) -> None:
        """Retain real contradictions while treating context-only uncertainty neutrally."""
        judgments = [
            {**create_packet_judgment(), "packet_number": 1},
            {**create_packet_judgment("E2", "unrelated"), "packet_number": 2},
        ]
        self.assertEqual(combine_packet_classifications(judgments), "unclear")
        self.assertIn("conflicting", summarize_group_judgments(judgments, "complete", "unclear"))
        judgments[1].update(category="unclear", context_only=True, needs_review=True, review_reasons=["Context only."])
        self.assertEqual(combine_packet_classifications(judgments), "supporting_software")
        reasons = collect_group_review_reasons(create_report_plan()["groups"][0], judgments, "complete", "supporting_software", "complete")
        self.assertEqual(reasons, [])

    def test_summary_and_csv_citations_are_bounded_but_audit_is_complete(self) -> None:
        """Keep all original judgments and duplicate locations outside the short CSV."""
        plan = create_report_plan(4)
        group = plan["groups"][0]
        group["evidence_blocks"][0]["locations"].append({
            "file": "duplicate.py", "commit": "a" * 40, "line_number": 100, "end_line": 101,
        })
        judgments = []
        for number in range(1, 5):
            judgment = create_packet_judgment(f"E{number}")
            judgment["explanation"] = "Detailed finding. " * 100
            judgment["observed_versions"] = [{"version": "003", "evidence_ids": [f"E{number}"]}]
            judgments.append(judgment)
        receipts = [([], {"status": "complete", "classification": judgment}) for judgment in judgments]
        with TemporaryDirectory() as directory, patch(
            "nasa_eo_search.evidence_reports.inspect_classification_receipts", side_effect=receipts,
        ):
            output = Path(directory)
            write_evidence_reports(output, plan, {"model": "test-model", "max_output_tokens": 2048})
            row = read_review_rows(output)[0]
            audit = read_evidence_json(output / "audit.json")["groups"][0]
            self.assertLessEqual(len(row["explanation"]), MAX_SUMMARY_CHARACTERS)
            self.assertEqual(len(row["evidence_citations"].splitlines()), 3)
            self.assertEqual(len(audit["citations"]), 4)
            self.assertEqual(len(audit["packets"]), 4)
            self.assertEqual(audit["packets"][0]["classification"]["explanation"], judgments[0]["explanation"])
            self.assertEqual(len(audit["citations"][0]["all_locations"]), 2)
            self.assertIn("#L100-L101", audit["citations"][0]["all_locations"][1]["url"])
            self.assertEqual(audit["observed_versions"], [{"version": "003", "evidence_ids": ["E1", "E2", "E3", "E4"]}])

    def test_partial_processing_is_not_a_final_category(self) -> None:
        """Keep partial successful evidence visible without promoting its category."""
        receipts = [([], {"status": "complete", "classification": create_packet_judgment()}), ([], {"status": "pending"})]
        with TemporaryDirectory() as directory, patch(
            "nasa_eo_search.evidence_reports.inspect_classification_receipts", side_effect=receipts,
        ):
            output = Path(directory)
            summary = write_evidence_reports(output, create_report_plan(), {"model": "test-model", "max_output_tokens": 2048})
            row = read_review_rows(output)[0]
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["category"], "unclear")
            self.assertTrue(row["explanation"].startswith("Assessment unfinished"))
            self.assertEqual(summary["completed_requests"], 1)
            self.assertEqual(summary["pending_requests"], 1)

    def test_legacy_plan_rejection_precedes_credentials_api_and_writes(self) -> None:
        """Protect version-one outputs and receipts from accidental reruns."""
        with TemporaryDirectory() as directory:
            output = Path(directory)
            plan = create_report_plan(1)
            plan["prompt_version"] = "repository-product-v1"
            save_evidence_json(output / "plan.json", {"schema_version": 1, "plan_id": identify_evidence(plan), "data": plan})
            before = {path.name: path.read_bytes() for path in output.iterdir()}
            errors = StringIO()
            with patch("nasa_eo_search.evidence_classify.load_openai_api_key") as key_reader, patch(
                "nasa_eo_search.evidence_classify.request_evidence_classification",
            ) as request, redirect_stderr(errors):
                self.assertEqual(main(["run", str(output), "--model", "test-model"]), 1)
                key_reader.assert_not_called()
                request.assert_not_called()
            self.assertIn("repack SOURCE --output NEW", errors.getvalue())
            self.assertEqual(before, {path.name: path.read_bytes() for path in output.iterdir()})

    def test_plan_validates_compact_citation_mappings_and_packet_version(self) -> None:
        """Reject changed block text, source mappings, and old-format packets."""
        with TemporaryDirectory() as directory:
            output = Path(directory)
            valid_plan = create_report_plan(1)
            save_evidence_json(output / "plan.json", {"schema_version": 2, "plan_id": identify_evidence(valid_plan), "data": valid_plan})
            self.assertEqual(load_evidence_plan(output), valid_plan)
            for changed_field in ("text", "source_ids", "format_version"):
                plan = copy.deepcopy(valid_plan)
                if changed_field == "source_ids":
                    plan["groups"][0]["evidence_blocks"][0]["source_ids"] = ["missing-source"]
                else:
                    target = plan["groups"][0]["packets"][0]
                    if changed_field == "text":
                        target["evidence"][0]["text"] = "modified"
                    else:
                        target["format_version"] = 1
                save_evidence_json(output / "plan.json", {"schema_version": 2, "plan_id": identify_evidence(plan), "data": plan})
                with self.subTest(changed_field=changed_field), self.assertRaises(ValueError):
                    load_evidence_plan(output)

    def test_duplicate_locations_validate_their_compact_projection(self) -> None:
        """Allow two-file deduplication while retaining richer audit-only metadata."""
        plan = create_report_plan(2)
        group = plan["groups"][0]
        primary, duplicate = group["evidence_blocks"]
        primary["source_ids"].extend(duplicate["source_ids"])
        primary["locations"].extend(duplicate["locations"])
        for location in primary["locations"]:
            location["end_line"] = location["line_number"]
            location["source_ids"] = primary["source_ids"]
            location["representation"] = "source_lines"
        group["evidence_blocks"] = [primary]
        group["packets"] = group["packets"][:1]
        compact_locations = [
            {field: location[field] for field in ("file", "commit", "line_number", "end_line")}
            for location in primary["locations"]
        ]
        group["packets"][0]["evidence"][0]["locations"] = compact_locations
        with TemporaryDirectory() as directory:
            output = Path(directory)
            save_evidence_json(output / "plan.json", {"schema_version": 2, "plan_id": identify_evidence(plan), "data": plan})
            self.assertEqual(load_evidence_plan(output), plan)
            compact_locations[1]["line_number"] += 1
            save_evidence_json(output / "plan.json", {"schema_version": 2, "plan_id": identify_evidence(plan), "data": plan})
            with self.assertRaisesRegex(ValueError, "duplicate locations"):
                load_evidence_plan(output)


if __name__ == "__main__":
    unittest.main()

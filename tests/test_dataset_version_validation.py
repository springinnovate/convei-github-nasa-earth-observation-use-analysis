"""Validate explicit dataset versions and reuse responses rejected by old checks."""

from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.evidence_classify import main, save_prepared_classification_plan
from nasa_eo_search.evidence_inputs import identify_evidence, read_evidence_json
from nasa_eo_search.evidence_model import (
    build_classification_request, validate_classification, version_occurs_in_product_csv,
    version_occurs_with_product,
)
from nasa_eo_search.evidence_reports import inspect_classification_receipts, save_evidence_json
from test_evidence_classify import make_model_response
from test_evidence_reports_v2 import create_packet_judgment, create_report_plan


def create_modis_version_plan() -> dict:
    """Build one auditable file excerpt containing an older Earth Engine version.

    Returns:
        Repository-level test plan with MODIS collection 006 in code and 061
        only in catalog metadata. No credentials or external files are accessed.
    """
    plan = create_report_plan(packet_count=1)
    group = plan["groups"][0]
    group["product"] = {"provider": "LPCLOUD", "short_name": "MOD13Q1", "catalog_versions": ["061"]}
    group["group_id"] = identify_evidence([group["repository"], "LPCLOUD", "MOD13Q1"])
    raw_evidence = group["evidence"][0]
    raw_evidence["text"] = 'collection = ee.ImageCollection("MODIS/006/MOD13Q1")'
    raw_evidence.pop("evidence_id")
    raw_evidence["evidence_id"] = identify_evidence(raw_evidence)
    block = group["evidence_blocks"][0]
    block["text"] = raw_evidence["text"]
    block["source_ids"] = [raw_evidence["evidence_id"]]
    packet = group["packets"][0]
    packet["product"] = group["product"]
    packet["evidence"][0]["text"] = raw_evidence["text"]
    return plan


class DatasetVersionValidationTests(unittest.TestCase):
    """Accept explicit source versions without inferring them from metadata."""

    def test_modis_collection_paths_expose_exact_version_tokens(self) -> None:
        """Accept both old and current collection versions before the product ID."""
        for version in ("006", "061"):
            for source_text in (
                f'ee.ImageCollection("MODIS/{version}/MOD13Q1")',
                f"https://example.org/MODIS/{version}/MOD13Q1",
                f"modis/{version}/mod13q1",
            ):
                with self.subTest(version=version, source_text=source_text):
                    self.assertTrue(version_occurs_with_product(version, source_text, "MOD13Q1"))

    def test_modis_path_versions_reject_other_identifiers_and_numeric_folders(self) -> None:
        """Reject partial versions, other products, suffixes, and unrelated numbers."""
        rejected_sources = (
            ("006", "MODIS/0061/MOD13Q1"),
            ("0061", "MODIS/006/MOD13Q1"),
            ("006", "MODIS/006/MYD13Q1"),
            ("006", "MODIS/006/MOD13Q10"),
            ("006", "MODIS/006/MOD13Q1_E"),
            ("006", "MODIS/006/MOD13Q1helper"),
            ("006", "myMODIS/006/MOD13Q1"),
            ("006", "data/006/MOD13Q1"),
            ("006", "MODIS/006/other/MOD13Q1"),
            ("006", "MODIS/version006/MOD13Q1"),
            ("061", "MODIS/006/MOD13Q1/061"),
            ("006", "MODIS/061/MOD13Q1; library version 006"),
            ("006", "Tool version 006 supports MOD13Q1."),
        )
        for version, source_text in rejected_sources:
            with self.subTest(version=version, source_text=source_text):
                self.assertFalse(version_occurs_with_product(version, source_text, "MOD13Q1"))

    def test_cited_old_collection_is_accepted_despite_newer_catalog_metadata(self) -> None:
        """Validate an observed 006 path while the saved catalog describes 061."""
        packet = create_modis_version_plan()["groups"][0]["packets"][0]
        judgment = create_packet_judgment()
        judgment["observed_versions"] = [{"version": "006", "evidence_ids": ["E1"]}]
        validated = validate_classification(judgment, packet)
        self.assertEqual(validated["observed_versions"], judgment["observed_versions"])

    def test_explicit_version_before_parenthesized_product_is_accepted(self) -> None:
        """Accept a title's adjacent version marker and the literal v07 suffix."""
        product_name = "GPM_3IMERGDF"
        accepted_sources = (
            ("06", "GPM IMERG Final Precipitation L3 1 day V06 (GPM_3IMERGDF)"),
            ("V06", "GPM IMERG Final Precipitation L3 1 day V06 (GPM_3IMERGDF)"),
            ("v06", "GPM IMERG Final Precipitation L3 1 day V06 (GPM_3IMERGDF)"),
            ("06", "Version 06 (GPM_3IMERGDF)"),
            ("06", "ver. 06 ( GPM_3IMERGDF )"),
            ("v07", "id: GPM_3IMERGDF.v07"),
        )
        for version, source_text in accepted_sources:
            with self.subTest(version=version, source_text=source_text):
                self.assertTrue(version_occurs_with_product(version, source_text, product_name))

    def test_parenthesized_titles_reject_unlinked_or_unmarked_versions(self) -> None:
        """Reject other products, different versions, and ordinary numeric statistics."""
        rejected_sources = (
            ("06", "Version 07 (GPM_3IMERGDF)"),
            ("V06", "Version V07 (GPM_3IMERGDF)"),
            ("06", "V06 (GPM_3IMERGHH)"),
            ("06", "V06 (GPM_3IMERGDF_E)"),
            ("06", "V006 (GPM_3IMERGDF)"),
            ("06", "V061 (GPM_3IMERGDF)"),
            ("06", "V06 for a separate product; later (GPM_3IMERGDF)"),
            ("06", "V06\nDataset overview (GPM_3IMERGDF)"),
            ("06", "06 (GPM_3IMERGDF)"),
            ("6", "Sample count: 6 (GPM_3IMERGDF)"),
        )
        for version, source_text in rejected_sources:
            with self.subTest(version=version, source_text=source_text):
                self.assertFalse(version_occurs_with_product(version, source_text, "GPM_3IMERGDF"))

    def test_terminal_version_in_exact_product_name_is_accepted(self) -> None:
        """Recognize 4.1 and v4.1 in the complete MUR product identifier."""
        product_name = "MUR-JPL-L4-GLOB-v4.1"
        for version in ("4.1", "v4.1", "V4.1"):
            for source_text in (
                f'collection = "{product_name}"',
                f"https://example.org/products/{product_name}/granule.nc",
            ):
                with self.subTest(version=version, source_text=source_text):
                    self.assertTrue(version_occurs_with_product(version, source_text, product_name))

    def test_embedded_versions_require_exact_product_and_source_text(self) -> None:
        """Reject partial versions, other identifiers, derived suffixes, and metadata."""
        product_name = "MUR-JPL-L4-GLOB-v4.1"
        for version in ("4", "v4", "4.10", "04.1"):
            with self.subTest(version=version):
                self.assertFalse(version_occurs_with_product(version, product_name, product_name))
        for source_text in (
            "OTHER-L4-GLOB-v4.1",
            product_name + "_E",
            product_name + "-derived",
            product_name + ".extra",
            "MUR-JPL-L4-GLOB-v4.10",
        ):
            with self.subTest(source_text=source_text):
                self.assertFalse(version_occurs_with_product("4.1", source_text, product_name))
        self.assertFalse(version_occurs_with_product("4.1", "MUR-JPL-L4-GLOB", product_name))
        packet = create_modis_version_plan()["groups"][0]["packets"][0]
        packet["product"] = {"short_name": product_name, "catalog_versions": ["4.1"]}
        packet["evidence"][0]["text"] = "load_sea_surface_temperature()"
        judgment = create_packet_judgment()
        judgment["observed_versions"] = [{"version": "4.1", "evidence_ids": ["E1"]}]
        with self.assertRaisesRegex(ValueError, "not explicitly associated"):
            validate_classification(judgment, packet)

    def test_catalog_only_version_is_not_a_source_observation(self) -> None:
        """Reject catalog versions and mapping values absent from the cited code."""
        packet = create_modis_version_plan()["groups"][0]["packets"][0]
        packet["product"]["matched_identifiers"] = [{"term": "MOD13Q1", "catalog_version": "061"}]
        judgment = create_packet_judgment()
        judgment["observed_versions"] = [{"version": "061", "evidence_ids": ["E1"]}]
        with self.assertRaisesRegex(ValueError, "not explicitly associated"):
            validate_classification(judgment, packet)
        packet["evidence"][0]["text"] = 'collection_name = "MOD13Q1"'
        packet["product"]["catalog_versions"] = ["006"]
        judgment["observed_versions"] = [{"version": "006", "evidence_ids": ["E1"]}]
        with self.assertRaisesRegex(ValueError, "not explicitly associated"):
            validate_classification(judgment, packet)

    def test_enhanced_smap_identifier_cannot_supply_base_product_version(self) -> None:
        """Reject 005_RAW from SPL3SMP_E when assessing the distinct SPL3SMP product."""
        source_text = "SMAPL3 = ee.ImageCollection('NASA/SMAP/SPL3SMP_E/005_RAW')"
        self.assertFalse(version_occurs_with_product("005_RAW", source_text, "SPL3SMP"))
        packet = create_modis_version_plan()["groups"][0]["packets"][0]
        packet["product"] = {"short_name": "SPL3SMP", "catalog_versions": ["009"]}
        packet["evidence"][0]["text"] = source_text
        judgment = create_packet_judgment()
        judgment["observed_versions"] = [{"version": "005_RAW", "evidence_ids": ["E1"]}]
        with self.assertRaisesRegex(ValueError, "not explicitly associated"):
            validate_classification(judgment, packet)

    def test_smap_collection_versions_require_the_complete_product_path(self) -> None:
        """Accept enhanced SMAP versions only for the exact enhanced product."""
        for version in ("006", "005_RAW"):
            source_text = f"ee.ImageCollection('NASA/SMAP/SPL3SMP_E/{version}')"
            with self.subTest(version=version):
                self.assertTrue(version_occurs_with_product(version, source_text, "SPL3SMP_E"))
                self.assertFalse(version_occurs_with_product(version, source_text, "SPL3SMP"))
                self.assertFalse(version_occurs_with_product(version, source_text, "SPL3SMP_E_2"))
                self.assertFalse(version_occurs_with_product(version, source_text.replace(version, version + "1"), "SPL3SMP_E"))
        self.assertFalse(version_occurs_with_product("006", "OTHER/SPL3SMP_E/006", "SPL3SMP_E"))

    def test_csv_named_columns_associate_the_exact_product_and_version(self) -> None:
        """Accept explicit short-name and version values from one complete CSV row."""
        accepted_sources = (
            "short_name,version\nSPL3SMP,009\n",
            '"short_name","version"\n"SPL3SMP","009"\n',
            'title,version,short_name\n"Soil moisture, daily",009,SPL3SMP\n',
            "short_name,version\nSPL3SMP_E,006\nSPL3SMP,009\n",
        )
        for source_text in accepted_sources:
            with self.subTest(source_text=source_text):
                self.assertTrue(version_occurs_in_product_csv("009", source_text, "SPL3SMP"))
                self.assertTrue(version_occurs_with_product("009", source_text, "SPL3SMP"))
        packet = create_modis_version_plan()["groups"][0]["packets"][0]
        packet["product"] = {"short_name": "SPL3SMP", "catalog_versions": ["010"]}
        packet["evidence"][0]["text"] = accepted_sources[0]
        judgment = create_packet_judgment()
        judgment["observed_versions"] = [{"version": "009", "evidence_ids": ["E1"]}]
        self.assertEqual(validate_classification(judgment, packet)["observed_versions"],
                         judgment["observed_versions"])

    def test_explicit_product_version_urls_keep_sibling_products_separate(self) -> None:
        """Accept NSIDC version URLs only for their complete product identifier."""
        source_text = "https://nsidc.org/data/data-access-tool/SPL3SMP_E/versions/6"
        self.assertTrue(version_occurs_with_product("6", source_text, "SPL3SMP_E"))
        self.assertFalse(version_occurs_with_product("6", source_text, "SPL3SMP"))
        self.assertFalse(version_occurs_with_product("6", source_text + "1", "SPL3SMP_E"))
        self.assertFalse(version_occurs_with_product("6", source_text.replace("/versions/", "/"), "SPL3SMP_E"))
        self.assertFalse(version_occurs_with_product("6", source_text.replace("SPL3SMP_E", "OTHER_SPL3SMP_E"), "SPL3SMP_E"))

    def test_adjacent_catalog_title_and_version_fields_require_exact_product(self) -> None:
        """Recognize a cited JSONnet version field without borrowing other objects."""
        source_text = "  title: 'SPL3SMP_E.006 Soil Moisture',\n\n  version: '6',\n"
        self.assertTrue(version_occurs_with_product("6", source_text, "SPL3SMP_E"))
        self.assertFalse(version_occurs_with_product("6", source_text, "SPL3SMP"))
        self.assertFalse(version_occurs_with_product("6", source_text.replace("SPL3SMP_E.", "SPL3SMP_E_OTHER."), "SPL3SMP_E"))
        self.assertFalse(version_occurs_with_product("6", source_text.replace("'6'", "'61'"), "SPL3SMP_E"))
        self.assertFalse(version_occurs_with_product("6", source_text.replace("\n\n", "\n}, {\n"), "SPL3SMP_E"))
        self.assertFalse(version_occurs_with_product("6", source_text.replace("\n\n", "\n  software: 'helper',\n"), "SPL3SMP_E"))
        for sibling_title in ("SPL3SMP_E.OTHER", "SPL3SMP_E/OTHER", "SPL3SMP_E.006_OTHER", "SPL3SMP_E.006extra"):
            with self.subTest(sibling_title=sibling_title):
                self.assertFalse(version_occurs_with_product("6", source_text.replace("SPL3SMP_E.006", sibling_title), "SPL3SMP_E"))

    def test_csv_versions_cannot_come_from_other_rows_or_numeric_columns(self) -> None:
        """Reject sibling products, partial values, and numbers outside the version column."""
        rejected_sources = (
            "short_name,version\nSPL3SMP_E,009\n",
            "short_name,version\nSPL3SMP_OTHER,009\n",
            "short_name,version\nSPL3SMP,008\nSPL3SMP_E,009\n",
            "short_name,version\nSPL3SMP_E,009\nSPL3SMP,008\n",
            "short_name,version\nSPL3SMP,0090\n",
            "short_name,version\nSPL3SMP,9\n",
            "short_name,version,record_count\nSPL3SMP,008,009\n",
        )
        for source_text in rejected_sources:
            with self.subTest(source_text=source_text):
                self.assertFalse(version_occurs_in_product_csv("009", source_text, "SPL3SMP"))
                self.assertFalse(version_occurs_with_product("009", source_text, "SPL3SMP"))

    def test_csv_versions_require_a_unique_header_and_source_evidence(self) -> None:
        """Reject unnamed rows, ambiguous headers, and catalog metadata without source support."""
        rejected_sources = (
            "SPL3SMP,009\n",
            "name,version\nSPL3SMP,009\n",
            "short_name,collection\nSPL3SMP,009\n",
            "short_name,version,version\nSPL3SMP,009,009\n",
            "short_name,short_name,version\nSPL3SMP,SPL3SMP,009\n",
            "short_name,version\n",
            "",
        )
        for source_text in rejected_sources:
            with self.subTest(source_text=source_text):
                self.assertFalse(version_occurs_in_product_csv("009", source_text, "SPL3SMP"))
        packet = create_modis_version_plan()["groups"][0]["packets"][0]
        packet["product"] = {"short_name": "SPL3SMP", "catalog_versions": ["009"]}
        packet["evidence"][0]["text"] = "load_soil_moisture()"
        judgment = create_packet_judgment()
        judgment["observed_versions"] = [{"version": "009", "evidence_ids": ["E1"]}]
        with self.assertRaisesRegex(ValueError, "not explicitly associated"):
            validate_classification(judgment, packet)

    def test_malformed_or_incomplete_csv_does_not_establish_a_version(self) -> None:
        """Return false for broken quoting and rows that do not match the CSV header."""
        rejected_sources = (
            'short_name,version\n"SPL3SMP,009\n',
            'short_name,version\n"SPL3SMP"unexpected,009\n',
            '"short_name,version\nSPL3SMP,009\n',
            "short_name,version,provider\nSPL3SMP,009\n",
            "short_name,version\nSPL3SMP,009,NSIDC\n",
        )
        for source_text in rejected_sources:
            with self.subTest(source_text=source_text):
                self.assertFalse(version_occurs_in_product_csv("009", source_text, "SPL3SMP"))

    def test_immediate_version_allows_closing_punctuation_without_partial_digits(self) -> None:
        """Accept a complete SRTM version before punctuation and reject longer numbers."""
        for source_text in (
            "(SRTMGL1_003).",
            'dataset = "SRTMGL1_003";',
            "[SRTMGL1_003]",
        ):
            with self.subTest(source_text=source_text):
                self.assertTrue(version_occurs_with_product("003", source_text, "SRTMGL1"))
        for source_text in ("(SRTMGL1_0030).", "(SRTMGL1_0031).", "SRTMGL1_003extra"):
            with self.subTest(source_text=source_text):
                self.assertFalse(version_occurs_with_product("003", source_text, "SRTMGL1"))

    def test_failed_version_validation_reuses_durable_response_without_new_call(self) -> None:
        """Recover a paid response rejected by the old path check without retrying."""
        with TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "classification"
            plan = create_modis_version_plan()
            with redirect_stderr(StringIO()):
                save_prepared_classification_plan(output_directory, plan)
            packet = plan["groups"][0]["packets"][0]
            request_body = build_classification_request(packet, "test-model", 2048, plan["prompt_version"])
            request_id = identify_evidence(request_body)
            response = make_model_response(request_body)
            judgment = json.loads(response["output"][0]["content"][0]["text"])
            judgment["observed_versions"] = [{"version": "006", "evidence_ids": ["E1"]}]
            response["output"][0]["content"][0]["text"] = json.dumps(judgment)
            receipt_directory = output_directory / "requests" / request_id
            receipt_directory.mkdir(parents=True)
            receipt_path = receipt_directory / "attempt-000001.json"
            save_evidence_json(receipt_path, {
                "request_id": request_id, "prompt_version": plan["prompt_version"],
                "request": request_body, "response": response, "status": "failed",
                "error": "Observed version is not explicitly associated with the product in its cited source text.",
            })
            original_receipt = receipt_path.read_bytes()
            attempts, recovered = inspect_classification_receipts(output_directory, request_body, packet)
            self.assertEqual(attempts, [receipt_path])
            self.assertEqual(recovered["status"], "complete")
            self.assertEqual(recovered["classification"]["observed_versions"], judgment["observed_versions"])
            with patch("nasa_eo_search.evidence_classify.load_openai_api_key") as key_loader, \
                    patch("nasa_eo_search.evidence_classify.request_evidence_classification") as model_request, \
                    patch("nasa_eo_search.evidence_classify.build_tls_context") as connection_setup, \
                    redirect_stderr(StringIO()) as diagnostics:
                self.assertEqual(main(["run", str(output_directory), "--model", "test-model"]), 0)
            key_loader.assert_not_called()
            model_request.assert_not_called()
            connection_setup.assert_not_called()
            self.assertIn("1 cached requests; 0 eligible", diagnostics.getvalue())
            self.assertEqual(receipt_path.read_bytes(), original_receipt)
            self.assertEqual(list(receipt_directory.iterdir()), [receipt_path])
            summary = read_evidence_json(output_directory / "summary.json")
            self.assertEqual(summary["status"], "complete")
            self.assertEqual(summary["groups_by_status"]["complete"], 1)


if __name__ == "__main__":
    unittest.main()

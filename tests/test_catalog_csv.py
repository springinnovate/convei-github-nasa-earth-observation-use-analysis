"""Test CSV catalog exports, version associations, provenance, and file safety."""

from contextlib import redirect_stderr
from copy import deepcopy
import csv
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.catalog_csv import (
    CSV_COLUMNS, build_product_csv_row, main, protect_spreadsheet_cell,
    read_catalog_csv_rows, write_catalog_csv,
)


EXAMPLE_PRODUCT = {
    "provider": "NSIDC", "short_name": "ATL03",
    "collections": [
        {"concept_id": "C1-NSIDC", "entry_id": "ATL03_006", "version": "006",
         "title": 'Photon, "height" data', "summary": "First line\nCaf\u00e9, ice \u2014 \u51b0"},
        {"concept_id": "C2-NSIDC", "entry_id": None, "version": "007",
         "title": "Second version", "summary": None},
    ],
    "candidate_signatures": [
        {"term": "ATL03", "review_status": "unreviewed", "sources": [
            {"concept_id": "C1-NSIDC", "source_field": "short_name"},
            {"concept_id": "C2-NSIDC", "source_field": "short_name"},
        ]},
        {"term": "ATL03_006", "review_status": "approved", "sources": [
            {"concept_id": "C1-NSIDC", "source_field": "entry_id"},
        ]},
    ],
}


def save_example_catalog(
    parent_directory: str,
    products: list[dict] | None = None,
    status: str = "complete",
) -> Path:
    """Write a small catalog fixture with matching counts and run provenance.

    Args:
        parent_directory: Existing temporary directory for the fixture.
        products: Product rows; defaults to one multiversion example.
        status: Source collection status to record in summary.json.

    Returns:
        Path to the newly created catalog directory.
    """

    catalog_directory = Path(parent_directory) / "catalog"
    catalog_directory.mkdir()
    products = [EXAMPLE_PRODUCT] if products is None else products
    summary = {
        "schema_version": 1, "status": status, "products_saved": len(products),
        "scope": "Public EOSDIS collections", "source_url": "https://example.test/catalog",
        "started_at": "2026-09-18T10:00:00+00:00", "finished_at": "2026-09-18T10:01:00+00:00",
        "warnings": [] if status == "complete" else ["Collection stopped early"],
    }
    (catalog_directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (catalog_directory / "products.jsonl").write_text(
        "".join(json.dumps(product) + "\n" for product in products), encoding="utf-8",
    )
    return catalog_directory


class CatalogCsvTests(unittest.TestCase):
    """Check spreadsheet output without contacting external services."""

    def test_nested_product_metadata_is_validated(self) -> None:
        """Reject invalid version fields, candidates, and broken source references."""

        invalid_products = [None, [], {**EXAMPLE_PRODUCT, "provider": " "},
                            {**EXAMPLE_PRODUCT, "collections": []},
                            {**EXAMPLE_PRODUCT, "collections": [None]},
                            {**EXAMPLE_PRODUCT, "candidate_signatures": {}},
                            {**EXAMPLE_PRODUCT, "candidate_signatures": [None]}]
        for field, value in (("title", None), ("concept_id", ""), ("version", 7), ("summary", [])):
            product = deepcopy(EXAMPLE_PRODUCT)
            product["collections"][0][field] = value
            invalid_products.append(product)
        for field, value in (("term", ""), ("review_status", None), ("sources", []),
                             ("sources", [None]), ("sources", [{"concept_id": "C-unknown", "source_field": "id"}])):
            product = deepcopy(EXAMPLE_PRODUCT)
            product["candidate_signatures"][0][field] = value
            invalid_products.append(product)
        for product in invalid_products:
            with self.subTest(product=product), self.assertRaises(ValueError):
                build_product_csv_row(product)

    def test_invalid_product_error_identifies_line(self) -> None:
        """Include the source JSON Lines line number when a later record is broken."""

        with TemporaryDirectory() as temporary_directory:
            catalog_directory = save_example_catalog(temporary_directory)
            with (catalog_directory / "products.jsonl").open("a", encoding="utf-8") as products_output:
                products_output.write("[]\n")
            with self.assertRaisesRegex(ValueError, "products.jsonl line 2"):
                read_catalog_csv_rows(catalog_directory)

    def test_oversized_cells_are_preserved_with_warning(self) -> None:
        """Warn about Excel's cell limit without silently truncating descriptions."""

        with TemporaryDirectory() as temporary_directory:
            product = deepcopy(EXAMPLE_PRODUCT)
            product["collections"][0]["summary"] = "x" * 32768
            catalog_directory = save_example_catalog(temporary_directory, [product])
            output_path = Path(temporary_directory) / "large.csv"
            diagnostics = StringIO()
            with redirect_stderr(diagnostics):
                self.assertEqual(main([str(catalog_directory), "--output", str(output_path)]), 0)
            self.assertIn("32,767", diagnostics.getvalue())
            with output_path.open(encoding="utf-8-sig", newline="") as csv_input:
                row = next(csv.DictReader(csv_input))
            self.assertIn("x" * 32768, row["collection_descriptions"])

    def test_command_reports_interruption_and_file_errors(self) -> None:
        """Return readable diagnostics and exit codes for export-stage failures."""

        for error, expected_code in ((KeyboardInterrupt(), 130), (OSError("disk full"), 1)):
            with self.subTest(error=error), TemporaryDirectory() as temporary_directory:
                catalog_directory = save_example_catalog(temporary_directory)
                with patch("nasa_eo_search.catalog_csv.write_catalog_csv", side_effect=error), redirect_stderr(StringIO()):
                    self.assertEqual(main([str(catalog_directory), "--output", str(Path(temporary_directory) / "export.csv")]), expected_code)

    def test_invalid_summary_fields_are_rejected(self) -> None:
        """Reject active runs, unknown schemas, invalid counts, and bad provenance."""

        for field, value in (("schema_version", 2), ("schema_version", True),
                             ("status", "running"), ("status", "unknown"),
                             ("products_saved", -1), ("products_saved", True),
                             ("products_saved", 2), ("warnings", [None]),
                             ("scope", None), ("source_url", [])):
            with self.subTest(field=field, value=value), TemporaryDirectory() as temporary_directory:
                catalog_directory = save_example_catalog(temporary_directory)
                summary_path = catalog_directory / "summary.json"
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                summary[field] = value
                summary_path.write_text(json.dumps(summary), encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_catalog_csv_rows(catalog_directory)

    def test_existing_outputs_and_inputs_are_never_overwritten(self) -> None:
        """Refuse existing output paths, including the original product file."""

        with TemporaryDirectory() as temporary_directory:
            catalog_directory = save_example_catalog(temporary_directory)
            output_path = Path(temporary_directory) / "existing.csv"
            output_path.write_text("existing export", encoding="utf-8")
            for destination in (output_path, catalog_directory / "products.jsonl"):
                contents = destination.read_bytes()
                with redirect_stderr(StringIO()):
                    self.assertEqual(main([str(catalog_directory), "--output", str(destination)]), 1)
                self.assertEqual(destination.read_bytes(), contents)

    def test_write_errors_and_interruptions_remove_partial_export(self) -> None:
        """Remove only the new export when serialization fails or is interrupted."""

        for error in (OSError("disk full"), csv.Error("writer error"), KeyboardInterrupt()):
            with self.subTest(error=error), TemporaryDirectory() as temporary_directory:
                output_path = Path(temporary_directory) / "partial.csv"
                with patch("nasa_eo_search.catalog_csv.csv.DictWriter.writerows", side_effect=error):
                    with self.assertRaises(type(error)):
                        write_catalog_csv(output_path, [])
                self.assertFalse(output_path.exists())

    def test_partial_catalogs_are_exported_with_visible_status(self) -> None:
        """Preserve incomplete, failed, and interrupted source statuses in each row."""

        for status in ("incomplete", "failed", "interrupted"):
            with self.subTest(status=status), TemporaryDirectory() as temporary_directory:
                catalog_directory = save_example_catalog(temporary_directory, status=status)
                output_path = Path(temporary_directory) / "products.csv"
                diagnostics = StringIO()
                with redirect_stderr(diagnostics):
                    self.assertEqual(main([str(catalog_directory), "--output", str(output_path)]), 0)
                with output_path.open(encoding="utf-8-sig", newline="") as csv_input:
                    row = next(csv.DictReader(csv_input))
                self.assertEqual(row["catalog_status"], status)
                self.assertEqual(row["catalog_warnings"], "Collection stopped early")
                self.assertIn("Warning: source catalog status", diagnostics.getvalue())

    def test_empty_catalog_has_csv_headers_and_zero_rows(self) -> None:
        """Write a valid header-only CSV for a completed empty catalog."""

        with TemporaryDirectory() as temporary_directory:
            catalog_directory = save_example_catalog(temporary_directory, [])
            output_path = Path(temporary_directory) / "empty.csv"
            with redirect_stderr(StringIO()):
                self.assertEqual(main([str(catalog_directory), "--output", str(output_path)]), 0)
            with output_path.open(encoding="utf-8-sig", newline="") as csv_input:
                reader = csv.DictReader(csv_input)
                self.assertEqual(reader.fieldnames, list(CSV_COLUMNS))
                self.assertEqual(list(reader), [])

    def test_invalid_catalogs_fail_before_creating_csv(self) -> None:
        """Reject broken JSON, missing files, invalid metadata, and count mismatches."""

        replacements = [
            ("summary.json", "not json"), ("summary.json", "[]"),
            ("summary.json", None), ("products.jsonl", None),
            ("products.jsonl", "not json\n"), ("products.jsonl", "{}\n"),
            ("products.jsonl", ""),
            ("products.jsonl", (json.dumps(EXAMPLE_PRODUCT) + "\n") * 2),
        ]
        for filename, replacement in replacements:
            with self.subTest(filename=filename, replacement=replacement), TemporaryDirectory() as temporary_directory:
                catalog_directory = save_example_catalog(temporary_directory)
                input_path = catalog_directory / filename
                if replacement is None:
                    input_path.unlink()
                else:
                    input_path.write_text(replacement, encoding="utf-8")
                output_path = Path(temporary_directory) / "export.csv"
                with redirect_stderr(StringIO()):
                    self.assertEqual(main([str(catalog_directory), "--output", str(output_path)]), 1)
                self.assertFalse(output_path.exists())


    def test_csv_round_trip_preserves_text_provenance_and_inputs(self) -> None:
        """Export quoted Unicode text and multiline values without changing inputs."""

        with TemporaryDirectory() as temporary_directory:
            catalog_directory = save_example_catalog(temporary_directory)
            original_files = {path: path.read_bytes() for path in catalog_directory.iterdir()}
            output_path = Path(temporary_directory) / "products.csv"
            with redirect_stderr(StringIO()), patch("urllib.request.urlopen", side_effect=AssertionError("network request")):
                self.assertEqual(main([str(catalog_directory), "--output", str(output_path)]), 0)
            self.assertTrue(output_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            with output_path.open(encoding="utf-8-sig", newline="") as csv_input:
                reader = csv.DictReader(csv_input)
                rows = list(reader)
                self.assertEqual(reader.fieldnames, list(CSV_COLUMNS))
            self.assertEqual(len(rows), 1)
            for field, value in build_product_csv_row(EXAMPLE_PRODUCT).items():
                self.assertEqual(rows[0][field], value)
            self.assertEqual(rows[0]["catalog_status"], "complete")
            self.assertEqual(rows[0]["source_catalog_directory"], str(catalog_directory.resolve()))
            self.assertEqual(rows[0]["catalog_source_url"], "https://example.test/catalog")
            for path, contents in original_files.items():
                self.assertEqual(path.read_bytes(), contents)

    def test_formula_like_cells_receive_visible_text_prefix(self) -> None:
        """Label dangerous prefixes, including hidden and full-width variants."""

        for value in ("=1+1", "+SUM(A1)", "-2", "@command", " \t=1", "\rtext",
                      "\ntext", "\ttext", "\ufeff=1", "\x00=1", "\uff1d1", "\uff0b1", "\uff0d1", "\uff201"):
            with self.subTest(value=value):
                self.assertEqual(protect_spreadsheet_cell(value), "Text: " + value)
        for value in ("ATL03", "Caf\u00e9", "[1] =1", "a,b", 'a"b', "a\nb", "", "006"):
            self.assertEqual(protect_spreadsheet_cell(value), value)
        with TemporaryDirectory() as temporary_directory:
            product = {**EXAMPLE_PRODUCT, "provider": '=HYPERLINK("url")', "short_name": "+formula"}
            catalog_directory = save_example_catalog(temporary_directory, [product])
            _, rows = read_catalog_csv_rows(catalog_directory)
            self.assertEqual(rows[0]["provider"], 'Text: =HYPERLINK("url")')
            self.assertEqual(rows[0]["short_name"], "Text: +formula")

    def test_versions_and_candidate_sources_keep_their_associations(self) -> None:
        """Number each collection and candidate consistently across its columns."""

        row = build_product_csv_row(EXAMPLE_PRODUCT)
        self.assertEqual(row["collection_count"], "2")
        self.assertEqual(row["collection_versions"], "[1] 006\n[2] 007")
        self.assertEqual(row["collection_entry_ids"], "[1] ATL03_006\n[2] ")
        self.assertEqual(row["collection_concept_ids"], "[1] C1-NSIDC\n[2] C2-NSIDC")
        self.assertEqual(row["collection_descriptions"], "[1] First line\nCaf\u00e9, ice \u2014 \u51b0\n[2] ")
        self.assertEqual(row["candidate_search_terms"], "[1] ATL03\n[2] ATL03_006")
        self.assertEqual(row["candidate_review_statuses"], "[1] unreviewed\n[2] approved")
        self.assertEqual(row["candidate_sources"],
                         "[1] C1-NSIDC (short_name); C2-NSIDC (short_name)\n[2] C1-NSIDC (entry_id)")

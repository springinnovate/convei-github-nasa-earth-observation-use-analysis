"""Check dataset inventory exports and automatic catalog-term planning offline."""

from copy import deepcopy
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nasa_eo_search.batch_plan import identify_search_query, validate_saved_batch_plan
from nasa_eo_search.pipeline_catalog import DATASET_COLUMNS, build_catalog_search_plan, export_dataset_table
from nasa_eo_search.repo_preview import build_sourcegraph_product_query


def make_pipeline_product(short_name: str = "ATL03", provider: str = "NSIDC") -> dict:
    """Create saved product metadata with only actual catalog-derived identifiers.

    Args:
        short_name: Product family identifier.
        provider: Catalog provider identifier.

    Returns:
        One validated product with a short name, entry ID, and concept-ID term.
    """
    concept_id = f"C-{provider}-{short_name}"
    entry_id = f"{short_name}_007"
    return {
        "provider": provider, "short_name": short_name,
        "collections": [{"concept_id": concept_id, "entry_id": entry_id, "version": "007",
                         "title": f"NASA {short_name} photon data", "summary": "Photon height data.\nIce — 冰"}],
        "candidate_signatures": [
            {"term": term, "review_status": "unreviewed", "sources": [{"concept_id": concept_id, "source_field": field}]}
            for term, field in ((short_name, "short_name"), (entry_id, "entry_id"), (concept_id, "id"))
        ],
    }


def save_pipeline_catalog(parent: Path, products: list[dict], status: str = "complete") -> Path:
    """Write a temporary finished catalog for pure export and planning tests.

    Args:
        parent: Test-owned temporary directory.
        products: Original product dictionaries to serialize without modification.
        status: Catalog completion state.

    Returns:
        Path to the new fixture catalog.
    """
    directory = parent / "catalog"
    directory.mkdir()
    summary = {
        "schema_version": 1, "status": status, "products_saved": len(products),
        "scope": "Public EOSDIS collections tagged gov.nasa.eosdis",
        "source_url": "https://cmr.earthdata.nasa.gov/search/collections.json?tag_key=gov.nasa.eosdis&page_size=2000",
        "started_at": "2026-10-15T10:00:00+00:00", "finished_at": "2026-10-15T10:01:00+00:00",
        "warnings": [] if status == "complete" else ["Catalog stopped early."], "error": "",
    }
    (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (directory / "products.jsonl").write_text("\n".join(json.dumps(product) for product in products), encoding="utf-8")
    return directory


class PipelineCatalogExportTests(unittest.TestCase):
    """Keep the readable inventory grounded in saved metadata and safe to export."""

    def test_export_has_one_row_per_product_with_name_description_and_provenance(self) -> None:
        """Combine versions while preserving original terms, descriptions, and URL."""
        product = make_pipeline_product()
        second_collection = {**product["collections"][0], "concept_id": "C-SECOND", "title": "Earlier photon data", "version": "006"}
        product["collections"].extend([second_collection, {**second_collection, "concept_id": "C-THIRD"}])
        with TemporaryDirectory() as temporary:
            directory = save_pipeline_catalog(Path(temporary), [product])
            original_files = {path.name: path.read_bytes() for path in directory.iterdir()}
            output = Path(temporary) / "datasets.csv"
            rows = export_dataset_table(directory, output)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["dataset_name"], "NASA ATL03 photon data; Earlier photon data")
            self.assertEqual(row["description"], "Photon height data.\nIce — 冰")
            self.assertEqual(row["source_url"], json.loads((directory / "summary.json").read_text())["source_url"])
            self.assertIn("C-SECOND (version 006)", row["known_from"])
            self.assertEqual(row["candidate_search_terms"].splitlines(), ["ATL03", "ATL03_007", "C-NSIDC-ATL03"])
            self.assertIn("ATL03_007 [unreviewed]: C-NSIDC-ATL03 (entry_id)", row["candidate_sources"])
            self.assertTrue(output.read_bytes().startswith(b"\xef\xbb\xbf"))
            with output.open(encoding="utf-8-sig", newline="") as csv_input:
                reader = csv.DictReader(csv_input)
                self.assertEqual(reader.fieldnames, list(DATASET_COLUMNS))
                self.assertEqual(list(reader), rows)
            self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, original_files)

    def test_formula_protection_is_applied_once_only_to_written_csv(self) -> None:
        """Return unescaped metadata while preventing spreadsheet formula execution."""
        product = make_pipeline_product()
        product["collections"][0]["title"] = '=HYPERLINK("unsafe")'
        product["collections"][0]["summary"] = "+SUM(1,2)"
        with TemporaryDirectory() as temporary:
            directory = save_pipeline_catalog(Path(temporary), [product])
            output = Path(temporary) / "datasets.csv"
            rows = export_dataset_table(directory, output)
            self.assertEqual(rows[0]["dataset_name"], '=HYPERLINK("unsafe")')
            with output.open(encoding="utf-8-sig", newline="") as csv_input:
                written = next(csv.DictReader(csv_input))
            self.assertEqual(written["dataset_name"], 'Text: =HYPERLINK("unsafe")')
            self.assertEqual(written["description"], "Text: +SUM(1,2)")
            self.assertNotIn("Text: Text:", output.read_text(encoding="utf-8-sig"))

    def test_export_refuses_existing_output_and_removes_its_own_partial_file(self) -> None:
        """Preserve existing exports and clean up only newly created failed output."""
        with TemporaryDirectory() as temporary:
            directory = save_pipeline_catalog(Path(temporary), [make_pipeline_product()])
            output = Path(temporary) / "datasets.csv"
            output.write_text("User's original export", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                export_dataset_table(directory, output)
            self.assertEqual(output.read_text(), "User's original export")
            failed_output = Path(temporary) / "failed.csv"
            with patch("nasa_eo_search.pipeline_catalog.csv.DictWriter.writerow", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    export_dataset_table(directory, failed_output)
            self.assertFalse(failed_output.exists())

    def test_partial_catalog_status_and_warnings_are_visible_in_inventory(self) -> None:
        """Do not present a stopped catalog as a complete NASA inventory."""
        with TemporaryDirectory() as temporary:
            directory = save_pipeline_catalog(Path(temporary), [make_pipeline_product()], "interrupted")
            rows = export_dataset_table(directory, Path(temporary) / "datasets.csv")
            self.assertIn("Catalog status: interrupted", rows[0]["known_from"])
            self.assertIn("Catalog stopped early", rows[0]["known_from"])

    def test_empty_catalog_exports_headers_without_inventing_products(self) -> None:
        """Allow a zero-row inventory but reject starting an empty search plan."""
        with TemporaryDirectory() as temporary:
            directory = save_pipeline_catalog(Path(temporary), [])
            output = Path(temporary) / "datasets.csv"
            self.assertEqual(export_dataset_table(directory, output), [])
            with output.open(encoding="utf-8-sig", newline="") as csv_input:
                reader = csv.DictReader(csv_input)
                self.assertEqual(reader.fieldnames, list(DATASET_COLUMNS))
                self.assertEqual(list(reader), [])
            with self.assertRaisesRegex(ValueError, "no products"):
                build_catalog_search_plan(directory)


class PipelineCatalogPlanTests(unittest.TestCase):
    """Plan every collected metadata identifier without spreadsheet selection."""

    def test_default_plan_uses_all_products_and_all_saved_candidate_terms(self) -> None:
        """Preserve exact terms and use the existing identifier-boundary query builder."""
        products = [make_pipeline_product("ATL03"), make_pipeline_product("ATL06")]
        with TemporaryDirectory() as temporary:
            directory = save_pipeline_catalog(Path(temporary), products)
            original_files = {path.name: path.read_bytes() for path in directory.iterdir()}
            with patch("nasa_eo_search.catalog.fetch_cmr_collection_page") as network:
                plan = build_catalog_search_plan(directory)
            network.assert_not_called()
            self.assertEqual(len(plan["jobs"]), 6)
            self.assertEqual({job["term"] for job in plan["jobs"]},
                             {candidate["term"] for product in products for candidate in product["candidate_signatures"]})
            self.assertEqual(plan["report_unit"], "file")
            self.assertEqual(plan["selection_mode"], "all_collected_products")
            self.assertIsNone(plan["selection_path"])
            self.assertIn("public GitHub", plan["scope"])
            for job in plan["jobs"]:
                self.assertEqual(job["query"], build_sourcegraph_product_query(job["term"], False, collect_all=True))
                self.assertEqual(job["search_id"], identify_search_query(job["query"]))
                self.assertIn("fork:yes archived:yes count:all", job["query"])
                association = job["products"][0]
                self.assertEqual(association["dataset_name"], f"NASA {association['short_name']} photon data")
            validate_saved_batch_plan(plan)
            self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, original_files)

    def test_default_plan_has_no_twenty_query_limit(self) -> None:
        """Plan a larger catalog without silently dropping later product terms."""
        products = [make_pipeline_product(f"PRODUCT{number:02d}") for number in range(25)]
        with TemporaryDirectory() as temporary:
            directory = save_pipeline_catalog(Path(temporary), products)
            plan = build_catalog_search_plan(directory)
            self.assertEqual(len(plan["jobs"]), 75)
            self.assertTrue(any(job["term"] == "PRODUCT24_007" for job in plan["jobs"]))

    def test_shared_terms_have_one_query_and_preserve_every_product_association(self) -> None:
        """Share generic metadata terms without losing their collection provenance."""
        products = [make_pipeline_product("ATL03", "FIRST"), make_pipeline_product("ATL03", "SECOND")]
        with TemporaryDirectory() as temporary:
            directory = save_pipeline_catalog(Path(temporary), products)
            plan = build_catalog_search_plan(directory)
            shared = next(job for job in plan["jobs"] if job["term"] == "ATL03")
            self.assertEqual(len(shared["products"]), 2)
            self.assertEqual({product["provider"] for product in shared["products"]}, {"FIRST", "SECOND"})
            for association in shared["products"]:
                self.assertEqual(association["candidate"]["sources"],
                                 [{"concept_id": f"C-{association['provider']}-ATL03", "source_field": "short_name"}])
            self.assertEqual(len(plan["jobs"]), 4)

    def test_exact_short_name_filter_includes_all_providers_and_deduplicates_filters(self) -> None:
        """Allow a small pilot without requiring a separate selection CSV."""
        products = [make_pipeline_product("ATL03", "FIRST"), make_pipeline_product("ATL03", "SECOND"),
                    make_pipeline_product("ATL06")]
        with TemporaryDirectory() as temporary:
            directory = save_pipeline_catalog(Path(temporary), products)
            plan = build_catalog_search_plan(directory, ["ATL03", "ATL03"])
            self.assertEqual(plan["selected_short_names"], ["ATL03"])
            self.assertEqual(plan["selection_mode"], "short_name_filter")
            self.assertTrue(all(product["short_name"] == "ATL03" for job in plan["jobs"] for product in job["products"]))
            self.assertEqual({product["provider"] for job in plan["jobs"] for product in job["products"]}, {"FIRST", "SECOND"})
            for names in (["atl03"], ["ATL03", "UNKNOWN"], [], [""], "ATL03"):
                with self.subTest(names=names), self.assertRaises(ValueError):
                    build_catalog_search_plan(directory, names)

    def test_all_hosts_changes_scope_only_when_explicitly_requested(self) -> None:
        """Keep public GitHub as the default without expanding search hosts silently."""
        with TemporaryDirectory() as temporary:
            directory = save_pipeline_catalog(Path(temporary), [make_pipeline_product()])
            plan = build_catalog_search_plan(directory, all_hosts=True)
            self.assertIn("all hosts", plan["scope"])
            self.assertTrue(all("repo:^github" not in job["query"] for job in plan["jobs"]))
            validate_saved_batch_plan(plan)
            with self.assertRaises(ValueError):
                build_catalog_search_plan(directory, all_hosts="true")

    def test_literal_punctuation_is_escaped_not_used_as_search_syntax(self) -> None:
        """Do not turn catalog metadata into additional repository filters or regex."""
        product = make_pipeline_product()
        product["candidate_signatures"][0]["term"] = 'ATL03.(x) repo:other "quoted"'
        with TemporaryDirectory() as temporary:
            directory = save_pipeline_catalog(Path(temporary), [product])
            plan = build_catalog_search_plan(directory)
            job = next(job for job in plan["jobs"] if job["term"] == product["candidate_signatures"][0]["term"])
            self.assertEqual(job["query"], build_sourcegraph_product_query(job["term"], False, collect_all=True))
            self.assertIn(r"ATL03\.\(x\)\x{20}repo:other", job["query"])

    def test_invalid_catalog_status_count_and_provenance_fail_before_output(self) -> None:
        """Reuse catalog validation rather than exporting or searching damaged input."""
        for problem in ("active", "count", "duplicate", "source", "version"):
            with self.subTest(problem=problem), TemporaryDirectory() as temporary:
                product = make_pipeline_product()
                products = [product, deepcopy(product)] if problem == "duplicate" else [product]
                if problem == "source":
                    product["candidate_signatures"][0]["sources"][0]["concept_id"] = "missing-collection"
                if problem == "version":
                    product["collections"][0]["version"] = 7
                directory = save_pipeline_catalog(Path(temporary), products, "running" if problem == "active" else "complete")
                if problem == "count":
                    summary_path = directory / "summary.json"
                    summary = json.loads(summary_path.read_text())
                    summary["products_saved"] = 99
                    summary_path.write_text(json.dumps(summary))
                output = Path(temporary) / "datasets.csv"
                with self.assertRaises(ValueError):
                    export_dataset_table(directory, output)
                self.assertFalse(output.exists())
                with self.assertRaises(ValueError):
                    build_catalog_search_plan(directory)

    def test_invalid_duplicate_or_missing_candidate_terms_are_not_silently_skipped(self) -> None:
        """Refuse unsearchable metadata instead of quietly narrowing all-product scope."""
        for problem in ("leading-space", "control", "duplicate", "missing"):
            with self.subTest(problem=problem), TemporaryDirectory() as temporary:
                product = make_pipeline_product()
                if problem == "leading-space":
                    product["candidate_signatures"][0]["term"] = " ATL03"
                elif problem == "control":
                    product["candidate_signatures"][0]["term"] = "ATL03\nOTHER"
                elif problem == "duplicate":
                    product["candidate_signatures"].append(deepcopy(product["candidate_signatures"][0]))
                else:
                    product["candidate_signatures"] = []
                directory = save_pipeline_catalog(Path(temporary), [product])
                with self.assertRaises(ValueError):
                    build_catalog_search_plan(directory)


if __name__ == "__main__":
    unittest.main()

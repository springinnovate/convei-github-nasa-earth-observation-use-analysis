"""Check fixed catalog-derived web query rules without network or key access."""

from copy import deepcopy
import unittest

from nasa_eo_search.web_queries import build_web_discovery_plan, select_web_context_words
from test_pipeline_catalog import make_pipeline_product


def make_web_query_catalog(short_name: str = "MOD13Q1", title: str = "MODIS/Terra Vegetation Indices 16-Day L3 Global 250m SIN Grid V061") -> dict:
    """Build one saved catalog object for bounded query planning tests.

    Args:
        short_name: Exact identifier to place in the product fixture.
        title: Collection title from which contextual words will be taken.

    Returns:
        Catalog dictionary with a collection and its saved CMR source URL.
    """
    product = make_pipeline_product(short_name)
    product["collections"][0]["title"] = title
    return {"summary": {"source_url": "https://cmr.earthdata.nasa.gov/search/collections.json"},
            "products": [product]}


class WebQueryPlanTests(unittest.TestCase):
    """Keep search inputs reproducible, bounded, and grounded in catalog metadata."""

    def test_plan_keeps_catalog_identity_title_provenance_and_fixed_limits(self) -> None:
        """Derive two queries without synthesizing aliases, versions, or subject terms."""
        catalog = make_web_query_catalog()
        original = deepcopy(catalog)
        plan = build_web_discovery_plan(catalog, ["MOD13Q1"], "gpt-6.1-sol")
        self.assertEqual(plan["queries"], [{
            "short_name": "MOD13Q1", "queries": [
                'site:github.com "MOD13Q1"', 'site:github.com "MOD13Q1" MODIS Terra Vegetation Indices',
            ], "context_title": catalog["products"][0]["collections"][0]["title"],
            "context_provider": "NSIDC", "context_concept_id": "C-NSIDC-MOD13Q1",
            "context_source_url": catalog["summary"]["source_url"],
        }])
        self.assertEqual(plan["model"], "gpt-6.1-sol")
        self.assertEqual(plan["query_policy"], "catalog-title-v1")
        self.assertEqual((plan["max_tool_calls"], plan["max_output_tokens"], plan["max_source_repositories"]), (2, 2000, 20))
        self.assertEqual(catalog, original)

    def test_selection_order_is_preserved_and_duplicates_collapse(self) -> None:
        """Keep one web request per selected canonical name even across providers."""
        catalog = make_web_query_catalog("ATL03", "Photon height")
        catalog["products"].extend([make_pipeline_product("ATL06"), make_pipeline_product("ATL03", "Z_PROVIDER")])
        plan = build_web_discovery_plan(catalog, ["ATL06", "ATL03", "ATL06"], "chosen-model")
        self.assertEqual([record["short_name"] for record in plan["queries"]], ["ATL06", "ATL03"])
        self.assertEqual(plan["queries"][1]["context_provider"], "NSIDC")

    def test_collection_order_cannot_change_context_selection(self) -> None:
        """Choose title provenance deterministically across reordered products and versions."""
        catalog = make_web_query_catalog("ATL03", "Later title")
        product = catalog["products"][0]
        product["collections"].append({"concept_id": "A-FIRST", "title": "First chosen title"})
        catalog["products"].append(make_pipeline_product("ATL03", "Z_PROVIDER"))
        plan = build_web_discovery_plan(catalog, ["ATL03"], "chosen-model")
        catalog["products"].reverse()
        for product in catalog["products"]:
            product["collections"].reverse()
        self.assertEqual(build_web_discovery_plan(catalog, ["ATL03"], "chosen-model"), plan)
        self.assertEqual(plan["queries"][0]["context_concept_id"], "A-FIRST")

    def test_blank_or_absent_title_keeps_one_canonical_query(self) -> None:
        """Support old metadata without duplicating an empty contextual query."""
        for title in ("", "NASA MOD13Q1 Level 3 Global Grid V061"):
            with self.subTest(title=title):
                catalog = make_web_query_catalog(title=title)
                plan = build_web_discovery_plan(catalog, ["MOD13Q1"], "chosen-model")
                self.assertEqual(plan["queries"][0]["queries"], ['site:github.com "MOD13Q1"'])
        del catalog["products"][0]["collections"][0]["title"]
        self.assertEqual(len(build_web_discovery_plan(catalog, ["MOD13Q1"], "chosen-model")["queries"][0]["queries"]), 1)

    def test_context_words_are_literal_distinct_and_bounded(self) -> None:
        """Keep six source words and strip punctuation that could become query syntax."""
        self.assertEqual(select_web_context_words(
            "GHRSST Level 4 MUR Global Foundation Sea Surface Temperature Analysis (v4.1)", "MUR-ID"),
            ["GHRSST", "MUR", "Foundation", "Sea", "Surface", "Temperature"])
        self.assertEqual(select_web_context_words('NASA ATL03 "Photon" photon HEIGHT; height level:3', "ATL03"),
                         ["Photon", "HEIGHT"])
        self.assertEqual(select_web_context_words("全球 250m V061 L3 16-Day", "MOD13Q1"), [])

    def test_quoted_identifiers_escape_literal_backslashes(self) -> None:
        """Prevent a trailing backslash from consuming the query's closing quote."""
        catalog = make_web_query_catalog("DATA\\", "")
        query = build_web_discovery_plan(catalog, ["DATA\\"], "chosen-model")["queries"][0]["queries"][0]
        self.assertEqual(query, 'site:github.com "DATA\\\\"')

    def test_catalog_metadata_does_not_invent_older_versions(self) -> None:
        """Do not derive a known control's historical version from a newer catalog version."""
        catalog = make_web_query_catalog("SPL3SMP", "SMAP L3 Radiometer Global Daily 36 km EASE-Grid Soil Moisture V009")
        catalog["products"][0]["collections"][0]["version"] = "009"
        queries = build_web_discovery_plan(catalog, ["SPL3SMP"], "chosen-model")["queries"][0]["queries"]
        self.assertEqual(queries, ['site:github.com "SPL3SMP"',
                                   'site:github.com "SPL3SMP" SMAP Radiometer EASE Soil Moisture'])

    def test_invalid_model_and_selection_are_rejected(self) -> None:
        """Reject malformed model/identifier input before any service can be called."""
        catalog = make_web_query_catalog()
        for model in (None, 1, "", " ", "leading space", "tab\tmodel", "model\n", "model\x00", "gpt–model", "模型", "x" * 201):
            with self.subTest(model=repr(model)), self.assertRaisesRegex(ValueError, "model"):
                build_web_discovery_plan(catalog, ["MOD13Q1"], model)
        for selection in (None, "MOD13Q1", [], ["missing"], [None], [" MOD13Q1"], ['bad"term'], ["bad\tterm"]):
            with self.subTest(selection=repr(selection)), self.assertRaises(ValueError):
                build_web_discovery_plan(catalog, selection, "chosen-model")

    def test_malformed_query_metadata_is_rejected(self) -> None:
        """Fail clearly on missing products, invalid provenance, and nontext titles."""
        malformed_catalogs = [None, {}, {"products": None}, {"products": [None]},
                              {"products": [{"short_name": None}]}, {"products": [], "summary": None}]
        for field, value in (("provider", None), ("provider", ""), ("collections", []), ("collections", None)):
            catalog = make_web_query_catalog()
            catalog["products"][0][field] = value
            malformed_catalogs.append(catalog)
        for field, value in (("concept_id", None), ("concept_id", ""), ("title", 42)):
            catalog = make_web_query_catalog()
            catalog["products"][0]["collections"][0][field] = value
            malformed_catalogs.append(catalog)
        for catalog in malformed_catalogs:
            with self.subTest(catalog=catalog), self.assertRaises(ValueError):
                build_web_discovery_plan(catalog, ["MOD13Q1"], "chosen-model")


if __name__ == "__main__":
    unittest.main()

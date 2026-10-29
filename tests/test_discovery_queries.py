"""Check cited discovery aliases, deterministic requests, and legacy services."""

import csv
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from nasa_eo_search.discovery_queries import (
    build_discovery_queries, discovery_providers, discovery_query_terms,
    read_discovery_aliases, validate_discovery_alias_record,
)


def make_discovery_alias(short_name: str = "MOD13Q1", term: str = "MODIS/061/MOD13Q1",
                         source_url: str = "https://example.org/datasets/mod13q1") -> dict[str, str]:
    """Create one exact discovery alias for offline tests.

    Args:
        short_name: Catalog product owning the alias.
        term: Literal search term.
        source_url: Supporting public documentation URL.

    Returns:
        A short_name/term/source_url record.
    """
    return {"short_name": short_name, "term": term, "source_url": source_url}


def save_discovery_alias_csv(directory: Path, rows: list[list[str]],
                             headers: list[str] | None = None) -> Path:
    """Write a UTF-8 CSV fixture without contacting any service.

    Args:
        directory: Test-owned temporary output directory.
        rows: Values to write after the header.
        headers: Optional header override for malformed-input tests.

    Returns:
        Path to the newly written alias CSV.
    """
    path = directory / "aliases.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as alias_output:
        writer = csv.writer(alias_output)
        writer.writerow(headers if headers is not None else ["short_name", "term", "source_url"])
        writer.writerows(rows)
    return path


class DiscoveryQueryTests(unittest.TestCase):
    """Preserve catalog associations and provenance without inventing aliases."""

    def test_alias_csv_sorts_records_and_collapses_identical_duplicates(self) -> None:
        """Keep exact term spelling and deterministic order across CSV row orders."""
        first = make_discovery_alias()
        second = make_discovery_alias("MUR", "jplMURSST41", "https://example.org/mur?q=4.1#source")
        with TemporaryDirectory() as temporary_directory:
            path = save_discovery_alias_csv(Path(temporary_directory),
                                           [list(second.values()), list(first.values()), list(first.values())])
            original_bytes = path.read_bytes()
            self.assertEqual(read_discovery_aliases(path, {"MOD13Q1", "MUR"}), [first, second])
            self.assertEqual(path.read_bytes(), original_bytes)

    def test_alias_csv_requires_exact_headers_and_complete_rows(self) -> None:
        """Reject reordered, duplicate, missing, extra, and empty-header inputs."""
        bad_headers = [[], ["term", "short_name", "source_url"], ["short_name", "term"],
                       ["short_name", "term", "source_url", "notes"], ["short_name", "term", "term"]]
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for headers in bad_headers:
                with self.subTest(headers=headers):
                    path = save_discovery_alias_csv(directory, [], headers)
                    with self.assertRaisesRegex(ValueError, "headers"):
                        read_discovery_aliases(path, {"MOD13Q1"})
            for row in (["MOD13Q1", "alias"], ["MOD13Q1", "alias", "https://example.org", "extra"]):
                with self.subTest(row=row):
                    path = save_discovery_alias_csv(directory, [row])
                    with self.assertRaisesRegex(ValueError, "line 2"):
                        read_discovery_aliases(path, {"MOD13Q1"})

    def test_alias_csv_rejects_unknown_names_invalid_terms_and_conflicting_sources(self) -> None:
        """Keep aliases within the catalog and require one provenance per pair."""
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for term in ("", 'bad"quote', "line\nbreak", "bad\rterm", "bad\x00term", "bad\tterm", "bad\x01term", "x" * 257):
                with self.subTest(term=repr(term)):
                    path = save_discovery_alias_csv(directory, [["MOD13Q1", term, "https://example.org"]])
                    with self.assertRaises(ValueError):
                        read_discovery_aliases(path, {"MOD13Q1"})
            path = save_discovery_alias_csv(directory, [["UNKNOWN", "alias", "https://example.org"]])
            with self.assertRaisesRegex(ValueError, "catalog names"):
                read_discovery_aliases(path, {"MOD13Q1"})
            path = save_discovery_alias_csv(directory, [["MOD13Q1", "alias", "https://example.org/one"],
                                                       ["MOD13Q1", "alias", "https://example.org/two"]])
            with self.assertRaisesRegex(ValueError, "conflicting provenance"):
                read_discovery_aliases(path, {"MOD13Q1"})

    def test_alias_sources_require_absolute_https_without_credentials(self) -> None:
        """Reject unsupported schemes, relative URLs, user info, and malformed ports."""
        for source_url in ("", "http://example.org", "file:///tmp/data", "//example.org/data", "/data",
                           "https:///data", "https://user@example.org/data", "https://user:password@example.org",
                           "https://example.org:abc/data", "https://example.org:65536/data", "https://[bad",
                           "https://example.org/a b", "https://example.org/line\nbreak"):
            with self.subTest(source_url=source_url):
                with self.assertRaisesRegex(ValueError, "absolute HTTPS"):
                    validate_discovery_alias_record(make_discovery_alias(source_url=source_url), {"MOD13Q1"})
        alias = make_discovery_alias(source_url="https://example.org:443/data?q=1#identifier")
        self.assertEqual(validate_discovery_alias_record(alias, {"MOD13Q1"}), alias)

    def test_alias_csv_rejects_broken_quoting_and_invalid_utf8(self) -> None:
        """Turn malformed file content into a clear validation error."""
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "aliases.csv"
            for content in (b'short_name,term,source_url\nMOD13Q1,"unterminated\n', b"\xff\xfe\x80"):
                with self.subTest(content=content):
                    path.write_bytes(content)
                    with self.assertRaisesRegex(ValueError, "UTF-8"):
                        read_discovery_aliases(path, {"MOD13Q1"})

    def test_query_builder_keeps_canonical_terms_first_and_alias_ownership(self) -> None:
        """Retain every selected product while deduplicating identical per-product terms."""
        shared_first = make_discovery_alias("MOD13Q1", "shared", "https://example.org/shared")
        shared_second = make_discovery_alias("MUR", "shared", "https://example.org/shared")
        queries = build_discovery_queries(["MUR", "MOD13Q1", "MUR"], [shared_first, shared_second,
                    make_discovery_alias(), shared_first, make_discovery_alias("MUR", "MUR")])
        self.assertEqual(queries, [
            {"short_name": "MUR", "term": "MUR", "source_url": ""}, shared_second,
            {"short_name": "MOD13Q1", "term": "MOD13Q1", "source_url": ""},
            make_discovery_alias(), shared_first,
        ])
        self.assertEqual(discovery_query_terms({"discovery_queries": queries}),
                         ["MUR", "MOD13Q1", "shared", "MODIS/061/MOD13Q1"])

    def test_query_builder_rejects_aliases_for_unselected_or_malformed_records(self) -> None:
        """Validate saved alias records as strictly as new CSV input."""
        bad_aliases = [[None], [{"short_name": "MOD13Q1"}],
                       [{**make_discovery_alias(), "unexpected": "extra"}],
                       [{**make_discovery_alias(), "source_url": 123}],
                       [make_discovery_alias("MUR")],
                       [make_discovery_alias(), make_discovery_alias(source_url="https://example.org/different")]]
        for aliases in bad_aliases:
            with self.subTest(aliases=aliases):
                with self.assertRaises(ValueError):
                    build_discovery_queries(["MOD13Q1"], aliases)
        self.assertEqual(build_discovery_queries(["MUR"], []),
                         [{"short_name": "MUR", "term": "MUR", "source_url": ""}])

    def test_request_terms_preserve_legacy_order_and_reject_malformed_plans(self) -> None:
        """Reuse old discovery terms and validate new saved query shapes."""
        self.assertEqual(discovery_query_terms({"discovery_terms": ["ATL03", "ATL06", "ATL03"]}), ["ATL03", "ATL06"])
        for plan in ({}, {"discovery_terms": "ATL03"}, {"discovery_queries": None},
                     {"discovery_queries": ["ATL03"]}, {"discovery_queries": [{}]},
                     {"discovery_queries": [{"term": 'bad"quote'}]},
                     {"discovery_queries": [{"short_name": "ATL03", "term": "alias"}]},
                     {"discovery_terms": "ATL03", "discovery_queries": []},
                     {"discovery_terms": ["ATL03"], "discovery_queries": []},
                     {"discovery_terms": ["ATL03"],
                      "discovery_queries": [{"short_name": "ATL06", "term": "ATL06"}]}):
            with self.subTest(plan=plan):
                with self.assertRaises(ValueError):
                    discovery_query_terms(plan)

    def test_request_budget_reaches_every_canonical_name_before_aliases(self) -> None:
        """Prioritize five products' own names despite a long first-product alias list."""
        names = ["ATL03", "MOD13Q1", "SPL3SMP", "SRTMGL1", "MUR"]
        aliases = [make_discovery_alias("ATL03", f"first-alias-{index}") for index in range(6)]
        aliases.append(make_discovery_alias("MUR", "jplMURSST41"))
        queries = build_discovery_queries(names, aliases)
        plan = {"discovery_terms": names, "discovery_queries": queries}
        frozen_plan = copy.deepcopy(plan)
        scheduled = discovery_query_terms(plan)
        self.assertEqual(scheduled[:5], names)
        self.assertEqual(scheduled[5:8], ["first-alias-0", "jplMURSST41", "first-alias-1"])
        self.assertEqual(plan, frozen_plan)

    def test_request_alias_round_robin_skips_shared_and_canonical_duplicates(self) -> None:
        """Keep one request per term while giving each dataset its next unique alias."""
        names = ["FIRST", "SECOND", "THIRD"]
        aliases = [make_discovery_alias("FIRST", "A_SHARED"),
                   make_discovery_alias("FIRST", "B_FIRST"),
                   make_discovery_alias("FIRST", "SECOND"),
                   make_discovery_alias("SECOND", "A_SHARED"),
                   make_discovery_alias("SECOND", "C_SECOND"),
                   make_discovery_alias("THIRD", "D_THIRD")]
        queries = build_discovery_queries(names, aliases)
        self.assertEqual(discovery_query_terms({"discovery_terms": names, "discovery_queries": queries}),
                         ["FIRST", "SECOND", "THIRD", "A_SHARED", "C_SECOND", "D_THIRD", "B_FIRST"])

    def test_canonical_request_order_uses_saved_selection_not_query_record_order(self) -> None:
        """Honor frozen selected-name order independently of grouped query records."""
        queries = build_discovery_queries(["FIRST", "SECOND"],
                    [make_discovery_alias("FIRST", "FIRST_ALIAS"),
                     make_discovery_alias("SECOND", "SECOND_ALIAS")])
        self.assertEqual(discovery_query_terms({"discovery_terms": ["SECOND", "FIRST"],
                                               "discovery_queries": queries}),
                         ["SECOND", "FIRST", "SECOND_ALIAS", "FIRST_ALIAS"])

    def test_discovery_providers_preserve_old_mode_and_append_optional_grep(self) -> None:
        """Select repository discovery before code while keeping legacy defaults."""
        self.assertEqual(discovery_providers({}), ("github",))
        self.assertEqual(discovery_providers({"include_grep_app": True}), ("github", "grep.app"))
        self.assertEqual(discovery_providers({"github_discovery": "code"}), ("github",))
        self.assertEqual(discovery_providers({"github_discovery": "code-and-readme"}), ("github-repositories", "github"))
        self.assertEqual(discovery_providers({"github_discovery": "code-and-readme", "include_grep_app": True}),
                         ("github-repositories", "github", "grep.app"))
        for plan in ({"github_discovery": "unknown"}, {"github_discovery": None},
                     {"github_discovery": []}, {"include_grep_app": 1}):
            with self.subTest(plan=plan):
                with self.assertRaises(ValueError):
                    discovery_providers(plan)


if __name__ == "__main__":
    unittest.main()

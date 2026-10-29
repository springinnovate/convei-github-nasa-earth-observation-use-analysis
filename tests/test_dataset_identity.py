"""Check catalog-aware identifier ownership independently of model judgments."""

import unittest
from unittest.mock import patch

from nasa_eo_search.dataset_identity import (
    build_catalog_identity_rules, find_ascii_identifier_occurrences,
    find_authoritative_identifier_occurrences,
    find_product_identifier_occurrences,
)


def create_identity_product(short_name: str, *, provider: str = "NASA", entry_ids: tuple[str, ...] = (),
                            extra_candidates: tuple[str, ...] = ()) -> dict:
    """Build a small catalog product with explicit authoritative identifiers.

    Args:
        short_name: Catalog product name and its ordinary search term.
        provider: Product provider used to distinguish catalog groups.
        entry_ids: Additional collection entry IDs belonging to this product.
        extra_candidates: Search-only strings, not authoritative identifiers.

    Returns:
        Catalog-like product record accepted by the identity-rule builder.
    """
    return {"provider": provider, "short_name": short_name,
            "collections": [{"entry_id": entry_id, "concept_id": f"C{number}-{short_name}"}
                            for number, entry_id in enumerate(entry_ids, 1)],
            "candidate_signatures": [{"term": term}
                                     for term in (short_name, *entry_ids, *extra_candidates)]}


class DatasetIdentityTests(unittest.TestCase):
    """Preserve valid aliases while excluding occurrences of another product."""

    def test_enhanced_product_does_not_establish_base_product_identity(self) -> None:
        """Exclude only the base occurrence inside the known enhanced identifier."""
        products = [create_identity_product("SPL3SMP"), create_identity_product("SPL3SMP_E")]
        rules = build_catalog_identity_rules(products)
        source = "ee.ImageCollection('NASA/SMAP/SPL3SMP_E/006')"
        self.assertEqual(find_product_identifier_occurrences("SPL3SMP", source, rules[("NASA", "SPL3SMP")]), [])
        enhanced = find_product_identifier_occurrences("SPL3SMP_E", source, rules[("NASA", "SPL3SMP_E")])
        self.assertEqual([source[start:end] for start, end in enhanced], ["SPL3SMP_E"])

    def test_independent_base_occurrence_survives_on_the_same_line(self) -> None:
        """Keep the standalone base name alongside a separately matched sibling."""
        rules = build_catalog_identity_rules([
            create_identity_product("SPL3SMP"), create_identity_product("SPL3SMP_E"),
        ])[("NASA", "SPL3SMP")]
        source = "SPL3SMP_E/006 and SPL3SMP/009 and SPL3SMP_E_006.h5"
        start = source.index("SPL3SMP/009")
        self.assertEqual(find_product_identifier_occurrences("SPL3SMP", source, rules), [(start, start + 7)])

    def test_markdown_link_label_and_target_both_identify_the_enhanced_product(self) -> None:
        """Exclude escaped label and literal URL occurrences without altering the source."""
        rules = build_catalog_identity_rules([
            create_identity_product("SPL3SMP"), create_identity_product("SPL3SMP_E"),
        ])[("NASA", "SPL3SMP")]
        source = (r"[https://nsidc.org/data/data-access-tool/SPL3SMP\_E/versions/6]"
                  r"(https://nsidc.org/data/data-access-tool/SPL3SMP_E/versions/6)")
        self.assertEqual(len(find_ascii_identifier_occurrences("SPL3SMP", source)), 2)
        self.assertEqual(find_product_identifier_occurrences("SPL3SMP", source, rules), [])
        self.assertEqual([source[start:end] for start, end in
                          find_authoritative_identifier_occurrences("SPL3SMP_E", source)],
                         [r"SPL3SMP\_E", "SPL3SMP_E"])

    def test_escaped_identifiers_keep_case_and_raw_unicode_offsets(self) -> None:
        """Retain the genuine base hit after mixed-case escaped text and Unicode."""
        rules = build_catalog_identity_rules([
            create_identity_product("SPL3SMP"), create_identity_product("SPL3SMP_E"),
        ])[("NASA", "SPL3SMP")]
        source = r"İ 冰 spl3smp\_e/006 → SPL3SMP/009"
        start = source.index("SPL3SMP/009")
        self.assertEqual(find_product_identifier_occurrences("SPL3SMP", source, rules), [(start, start + 7)])

    def test_escaped_own_alias_preserves_the_original_literal_base_hit(self) -> None:
        """Recognize an escaped same-product entry without synthesizing alias hits."""
        rules = build_catalog_identity_rules([
            create_identity_product("BASE", entry_ids=("BASE_OWN-ALIAS",)),
        ])[("NASA", "BASE")]
        source = r"BASE\_OWN\-ALIAS"
        self.assertEqual(find_product_identifier_occurrences("BASE", source, rules), [(0, 4)])
        self.assertEqual(find_product_identifier_occurrences("BASE_OWN-ALIAS", source, rules), [])
        self.assertEqual(find_ascii_identifier_occurrences("BASE_OWN-ALIAS", source), [])

    def test_escaped_punctuation_does_not_decode_backslashes_before_letters(self) -> None:
        """Do not reinterpret unrelated backslashes as a known identifier."""
        rules = build_catalog_identity_rules([
            create_identity_product("BASE"), create_identity_product("BASE_EXTENDED"),
        ])[("NASA", "BASE")]
        for source in (r"BASE\x_EXTENDED", r"BASE\\_EXTENDED"):
            with self.subTest(source=source):
                self.assertEqual(find_product_identifier_occurrences("BASE", source, rules), [(0, 4)])

    def test_many_repeated_siblings_leave_only_the_separate_base_occurrence(self) -> None:
        """Handle a long line's repeated foreign spans without losing its real match."""
        rules = build_catalog_identity_rules([
            create_identity_product("BASE"), create_identity_product("BASE_E"),
        ])[("NASA", "BASE")]
        source = (r"BASE\_E " * 5000) + "BASE"
        self.assertEqual(find_product_identifier_occurrences("BASE", source, rules),
                         [(len(source) - 4, len(source))])

    def test_unknown_suffixes_and_granule_versions_remain_candidates(self) -> None:
        """Do not ban underscore filenames or assume uncataloged suffixes are siblings."""
        rules = build_catalog_identity_rules([
            create_identity_product("ATL03", entry_ids=("ATL03_007",)),
            create_identity_product("ATL03_E"),
        ])[("NASA", "ATL03")]
        for source in ("ATL03_20181017222812_02950102_003_01.h5", "ATL03_007.h5", "ATL03_UNKNOWN"):
            with self.subTest(source=source):
                self.assertEqual(find_product_identifier_occurrences("ATL03", source, rules), [(0, 5)])
        self.assertEqual(find_product_identifier_occurrences("ATL03", "MATL03 ATL030", rules), [])

    def test_same_product_aliases_are_not_discarded(self) -> None:
        """Treat a longer authoritative entry ID as evidence for its own product."""
        rules = build_catalog_identity_rules([create_identity_product("BASE", entry_ids=("BASE_ALTERNATE",))])
        self.assertIn({"term": "BASE_ALTERNATE", "same_product": True}, rules[("NASA", "BASE")]["BASE"])
        self.assertTrue(all(identifier["same_product"] for identifier in rules[("NASA", "BASE")]["BASE"]))
        self.assertEqual(find_product_identifier_occurrences("BASE", "BASE_ALTERNATE", rules[("NASA", "BASE")]), [(0, 4)])

    def test_maximal_identifier_restores_its_owner_through_a_foreign_middle_name(self) -> None:
        """Let the outer identifier determine identity across a chain of prefixes."""
        rules = build_catalog_identity_rules([
            create_identity_product("BASE", entry_ids=("BASE_E_F",)),
            create_identity_product("BASE_E"),
        ])
        self.assertEqual(find_product_identifier_occurrences("BASE", "BASE_E_F", rules[("NASA", "BASE")]), [(0, 4)])
        self.assertEqual(find_product_identifier_occurrences("BASE_E", "BASE_E_F", rules[("NASA", "BASE_E")]), [])
        self.assertEqual(find_product_identifier_occurrences("BASE", "BASE_E", rules[("NASA", "BASE")]), [])

    def test_shared_longer_identifiers_preserve_every_catalog_owner(self) -> None:
        """Keep ambiguity when a full alias is recorded for more than one product."""
        products = [create_identity_product("BASE", provider="FIRST", entry_ids=("BASE_SHARED",)),
                    create_identity_product("BASE", provider="SECOND", entry_ids=("BASE_SHARED",)),
                    create_identity_product("OTHER", entry_ids=("BASE_SHARED",))]
        rules = build_catalog_identity_rules(products)
        for provider in ("FIRST", "SECOND"):
            with self.subTest(provider=provider):
                self.assertEqual(find_product_identifier_occurrences("BASE", "BASE_SHARED", rules[(provider, "BASE")]), [(0, 4)])

    def test_search_only_candidate_cannot_invent_an_authoritative_sibling(self) -> None:
        """Ignore unsupported candidate aliases when resolving another product's name."""
        rules = build_catalog_identity_rules([
            create_identity_product("BASE"),
            create_identity_product("OTHER", extra_candidates=("BASE_FAKE",)),
        ])
        self.assertEqual(rules[("NASA", "BASE")], {})
        self.assertEqual(find_product_identifier_occurrences("BASE", "BASE_FAKE", rules[("NASA", "BASE")]), [(0, 4)])

    def test_concept_ids_are_authoritative_but_partial_alphanumeric_names_are_not(self) -> None:
        """Recognize actual collection IDs while retaining the scanner's boundaries."""
        product = create_identity_product("BASE")
        other = create_identity_product("OTHER")
        other["collections"] = [{"concept_id": "C100-BASE", "entry_id": "ABASE"}]
        rules = build_catalog_identity_rules([product, other])[("NASA", "BASE")]
        self.assertEqual(rules["BASE"], [{"term": "C100-BASE", "same_product": False}])
        self.assertEqual(find_product_identifier_occurrences("BASE", "C100-BASE", rules), [])
        self.assertEqual(find_product_identifier_occurrences("BASE", "ABASE", rules), [])

    def test_ascii_case_folding_preserves_unicode_character_offsets(self) -> None:
        """Keep original source positions when Unicode letters precede mixed-case terms."""
        rules = build_catalog_identity_rules([
            create_identity_product("SPL3SMP"), create_identity_product("spl3smp_E"),
        ])[("NASA", "SPL3SMP")]
        source = "İ 冰 spl3smp_e/006 → spl3smp_009"
        start = source.index("spl3smp_009")
        self.assertEqual(find_product_identifier_occurrences("spl3smp", source, rules), [(start, start + 7)])
        self.assertEqual(find_ascii_identifier_occurrences("i", "İ ı"), [])

    def test_suffix_occurrences_and_overlapping_literals_keep_exact_ranges(self) -> None:
        """Find suffix aliases and overlapping punctuation-separated terms precisely."""
        rules = build_catalog_identity_rules([
            create_identity_product("BASE"), create_identity_product("OTHER_BASE"),
        ])[("NASA", "BASE")]
        self.assertEqual(find_product_identifier_occurrences("BASE", "OTHER_BASE BASE", rules), [(11, 15)])
        self.assertEqual(find_ascii_identifier_occurrences("A_A", "A_A_A"), [(0, 3), (2, 5)])

    def test_absent_rules_keep_the_literal_matching_contract(self) -> None:
        """Retain complete literal terms when there is no catalog disambiguation."""
        self.assertEqual(find_product_identifier_occurrences("ATL03", "MATL03 ATL03_007 ATL030"), [(7, 12)])
        self.assertEqual(find_product_identifier_occurrences("", "ATL03"), [])
        self.assertEqual(build_catalog_identity_rules([]), {})

    def test_large_catalog_uses_one_shared_matcher(self) -> None:
        """Build all identifier relations through one compiled multi-term matcher."""
        products = [create_identity_product(f"PRODUCT_{number:05d}") for number in range(1000)]
        from nasa_eo_search.dataset_identity import build_literal_term_matcher
        with patch("nasa_eo_search.dataset_identity.build_literal_term_matcher", wraps=build_literal_term_matcher) as matcher_builder:
            rules = build_catalog_identity_rules(products)
        matcher_builder.assert_called_once()
        self.assertEqual(len(rules), 1000)
        self.assertTrue(all(not product_rules for product_rules in rules.values()))

    def test_invalid_or_duplicate_product_identity_fails_explicitly(self) -> None:
        """Reject ambiguous catalog groups and invalid search terms before matching."""
        product = create_identity_product("BASE")
        with self.assertRaisesRegex(ValueError, "unique"):
            build_catalog_identity_rules([product, product])
        for bad_term in ("", None, "BASE\nOTHER", "BASE\x00"):
            invalid_product = create_identity_product("BASE")
            invalid_product["candidate_signatures"] = [{"term": bad_term}]
            with self.subTest(term=bad_term), self.assertRaisesRegex(ValueError, "single-line"):
                build_catalog_identity_rules([invalid_product])


if __name__ == "__main__":
    unittest.main()

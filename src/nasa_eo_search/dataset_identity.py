"""Associate literal code occurrences with the most specific catalog identifiers."""

import re
from string import punctuation
from typing import Any

from nasa_eo_search.repository_scan import (
    ASCII_ALPHANUMERIC, ASCII_LOWERCASE, build_literal_term_matcher,
    find_literal_terms_in_line,
)


def build_catalog_identity_rules(
    products: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, list[dict[str, Any]]]]:
    """Find longer catalog identifiers that can disambiguate each search term.

    Use product short names and collection entry/concept IDs as authoritative
    identifiers. Candidate signatures select the terms to assess, but cannot
    invent additional authoritative identifiers. Compile the candidate terms
    together, then scan each authoritative identifier once; the catalog is not
    compared with itself pair by pair.

    Include longer identifiers owned by this product as well as other products.
    This preserves aliases and permits an outer identifier to disambiguate a
    chain of nested names. An identifier shared by multiple catalog products
    remains compatible with each recorded owner.

    Args:
        products: Validated catalog product groups containing provider,
            short_name, collections, and candidate_signatures.

    Returns:
        Rules keyed by the exact provider/short-name pair. Each product maps
        search terms to longer identifier records containing term and
        same_product. Terms without longer identifiers are omitted. All case
        comparisons fold ASCII letters only.

    Raises:
        ValueError: A product identity is missing or duplicated, or a candidate
            term is empty, not text, or contains a newline or NUL character.
    """
    candidate_terms_by_product: dict[tuple[str, str], list[str]] = {}
    owners_by_identifier: dict[str, set[tuple[str, str]]] = {}
    identifier_spellings: dict[str, str] = {}
    for product in products:
        provider, short_name = product.get("provider"), product.get("short_name")
        if not isinstance(provider, str) or not provider or not isinstance(short_name, str) or not short_name:
            raise ValueError("Catalog identity rules require provider and short_name text.")
        product_key = (provider, short_name)
        if product_key in candidate_terms_by_product:
            raise ValueError("Catalog identity rules require unique product groups.")
        candidate_terms = [candidate.get("term") for candidate in product.get("candidate_signatures", [])]
        if any(not isinstance(term, str) or not term or any(character in term for character in "\r\n\x00")
               for term in candidate_terms):
            raise ValueError("Catalog identity search terms must be nonempty single-line text.")
        candidate_terms_by_product[product_key] = list(dict.fromkeys(candidate_terms))
        authoritative_identifiers = [short_name]
        for collection in product.get("collections", []):
            authoritative_identifiers.extend([collection.get("entry_id"), collection.get("concept_id")])
        for identifier in authoritative_identifiers:
            if not isinstance(identifier, str) or not identifier:
                continue
            normalized_identifier = identifier.translate(ASCII_LOWERCASE)
            owners_by_identifier.setdefault(normalized_identifier, set()).add(product_key)
            identifier_spellings[normalized_identifier] = min(
                identifier_spellings.get(normalized_identifier, identifier), identifier,
            )

    rules_by_product: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {
        product_key: {} for product_key in candidate_terms_by_product
    }
    normalized_terms = tuple(sorted({term.translate(ASCII_LOWERCASE)
                                     for terms in candidate_terms_by_product.values() for term in terms}))
    if not normalized_terms:
        return rules_by_product
    matcher = build_literal_term_matcher(normalized_terms)
    longer_identifiers_by_term: dict[str, list[str]] = {}
    for identifier in sorted(owners_by_identifier):
        for term in find_literal_terms_in_line(identifier, matcher):
            if len(term) < len(identifier):
                longer_identifiers_by_term.setdefault(term, []).append(identifier)
    for product_key, candidate_terms in candidate_terms_by_product.items():
        for term in candidate_terms:
            longer_identifiers = longer_identifiers_by_term.get(term.translate(ASCII_LOWERCASE), [])
            if longer_identifiers:
                rules_by_product[product_key][term] = [
                    {"term": identifier_spellings[identifier],
                     "same_product": product_key in owners_by_identifier[identifier]}
                    for identifier in longer_identifiers
                ]
    return rules_by_product


def find_ascii_identifier_occurrences(term: str, text: str) -> list[tuple[int, int]]:
    """Locate bounded literal identifiers without changing source offsets.

    Args:
        term: Literal identifier to find; an empty term has no occurrences.
        text: Original source text. Only ASCII letters are normalized for search.

    Returns:
        Zero-based, end-exclusive character ranges, including overlapping
        occurrences. Both ends must touch a text edge or a non-ASCII-alphanumeric
        character. Underscores therefore permit granule filename suffixes.
    """
    if not term:
        return []
    normalized_text, normalized_term = text.translate(ASCII_LOWERCASE), term.translate(ASCII_LOWERCASE)
    occurrences = []
    next_start = 0
    while True:
        start = normalized_text.find(normalized_term, next_start)
        if start < 0:
            break
        end = start + len(term)
        if ((start == 0 or text[start - 1] not in ASCII_ALPHANUMERIC)
                and (end == len(text) or text[end] not in ASCII_ALPHANUMERIC)):
            occurrences.append((start, end))
        next_start = start + 1
    return occurrences


def find_authoritative_identifier_occurrences(term: str, text: str) -> list[tuple[int, int]]:
    """Locate known catalog names written literally or with Markdown escapes.

    This helper only checks longer identifiers surrounding existing literal
    search hits. A backslash before ASCII punctuation, such as ``\\_``, may
    spell that punctuation in Markdown. Match those characters in place rather
    than decoding source text or creating new scanner hits. Ordinary source
    lines without backslashes keep the literal-matching path.

    Args:
        term: Authoritative catalog identifier used to disambiguate a short hit.
        text: Original source text, including any backslashes.

    Returns:
        Bounded, zero-based, end-exclusive ranges in the unmodified source.
        Ranges include any matched escape characters and may overlap.
    """
    if not term or "\\" not in text:
        return find_ascii_identifier_occurrences(term, text)
    escaped_identifier = "".join((r"(?:\\)?" if character in punctuation else "")
                                 + re.escape(character) for character in term)
    pattern = r"(?=(?<![A-Za-z0-9])(" + escaped_identifier + r")(?![A-Za-z0-9]))"
    return [match.span(1) for match in re.finditer(pattern, text, re.IGNORECASE | re.ASCII)]


def find_product_identifier_occurrences(
    term: str, text: str,
    identity_rules: dict[str, list[dict[str, Any]]] | None = None,
) -> list[tuple[int, int]]:
    """Keep literal occurrences compatible with the most specific catalog name.

    A shorter term inside a longer known identifier belongs to the product only
    when at least one maximal containing identifier has that product as an
    owner. A separate occurrence elsewhere in the same line remains eligible.
    Unknown suffixes and ordinary granule filenames retain the literal match;
    catalog absence does not establish that they name another product.
    Longer catalog identifiers may use Markdown backslash escapes. Only the
    shorter term's existing literal occurrences are returned. A sweep over
    source positions compares each occurrence with overlapping containers,
    rather than rescanning all occurrences elsewhere in the line.

    Args:
        term: Literal search term for the product being assessed.
        text: Unmodified source text to inspect.
        identity_rules: One product's term-to-identifier mapping returned by
            build_catalog_identity_rules. None keeps broad literal matching.

    Returns:
        Eligible zero-based, end-exclusive character ranges in the original
        text. No source characters are changed or removed.
    """
    occurrences = find_ascii_identifier_occurrences(term, text)
    if not occurrences or not identity_rules:
        return occurrences
    normalized_term = term.translate(ASCII_LOWERCASE)
    longer_identifiers = [identifier for candidate_term, identifiers in identity_rules.items()
                          if candidate_term.translate(ASCII_LOWERCASE) == normalized_term
                          for identifier in identifiers]
    ownership_by_range: dict[tuple[int, int], bool] = {}
    for identifier in longer_identifiers:
        if len(identifier["term"]) <= len(term):
            continue
        for identifier_range in find_authoritative_identifier_occurrences(identifier["term"], text):
            ownership_by_range[identifier_range] = (ownership_by_range.get(identifier_range, False)
                                                   or identifier["same_product"] is True)
    containing_identifiers = [(start, end, same_product)
                              for (start, end), same_product in ownership_by_range.items()]
    # Larger intervals must precede smaller ones with the same start. The
    # subsequent maximum-end pass can then skip every contained interval.
    containing_identifiers.sort(key=lambda container: (container[0], -container[1]))
    active_containers: list[tuple[int, int, bool]] = []
    next_container = 0
    eligible_occurrences = []
    for start, end in occurrences:
        while (next_container < len(containing_identifiers)
               and containing_identifiers[next_container][0] <= start):
            active_containers.append(containing_identifiers[next_container])
            next_container += 1
        active_containers = [container for container in active_containers if container[1] >= end]
        greatest_end = -1
        compatible_owner = False
        for _, container_end, same_product in active_containers:
            if container_end > greatest_end:
                compatible_owner = compatible_owner or same_product
                greatest_end = container_end
        if not active_containers or compatible_owner:
            eligible_occurrences.append((start, end))
    return eligible_occurrences

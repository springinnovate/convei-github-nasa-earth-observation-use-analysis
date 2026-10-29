"""Freeze bounded web queries from catalog identifiers and collection titles."""

import re
from typing import Any

from nasa_eo_search.repository_discovery import validate_search_term


WEB_QUERY_POLICY = "catalog-title-v1"
TITLE_METADATA_WORDS = frozenset({
    "nasa", "level", "global", "daily", "day", "days", "monthly", "month", "year",
    "yearly", "hour", "hours", "hourly", "minute", "minutes", "second", "seconds",
    "arc", "degree", "degrees", "grid", "sin", "km", "m", "meter", "meters", "x",
    "v", "version", "data", "dataset", "product",
})


def select_web_context_words(title: str, short_name: str) -> list[str]:
    """Extract at most six literal title words for one contextual web query.

    Preserve title order and spelling while removing repeated words, numeric
    version/resolution tokens, the exact short name, and a fixed list of generic
    metadata words. Only ASCII alphabetic words enter the query, so punctuation
    in catalog titles cannot introduce search qualifiers. Do not invent subject
    terms, dataset versions, or aliases.

    Args:
        title: Original title selected from saved collection metadata.
        short_name: Exact catalog identifier already present in the query.

    Returns:
        Up to six distinct words, or an empty list for a title with no usable words.
    """
    words = []
    seen_words = set(TITLE_METADATA_WORDS) | {short_name.casefold()}
    for token in re.findall(r"\b[A-Za-z][A-Za-z0-9]*\b", title):
        if not token.isalpha() or token.casefold() in seen_words:
            continue
        words.append(token)
        seen_words.add(token.casefold())
        if len(words) == 6:
            break
    return words


def build_web_discovery_plan(catalog: dict[str, Any], short_names: list[str], model: str) -> dict[str, Any]:
    """Build fixed web-search inputs and limits without searching or reading a key.

    Each selected canonical short name contributes a quoted GitHub-only query
    and, when possible, a second query containing literal catalog title words.
    Sort candidate collections by provider and concept identifier to choose the
    same title regardless of catalog order. Record its original title and source
    metadata so the queries can be inspected before a live run and checked on
    resume. Aliases and known repository names are not added by this rule.

    Args:
        catalog: Saved catalog object with products and an optional summary source
            URL. The caller normally supplies a catalog already validated by the
            catalog reader; this function checks the fields used for web queries.
        short_names: Nonempty list of exact selected catalog names. Duplicates
            collapse while preserving the first occurrence's position.
        model: Explicit Responses API model name, preserved without substitution.

    Returns:
        Frozen model, query policy, ordered query records with title provenance,
        and per-request limits: two tool calls, 2,000 output tokens, and twenty
        distinct source repositories.

    Raises:
        ValueError: The model, selection, catalog structure, or required query
            provenance fields are malformed, or a selected name is absent.
    """
    if (not isinstance(model, str) or not 1 <= len(model) <= 200
            or any(not 33 <= ord(character) <= 126 for character in model)):
        raise ValueError("Web search model must be 1 to 200 printable ASCII characters without whitespace.")
    if not isinstance(short_names, list) or not short_names:
        raise ValueError("Web discovery requires a nonempty list of catalog short names.")
    for short_name in short_names:
        validate_search_term(short_name)
        if short_name != short_name.strip() or any(not character.isprintable() for character in short_name):
            raise ValueError("Web discovery short names cannot contain surrounding whitespace or control characters.")
    if not isinstance(catalog, dict) or not isinstance(catalog.get("products"), list):
        raise ValueError("Web discovery requires a catalog products list.")
    summary = catalog.get("summary", {})
    if not isinstance(summary, dict) or not isinstance(summary.get("source_url", ""), str):
        raise ValueError("Web discovery catalog source URL must be text.")
    selected_names = list(dict.fromkeys(short_names))
    collections_by_name: dict[str, list[dict[str, str]]] = {name: [] for name in selected_names}
    for product in catalog["products"]:
        if not isinstance(product, dict) or not isinstance(product.get("short_name"), str):
            raise ValueError("Web discovery catalog products require short-name text.")
        if product["short_name"] not in collections_by_name:
            continue
        provider = product.get("provider")
        collections = product.get("collections")
        if not isinstance(provider, str) or not provider or not isinstance(collections, list) or not collections:
            raise ValueError("Selected web discovery products require a provider and collection metadata.")
        for collection in collections:
            if (not isinstance(collection, dict) or not isinstance(collection.get("concept_id"), str)
                    or not collection["concept_id"] or not isinstance(collection.get("title", ""), str)):
                raise ValueError("Web discovery collection identifiers and titles must be text.")
            collections_by_name[product["short_name"]].append({
                "provider": provider, "concept_id": collection["concept_id"], "title": collection.get("title", ""),
            })
    queries = []
    for short_name in selected_names:
        collections = collections_by_name[short_name]
        if not collections:
            raise ValueError("Web discovery short names must be present in the saved catalog.")
        selected_collection = min(collections, key=lambda collection: (
            collection["provider"], collection["concept_id"], collection["title"],
        ))
        title = selected_collection["title"]
        quoted_term = short_name.replace("\\", "\\\\")
        canonical_query = f'site:github.com "{quoted_term}"'
        requested_queries = [canonical_query]
        context_words = select_web_context_words(title, short_name)
        if context_words:
            requested_queries.append(canonical_query + " " + " ".join(context_words))
        queries.append({
            "short_name": short_name, "queries": requested_queries, "context_title": title,
            "context_provider": selected_collection["provider"],
            "context_concept_id": selected_collection["concept_id"],
            "context_source_url": summary.get("source_url", ""),
        })
    return {
        "model": model, "query_policy": WEB_QUERY_POLICY, "queries": queries,
        "max_tool_calls": 2, "max_output_tokens": 2000, "max_source_repositories": 20,
    }

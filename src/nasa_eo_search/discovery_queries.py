"""Build reproducible discovery queries from catalog names and cited aliases."""

import csv
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from nasa_eo_search.repository_discovery import validate_search_term


DISCOVERY_ALIAS_COLUMNS = ("short_name", "term", "source_url")


def validate_discovery_alias_record(
    record: dict[str, str], available_short_names: set[str],
) -> dict[str, str]:
    """Validate one dataset alias and its supporting HTTPS source without fetching it.

    Args:
        record: Exactly the short_name, term, and source_url fields for one alias.
        available_short_names: Catalog names this alias is permitted to reference.

    Returns:
        An independent copy preserving the exact identifier and source spelling.

    Raises:
        ValueError: A field is absent, extra, not text, or invalid; the dataset is
            unknown; or the source URL is not absolute HTTPS without user info.
    """
    if (not isinstance(record, dict) or set(record) != set(DISCOVERY_ALIAS_COLUMNS)
            or any(not isinstance(record[column], str) for column in DISCOVERY_ALIAS_COLUMNS)):
        raise ValueError("Discovery aliases require short_name, term, and source_url text fields.")
    if record["short_name"] not in available_short_names:
        raise ValueError("Discovery alias short_name must be in the selected catalog names.")
    validate_search_term(record["term"])
    if any(ord(character) < 32 or ord(character) == 127 for character in record["term"]):
        raise ValueError("Discovery alias terms cannot contain control characters.")
    source_url = record["source_url"]
    try:
        parsed_url = urlsplit(source_url)
        valid_url = (parsed_url.scheme == "https" and bool(parsed_url.hostname)
                     and parsed_url.username is None and parsed_url.password is None
                     and not any(character.isspace() or ord(character) < 32 or ord(character) == 127
                                 for character in source_url))
        # Accessing port also checks malformed or out-of-range port strings.
        parsed_url.port
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("Discovery alias source_url must be absolute HTTPS without user information.")
    return {column: record[column] for column in DISCOVERY_ALIAS_COLUMNS}


def read_discovery_aliases(path: Path, available_short_names: set[str]) -> list[dict[str, str]]:
    """Read and validate a CSV of additional discovery terms and their provenance.

    The UTF-8 file must have exactly the ordered headers short_name, term, and
    source_url. Identical duplicate rows collapse; conflicting source URLs for
    one dataset/term are rejected. This does not fetch URLs or change the catalog.

    Args:
        path: CSV file to read, optionally containing a UTF-8 byte-order mark.
        available_short_names: Exact dataset short names in the saved catalog.

    Returns:
        Alias records sorted by dataset name and term, independent of row order.

    Raises:
        OSError: The input file cannot be opened or read.
        ValueError: The CSV, encoding, headers, aliases, or provenance are invalid.
    """
    aliases_by_identity: dict[tuple[str, str], dict[str, str]] = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as alias_input:
            reader = csv.DictReader(alias_input, strict=True)
            if reader.fieldnames != list(DISCOVERY_ALIAS_COLUMNS):
                raise ValueError("Discovery alias CSV headers must be short_name,term,source_url in that order.")
            for record in reader:
                try:
                    alias = validate_discovery_alias_record(record, available_short_names)
                except ValueError as error:
                    raise ValueError(f"Discovery alias CSV line {reader.line_num}: {error}") from error
                identity = (alias["short_name"], alias["term"])
                if identity in aliases_by_identity and aliases_by_identity[identity] != alias:
                    raise ValueError(f"Discovery alias CSV line {reader.line_num}: conflicting provenance for a dataset/term.")
                aliases_by_identity[identity] = alias
    except (csv.Error, UnicodeError) as error:
        raise ValueError("Discovery alias CSV must contain valid UTF-8 and well-formed CSV rows.") from error
    return [aliases_by_identity[identity] for identity in sorted(aliases_by_identity)]


def build_discovery_queries(short_names: list[str], aliases: list[dict[str, str]]) -> list[dict[str, str]]:
    """Pair selected catalog names with their canonical and additional search terms.

    Each selected product contributes its short name first, then its aliases in
    deterministic term order. Repeated terms for one product collapse; a shared
    alias retains one association for each product. Canonical terms use an empty
    source_url because their catalog provenance is already stored in the plan.

    Args:
        short_names: Selected catalog names in desired discovery order.
        aliases: Valid alias records referring only to those selected names.

    Returns:
        Ordered short_name/term/source_url records ready to freeze in a plan.

    Raises:
        ValueError: Inputs are malformed, a term is invalid, an alias refers to an
            unselected product, or duplicate alias records conflict.
    """
    if not isinstance(short_names, list) or not isinstance(aliases, list):
        raise ValueError("Discovery names and aliases must be lists.")
    for short_name in short_names:
        validate_search_term(short_name)
    selected_names = list(dict.fromkeys(short_names))
    available_names = set(selected_names)
    aliases_by_identity: dict[tuple[str, str], dict[str, str]] = {}
    for record in aliases:
        alias = validate_discovery_alias_record(record, available_names)
        identity = (alias["short_name"], alias["term"])
        if identity in aliases_by_identity and aliases_by_identity[identity] != alias:
            raise ValueError("Discovery aliases have conflicting provenance for a dataset/term.")
        aliases_by_identity[identity] = alias
    queries = []
    for short_name in selected_names:
        queries.append({"short_name": short_name, "term": short_name, "source_url": ""})
        queries.extend(aliases_by_identity[identity] for identity in sorted(aliases_by_identity)
                       if identity[0] == short_name and identity[1] != short_name)
    return queries


def discovery_query_terms(plan: dict[str, Any]) -> list[str]:
    """List distinct request terms from a new or legacy discovery plan.

    New plans search every selected catalog name before additional aliases.
    Remaining aliases take turns by dataset so one product's longer alias list
    cannot consume the request budget before other products receive a query.
    Frozen query records retain their original dataset grouping and provenance.

    Args:
        plan: Saved settings with discovery_queries or legacy discovery_terms.

    Returns:
        Canonical terms in selected-dataset order, then interleaved aliases.
        Shared terms are requested once. Legacy plans retain their saved order.

    Raises:
        ValueError: The saved query list, a record, or a term is malformed, or a
            new plan's queries do not match its selected dataset names.
    """
    if "discovery_queries" not in plan:
        terms = plan.get("discovery_terms")
        if not isinstance(terms, list):
            raise ValueError("Saved discovery_terms must be a list.")
        for term in terms:
            validate_search_term(term)
        return list(dict.fromkeys(terms))

    queries = plan["discovery_queries"]
    if not isinstance(queries, list) or any(not isinstance(query, dict)
                                          or "short_name" not in query or "term" not in query
                                          for query in queries):
        raise ValueError("Saved discovery_queries must be a list of dataset/term records.")
    for query in queries:
        validate_search_term(query["short_name"])
        validate_search_term(query["term"])
    selected_names = plan.get("discovery_terms", [query["short_name"] for query in queries])
    if not isinstance(selected_names, list):
        raise ValueError("Saved discovery_terms must be a list.")
    for short_name in selected_names:
        validate_search_term(short_name)
    ordered_names = list(dict.fromkeys(selected_names))
    terms_by_dataset: dict[str, list[str]] = {short_name: [] for short_name in ordered_names}
    for query in queries:
        if query["short_name"] not in terms_by_dataset:
            raise ValueError("Saved discovery query refers to an unselected dataset.")
        dataset_terms = terms_by_dataset[query["short_name"]]
        if query["term"] not in dataset_terms:
            dataset_terms.append(query["term"])
    if any(short_name not in terms_by_dataset[short_name] for short_name in ordered_names):
        raise ValueError("Saved discovery queries are missing a canonical dataset term.")

    ordered_terms = list(ordered_names)
    scheduled_terms = set(ordered_terms)
    next_positions = {short_name: 0 for short_name in ordered_names}
    while True:
        added_term = False
        for short_name in ordered_names:
            dataset_terms = terms_by_dataset[short_name]
            while next_positions[short_name] < len(dataset_terms):
                term = dataset_terms[next_positions[short_name]]
                next_positions[short_name] += 1
                if term in scheduled_terms:
                    continue
                ordered_terms.append(term)
                scheduled_terms.add(term)
                added_term = True
                break
        if not added_term:
            return ordered_terms


def discovery_providers(plan: dict[str, Any]) -> tuple[str, ...]:
    """Choose discovery services while preserving legacy code-search behavior.

    Args:
        plan: Saved settings with an optional github_discovery mode and grep.app
            inclusion flag. Omitted mode uses legacy GitHub code search.

    Returns:
        Repository search before code search in code-and-readme mode, followed
        by grep.app when enabled. Legacy plans return code search first.

    Raises:
        ValueError: A saved mode or grep.app inclusion flag is unsupported.
    """
    mode = plan.get("github_discovery", "code")
    if not isinstance(mode, str) or mode not in {"code", "code-and-readme"}:
        raise ValueError("Unsupported GitHub discovery mode.")
    include_grep_app = plan.get("include_grep_app", False)
    if type(include_grep_app) is not bool:
        raise ValueError("include_grep_app must be boolean.")
    providers = ("github-repositories", "github") if mode == "code-and-readme" else ("github",)
    return (*providers, "grep.app") if include_grep_app else providers

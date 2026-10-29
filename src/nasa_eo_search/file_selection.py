"""Choose saved dataset/file matches before preparing classification context."""

import csv
from pathlib import Path
from typing import Any

from nasa_eo_search.evidence_inputs import identify_evidence


def identify_saved_file_match(repository_group: dict[str, Any], file_record: dict[str, Any]) -> str:
    """Return the stable match-table ID without copying or formatting source text.

    Args:
        repository_group: Saved repository/product evidence with catalog metadata.
        file_record: One matching file and its recorded commit.

    Returns:
        The dataset/repository/file/revision identifier used by ``matches.csv``.
    """
    product = repository_group["product"]
    return identify_evidence([
        repository_group["repository"], product["provider"], product["short_name"],
        file_record["file"], file_record.get("commit"),
    ])


def read_selected_match_ids(selection_csv: Path) -> list[str]:
    """Read selected match IDs from a copied or reduced match-table CSV.

    Other columns are ignored: evidence always comes from the saved search.
    Large copied snippets are accepted without loading the entire CSV into
    memory. Duplicate IDs select a match once. UTF-8 files with a byte-order
    mark, as commonly exported by spreadsheet software, are accepted.

    Args:
        selection_csv: CSV with a ``match_id`` column and at least one data row.

    Returns:
        Sorted, distinct, nonempty match identifiers.

    Raises:
        OSError: If the CSV cannot be read.
        ValueError: If the required column or any match ID is missing.
        csv.Error: If the CSV cannot be parsed.
    """
    match_ids = set()
    original_field_limit = csv.field_size_limit()
    # Sourcegraph excerpts may exceed the default limit even though only IDs
    # are read here. This limit fits a Windows C long as well as wider platforms.
    csv.field_size_limit(max(original_field_limit, 2**31 - 1))
    try:
        with selection_csv.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source, strict=True)
            if reader.fieldnames is None or reader.fieldnames.count("match_id") != 1:
                raise ValueError("--selection requires a CSV with exactly one match_id column.")
            for row in reader:
                match_id = row.get("match_id")
                if not isinstance(match_id, str) or not match_id.strip():
                    raise ValueError(f"--selection has a missing match_id at CSV line {reader.line_num}.")
                match_ids.add(match_id.strip())
    finally:
        csv.field_size_limit(original_field_limit)
    if not match_ids:
        raise ValueError("--selection contains no match IDs; select at least one saved match.")
    return sorted(match_ids)


def build_file_selection(
    available_matches: dict[str, str], short_names: list[str] | None,
    match_ids: list[str] | None,
) -> dict[str, Any]:
    """Validate filters and intersect their matches in a repeatable selection.

    Args:
        available_matches: Saved match IDs mapped to exact dataset short names.
        short_names: Exact dataset filters, or None to allow every dataset.
        match_ids: IDs supplied by a selection CSV, or None to allow every ID.

    Returns:
        Canonical selectors, selected match IDs, and available/selected counts.

    Raises:
        ValueError: If a filter is empty, malformed, unknown, or selects no matches.
    """
    for label, values in (("short names", short_names), ("match IDs", match_ids)):
        if values is not None and (
            not isinstance(values, list) or not values
            or any(not isinstance(value, str) or not value.strip() for value in values)
        ):
            raise ValueError(f"Classification selection {label} must be a nonempty list of strings.")
    canonical_names = sorted(set(short_names)) if short_names is not None else None
    canonical_ids = sorted(set(match_ids)) if match_ids is not None else None
    unknown_names = set(canonical_names or []) - set(available_matches.values())
    unknown_ids = set(canonical_ids or []) - available_matches.keys()
    if unknown_names:
        raise ValueError("Unknown --short-name in saved matches: " + ", ".join(sorted(unknown_names)[:5]) + ".")
    if unknown_ids:
        raise ValueError("Unknown match_id in --selection: " + ", ".join(sorted(unknown_ids)[:5]) + ".")
    requested_names = set(canonical_names) if canonical_names is not None else None
    requested_ids = set(canonical_ids) if canonical_ids is not None else None
    selected_ids = sorted(
        match_id for match_id, short_name in available_matches.items()
        if (requested_names is None or short_name in requested_names)
        and (requested_ids is None or match_id in requested_ids)
    )
    if not selected_ids and (canonical_names is not None or canonical_ids is not None):
        raise ValueError("The classification selection contains no matching saved files; check both filters.")
    return {
        "schema_version": 1, "short_names": canonical_names, "match_ids": canonical_ids,
        "selected_match_ids": selected_ids, "available_matches": len(available_matches),
        "selected_matches": len(selected_ids),
    }


def select_file_classification_matches(
    source_plan: dict[str, Any], short_names: list[str] | None = None,
    selection_csv: Path | None = None, saved_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select saved matches, or verify and reuse a frozen classification selection.

    A resume with neither selector uses the saved selection without reopening its
    CSV. Supplying either selector requires the same canonical filters used when
    creating the output. Earlier plans without selection metadata remain full
    search assessments. Full source validation is performed separately by the
    caller, before any context preparation or paid requests.

    Args:
        source_plan: Complete saved search evidence, before per-file preparation.
        short_names: Repeatable exact dataset filters from the command line.
        selection_csv: Optional CSV of stable match IDs from the saved table.
        saved_plan: Existing validated file-classification plan, or None for new work.

    Returns:
        Canonical selection metadata ready to store in ``file_selection``.

    Raises:
        OSError: If a supplied selection CSV cannot be read.
        ValueError: If selectors or frozen metadata are invalid or incompatible.
        csv.Error: If the supplied CSV cannot be parsed.
    """
    available_matches = {
        identify_saved_file_match(group, file_record): group["product"]["short_name"]
        for group in source_plan["groups"] for file_record in group["files"]
    }
    saved_selection = None
    if saved_plan is not None:
        if "file_selection" in saved_plan:
            saved_selection = saved_plan["file_selection"]
            if not isinstance(saved_selection, dict) or type(saved_selection.get("schema_version")) is not int or saved_selection["schema_version"] != 1:
                raise ValueError("Invalid saved classification selection; choose a new output directory.")
            expected_selection = build_file_selection(
                available_matches, saved_selection.get("short_names"), saved_selection.get("match_ids"),
            )
            if (saved_selection != expected_selection
                    or type(saved_selection.get("available_matches")) is not int
                    or type(saved_selection.get("selected_matches")) is not int):
                raise ValueError("Saved classification selection differs from the source matches; choose a new output directory.")
        else:
            saved_selection = build_file_selection(available_matches, None, None)
        if sorted(group["group_id"] for group in saved_plan["groups"]) != saved_selection["selected_match_ids"]:
            raise ValueError("Saved classification groups differ from the frozen selection; choose a new output directory.")
        if short_names is None and selection_csv is None:
            return saved_selection
    requested_selection = build_file_selection(
        available_matches, short_names,
        read_selected_match_ids(selection_csv) if selection_csv is not None else None,
    )
    if saved_selection is not None and requested_selection != saved_selection:
        raise ValueError("Classification selection differs from this run. Omit both selectors to resume, or choose a new output directory.")
    return requested_selection

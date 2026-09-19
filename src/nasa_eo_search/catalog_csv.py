"""Export saved NASA product groups as a comma-separated values (CSV) table."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys
import unicodedata
from typing import Any


CSV_COLUMNS = (
    "provider", "short_name", "collection_count", "collection_titles",
    "collection_descriptions", "collection_versions", "collection_concept_ids",
    "collection_entry_ids", "candidate_search_terms", "candidate_review_statuses",
    "candidate_sources", "source_catalog_directory", "catalog_status",
    "catalog_scope", "catalog_source_url", "catalog_started_at",
    "catalog_finished_at", "catalog_warnings", "catalog_error",
)
COLLECTION_COLUMNS = {
    "collection_titles": "title", "collection_descriptions": "summary",
    "collection_versions": "version", "collection_concept_ids": "concept_id",
    "collection_entry_ids": "entry_id",
}


def require_catalog_text(value: Any, field_name: str, optional: bool = False) -> str:
    """Validate a catalog text field and render optional missing values as blanks.

    Args:
        value: Value read from the catalog's JSON files.
        field_name: Field description to include in validation errors.
        optional: Whether None and empty strings are accepted.

    Returns:
        Original text, or an empty string for an optional None value.

    Raises:
        ValueError: If the field has the wrong type or required text is blank.
    """

    if value is None and optional:
        return ""
    if not isinstance(value, str) or (not optional and not value.strip()):
        raise ValueError(f"{field_name} must be {'text' if optional else 'nonempty text'}.")
    return value


def number_catalog_values(values: list[str]) -> str:
    """Join related values with numbered labels for comparison across CSV columns.

    Args:
        values: Text in source order, including blanks for missing optional fields.

    Returns:
        Newline-separated entries such as '[1] 006' and '[2] 007'.
    """

    return "\n".join(f"[{index}] {value}" for index, value in enumerate(values, start=1))


def protect_spreadsheet_cell(cell_text: str) -> str:
    """Label formula-like text so spreadsheets display it as catalog content.

    Check formula prefixes after leading whitespace, control, and format characters,
    including full-width variants. A visible 'Text: ' prefix stays ordinary text
    when the exported CSV is saved and reopened. The source JSON is unchanged.

    Args:
        cell_text: Formatted field to write into one quoted CSV cell.

    Returns:
        Original text, or text prefixed with 'Text: ' when its start is unsafe.
    """

    normalized_text = unicodedata.normalize("NFKC", cell_text)
    significant_text = normalized_text.lstrip()
    while significant_text and unicodedata.category(significant_text[0]).startswith("C"):
        significant_text = significant_text[1:].lstrip()
    if (significant_text.startswith(("=", "+", "-", "@"))
            or cell_text.startswith(("\t", "\r", "\n"))):
        return "Text: " + cell_text
    return cell_text


def build_product_csv_row(product: Any) -> dict[str, str]:
    """Flatten a product group while keeping version and search-term associations.

    Collection columns share collection numbers; candidate columns share a separate
    numbering sequence. Every candidate source includes its concept ID and field.

    Args:
        product: Product object from one line of products.jsonl.

    Returns:
        Product columns ready to combine with run provenance and escape for CSV.

    Raises:
        ValueError: If the product or its nested records lack valid metadata.
    """

    if not isinstance(product, dict):
        raise ValueError("Product must be a JSON object.")
    row = {field: require_catalog_text(product.get(field), field)
           for field in ("provider", "short_name")}
    collections = product.get("collections")
    candidates = product.get("candidate_signatures")
    if not isinstance(collections, list) or not collections:
        raise ValueError("collections must be a nonempty list.")
    if not isinstance(candidates, list):
        raise ValueError("candidate_signatures must be a list.")
    row["collection_count"] = str(len(collections))
    for column, field in COLLECTION_COLUMNS.items():
        values = []
        for collection in collections:
            if not isinstance(collection, dict):
                raise ValueError("Each collection must be an object.")
            values.append(require_catalog_text(
                collection.get(field), f"collection.{field}",
                optional=field in ("summary", "version", "entry_id"),
            ))
        row[column] = number_catalog_values(values)
    concept_ids = {collection["concept_id"] for collection in collections}
    candidate_terms, review_statuses, candidate_sources = [], [], []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("Each candidate must be an object.")
        candidate_terms.append(require_catalog_text(candidate.get("term"), "candidate.term"))
        review_statuses.append(require_catalog_text(candidate.get("review_status"), "candidate.review_status"))
        sources = candidate.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError("candidate.sources must be a nonempty list.")
        source_labels = []
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("Each candidate source must be an object.")
            concept_id = require_catalog_text(source.get("concept_id"), "source.concept_id")
            source_field = require_catalog_text(source.get("source_field"), "source.source_field")
            if concept_id not in concept_ids:
                raise ValueError(f"Candidate source {concept_id} is absent from the product collections.")
            source_labels.append(f"{concept_id} ({source_field})")
        candidate_sources.append("; ".join(source_labels))
    row["candidate_search_terms"] = number_catalog_values(candidate_terms)
    row["candidate_review_statuses"] = number_catalog_values(review_statuses)
    row["candidate_sources"] = number_catalog_values(candidate_sources)
    return row


def read_catalog_csv_rows(catalog_directory: Path) -> tuple[str, list[dict[str, str]]]:
    """Read and validate a saved catalog before creating its CSV export.

    Attach run provenance to each product row, reject duplicate product groups,
    and compare the parsed product count with summary.json. An active run is
    rejected because its product file may still be changing.

    Args:
        catalog_directory: Directory containing summary.json and products.jsonl.

    Returns:
        Source catalog status and validated, spreadsheet-protected rows.

    Raises:
        ValueError: For malformed metadata, active runs, duplicates, or count mismatch.
        OSError: If an input file cannot be read.
    """

    catalog_directory = catalog_directory.resolve()
    with (catalog_directory / "summary.json").open(encoding="utf-8-sig") as summary_input:
        summary = json.load(summary_input)
    if not isinstance(summary, dict) or type(summary.get("schema_version")) is not int or summary["schema_version"] != 1:
        raise ValueError("summary.json must describe catalog schema_version 1.")
    catalog_status = summary.get("status")
    if catalog_status not in ("complete", "incomplete", "failed", "interrupted"):
        raise ValueError("Catalog status must be complete, incomplete, failed, or interrupted; wait for active runs to finish.")
    expected_count = summary.get("products_saved")
    if type(expected_count) is not int or expected_count < 0:
        raise ValueError("summary.products_saved must be a nonnegative integer.")
    warnings = summary.get("warnings", [])
    if not isinstance(warnings, list) or any(not isinstance(warning, str) for warning in warnings):
        raise ValueError("summary.warnings must be a list of text messages.")
    provenance = {
        "source_catalog_directory": str(catalog_directory), "catalog_status": catalog_status,
        "catalog_warnings": "\n".join(warnings),
    }
    for field in ("scope", "source_url", "started_at", "finished_at", "error"):
        provenance[f"catalog_{field}"] = require_catalog_text(
            summary.get(field), f"summary.{field}", optional=field in ("finished_at", "error"),
        )
    rows = []
    seen_product_groups: set[tuple[str, str]] = set()
    with (catalog_directory / "products.jsonl").open(encoding="utf-8-sig") as products_input:
        for line_number, product_line in enumerate(products_input, start=1):
            if not product_line.strip():
                continue
            try:
                row = build_product_csv_row(json.loads(product_line))
                product_key = (row["provider"], row["short_name"])
                if product_key in seen_product_groups:
                    raise ValueError(f"Duplicate provider/short-name group: {product_key!r}.")
                seen_product_groups.add(product_key)
            except ValueError as validation_error:
                raise ValueError(f"products.jsonl line {line_number}: {validation_error}") from validation_error
            row.update(provenance)
            rows.append({column: protect_spreadsheet_cell(row[column]) for column in CSV_COLUMNS})
    if len(rows) != expected_count:
        raise ValueError(f"Read {len(rows)} product rows, but summary.products_saved is {expected_count}.")
    return catalog_status, rows


def write_catalog_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    """Write validated product rows into a new UTF-8 CSV with a header row.

    Quote every cell, preserve Unicode and embedded newlines, and include a UTF-8
    byte-order mark for spreadsheet encoding detection. Remove a newly created
    partial export on write failure or interruption; existing files are preserved.

    Args:
        output_path: New CSV file in an existing parent directory.
        rows: Validated rows returned by read_catalog_csv_rows.

    Raises:
        OSError: If the output exists or writing/cleanup fails.
        csv.Error: If a CSV row cannot be serialized.
        KeyboardInterrupt: If export is interrupted.
    """

    csv_output = output_path.open("x", encoding="utf-8-sig", newline="")
    try:
        with csv_output:
            writer = csv.DictWriter(csv_output, fieldnames=CSV_COLUMNS, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)
    except (OSError, csv.Error, KeyboardInterrupt):
        output_path.unlink()
        raise


def main(argument_values: list[str] | None = None) -> int:
    """Run nasa-catalog-export to turn a saved product catalog into a CSV.

    Validate the catalog, write one row per product, and report the exported count
    and source status to standard error. Each CSV row includes source provenance.

    Args:
        argument_values: Arguments without the executable name; defaults to sys.argv.

    Returns:
        Zero for a successful export, one for invalid input/files or write failure,
        or 130 for interruption. Argparse exits with two for invalid CLI syntax.
    """

    argument_parser = argparse.ArgumentParser(
        prog="nasa-catalog-export", description="Export a saved NASA product catalog to CSV.",
    )
    argument_parser.add_argument("catalog_directory", type=Path, help="saved catalog directory")
    argument_parser.add_argument("--output", required=True, type=Path, help="new CSV file")
    arguments = argument_parser.parse_args(argument_values)
    try:
        catalog_status, rows = read_catalog_csv_rows(arguments.catalog_directory)
        oversized_cells = sum(len(value) > 32767 for row in rows for value in row.values())
        if oversized_cells:
            print(f"Warning: {oversized_cells} cells exceed Excel's 32,767-character limit; "
                  "the CSV preserves their full text.", file=sys.stderr)
        write_catalog_csv(arguments.output, rows)
    except (OSError, ValueError, csv.Error) as export_error:
        print(f"nasa-catalog-export: {export_error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Catalog export interrupted.", file=sys.stderr)
        return 130
    if catalog_status != "complete":
        print(f"Warning: source catalog status is {catalog_status}; review catalog_warnings "
              "and catalog_error in the CSV.", file=sys.stderr)
    print(f"Exported {len(rows)} products to {arguments.output.resolve()} "
          f"(source catalog status: {catalog_status}).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

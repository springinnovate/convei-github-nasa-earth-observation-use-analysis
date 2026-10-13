"""Build product-linked status and match reports from saved batch attempts."""

import csv
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from nasa_eo_search.batch_files import (
    COMPLETED_SEARCH, atomic_batch_output, inspect_search_attempt, list_search_attempts,
)
from nasa_eo_search.catalog_csv import protect_spreadsheet_cell
from nasa_eo_search.repo_preview import format_repository_match


ASSOCIATION_COLUMNS = ("provider", "short_name", "search_term", "search_id", "query")
STATUS_COLUMNS = (*ASSOCIATION_COLUMNS, "status", "attempt_count", "latest_attempt",
                  "match_records", "warnings", "error")
MATCH_COLUMNS = (*ASSOCIATION_COLUMNS, "attempt", "attempt_status", "latest_attempt",
                 "repository", "repository_url", "owner", "stars", "file", "file_type",
                 "language", "commit", "file_url", "matching_text", "interpretation")


def write_protected_csv_rows(output_path: Path, columns: tuple[str, ...], rows: Iterable[dict[str, Any]]) -> None:
    """Atomically publish a spreadsheet report with quoted, protected text cells.

    Args:
        output_path: Batch-owned report file to replace.
        columns: Ordered CSV column names.
        rows: Row dictionaries; None becomes a blank cell.

    Raises:
        OSError: If writing or publishing the report fails.
        csv.Error: If a row cannot be serialized.
    """

    with atomic_batch_output(output_path, "utf-8-sig") as csv_output:
        writer = csv.DictWriter(csv_output, fieldnames=columns, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: protect_spreadsheet_cell(
                "" if row.get(column) is None else str(row[column])
            ) for column in columns})


def write_batch_reports(
    output_directory: Path, plan: dict[str, Any], status_override: str | None = None,
) -> dict[str, Any]:
    """Refresh batch status and combined CSVs from all preserved attempts.

    searches.csv has one row per product/query using the latest attempt's status.
    matches.csv includes every readable match from every attempt, associated with
    each selected product. Historical attempts are labeled for filtering. Publish
    summary.json last, after both CSVs are successfully written.

    Args:
        output_directory: Existing batch root.
        plan: Validated saved plan containing jobs and product associations.
        status_override: Use running or interrupted while managing execution.

    Returns:
        Batch status, distinct-query status counts, and report row counts.

    Raises:
        OSError: If reports cannot be saved.
        ValueError: If attempt directory paths are malformed.
    """

    status_rows = []
    match_rows = []
    status_counts = dict.fromkeys((COMPLETED_SEARCH, "incomplete", "failed", "interrupted", "pending"), 0)
    for job in plan["jobs"]:
        attempts = list_search_attempts(output_directory, job["search_id"])
        inspected_attempts = [(attempt, *inspect_search_attempt(attempt, job["query"])) for attempt in attempts]
        latest_summary = inspected_attempts[-1][1] if attempts else {"status": "pending"}
        latest_matches = inspected_attempts[-1][2] if attempts else []
        status_counts[latest_summary["status"]] += 1
        for product in job["products"]:
            association = {
                "provider": product["provider"], "short_name": product["short_name"],
                "search_term": job["term"], "search_id": job["search_id"], "query": job["query"],
            }
            status_rows.append({
                **association, "status": latest_summary["status"], "attempt_count": len(attempts),
                "latest_attempt": attempts[-1].name if attempts else "", "match_records": len(latest_matches),
                "warnings": json.dumps(latest_summary.get("warnings", []), ensure_ascii=False),
                "error": latest_summary.get("error", ""),
            })
            for attempt, attempt_summary, matches in inspected_attempts:
                for content_match in matches:
                    match = format_repository_match(content_match)
                    repository_parts = match["repository"].split("/")
                    match_rows.append({
                        **association, "attempt": attempt.name, "attempt_status": attempt_summary["status"],
                        "latest_attempt": str(attempt == attempts[-1]).lower(),
                        "repository": match["repository"], "repository_url": match["repository_url"],
                        "owner": repository_parts[1] if len(repository_parts) > 2 else "",
                        "stars": match["stars"], "file": match["file"],
                        "file_type": PurePosixPath(match["file"]).suffix,
                        "language": match["language"], "commit": match["commit"], "file_url": match["file_url"],
                        "matching_text": "\n".join(f"{line['line_number']}: {line['text']}" for line in match["matching_lines"]),
                        "interpretation": match["interpretation"],
                    })
    status = "complete"
    if status_counts["failed"]:
        status = "failed"
    elif status_counts["interrupted"]:
        status = "interrupted"
    elif status_counts["incomplete"] or status_counts["pending"]:
        status = "incomplete"
    summary = {
        "schema_version": 1, "status": status_override or status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": plan["scope"], "catalog_status": plan["catalog_summary"]["status"],
        "distinct_queries": len(plan["jobs"]), "searches_by_status": status_counts,
        "product_query_rows": len(status_rows), "match_rows_all_attempts": len(match_rows),
    }
    write_protected_csv_rows(output_directory / "searches.csv", STATUS_COLUMNS, status_rows)
    write_protected_csv_rows(output_directory / "matches.csv", MATCH_COLUMNS, match_rows)
    with atomic_batch_output(output_directory / "summary.json") as summary_output:
        json.dump(summary, summary_output, indent=2)
    return summary

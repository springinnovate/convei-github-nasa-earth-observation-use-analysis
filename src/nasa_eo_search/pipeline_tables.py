"""Produce stable file-level match rows shared by search and classification."""

from typing import Any

from nasa_eo_search.evidence_inputs import identify_evidence


FILE_MATCH_COLUMNS = (
    "match_id", "provider", "short_name", "dataset_name", "repository", "file",
    "commit", "line_numbers", "snippet", "source_url", "search_terms",
    "search_complete", "warnings",
)


def build_file_match_row(group: dict[str, Any], file_record: dict[str, Any]) -> dict[str, Any]:
    """Describe one dataset/file/revision without collapsing other repository files.

    Args:
        group: Product/repository group with catalog titles and search warnings.
        file_record: One deduplicated file revision with matching lines and sources.

    Returns:
        Raw CSV fields shared unchanged between matches and classified matches.
        Multiple matching lines and query terms stay together in this file row.
    """
    product = group["product"]
    match_id = identify_evidence([
        group["repository"], product["provider"], product["short_name"],
        file_record["file"], file_record.get("commit"),
    ])
    matching_lines = sorted({(line["line_number"], line["text"])
                             for line in file_record["matching_lines"]})
    source_url = file_record.get("file_url") or ""
    if source_url.startswith("https://github.com/") and matching_lines:
        source_url = source_url.split("#", 1)[0] + f"#L{matching_lines[0][0]}"
    titles = list(dict.fromkeys(collection.get("title", "") for collection in product["collections"]
                               if collection.get("title")))
    warnings = list(group.get("warnings", []))
    if len({number for number, _ in matching_lines}) != len(matching_lines):
        warnings.append("Saved attempts disagree on text at the same file revision and line; both snippets are retained.")
    return {
        "match_id": match_id, "provider": product["provider"], "short_name": product["short_name"],
        "dataset_name": "; ".join(titles), "repository": group["repository"], "file": file_record["file"],
        "commit": file_record.get("commit") or "",
        "line_numbers": "; ".join(str(number) for number in sorted({number for number, _ in matching_lines})),
        "snippet": "\n".join(f"{number}: {text}" for number, text in matching_lines),
        "source_url": source_url,
        "search_terms": "; ".join(sorted({source["term"] for source in file_record.get("sources", [])})),
        "search_complete": str(group["search_complete"]).lower(), "warnings": "\n".join(warnings),
    }


def collapse_batch_file_rows(
    match_rows: list[dict[str, Any]], status_rows: list[dict[str, Any]], catalog_complete: bool,
) -> list[dict[str, Any]]:
    """Union matching lines from all saved attempts while preserving product associations.

    Args:
        match_rows: Raw batch rows including original matching_lines and dataset_name.
        status_rows: Latest per-product query statuses, including zero-hit queries.
        catalog_complete: Whether the saved source catalog completed successfully.

    Returns:
        One stable row per product/repository/file/revision. Repeated query and
        retry hits are merged, while distinct files and revisions remain separate.
    """
    completed_status = "finished_no_reported_limits"
    product_statuses: dict[tuple[str, str], list[str]] = {}
    for status in status_rows:
        product_statuses.setdefault((status["provider"], status["short_name"]), []).append(status["status"])
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in match_rows:
        group_key = (row["repository"], row["provider"], row["short_name"])
        group = groups.setdefault(group_key, {
            "repository": row["repository"],
            "product": {"provider": row["provider"], "short_name": row["short_name"],
                        "collections": [{"title": row.get("dataset_name", "")}]},
            "files": {},
        })
        file_key = (row["file"], row["commit"])
        record = group["files"].setdefault(file_key, {
            "file": row["file"], "commit": row["commit"], "file_url": row["file_url"],
            "matching_lines": [], "sources": [],
        })
        for matching_line in row["matching_lines"]:
            if matching_line not in record["matching_lines"]:
                record["matching_lines"].append(matching_line)
        provenance = {"term": row["search_term"], "status": row["attempt_status"],
                      "latest_attempt": row["latest_attempt"] == "true"}
        if provenance not in record["sources"]:
            record["sources"].append(provenance)
    result = []
    for group_key, group in sorted(groups.items()):
        files = list(group["files"].values())
        statuses = product_statuses.get(group_key[1:], [])
        group["search_complete"] = bool(statuses) and all(status == completed_status for status in statuses)
        warnings = []
        if not group["search_complete"]:
            warnings.append("One or more selected product searches are incomplete.")
        if not catalog_complete:
            warnings.append("The source catalog is partial.")
        if any(not any(source["latest_attempt"] for source in record["sources"]) for record in files):
            warnings.append("Some evidence appears only in historical search attempts.")
        if any(source["status"] != completed_status for record in files for source in record["sources"]):
            warnings.append("Some evidence came from partial or damaged search attempts; inspect provenance.")
        if len({record["commit"] for record in files}) > 1:
            warnings.append("Evidence spans multiple commits; it is not a single repository snapshot.")
        group["warnings"] = warnings
        for record in sorted(files, key=lambda item: (item["file"], item["commit"] or "")):
            result.append(build_file_match_row(group, record))
    return result

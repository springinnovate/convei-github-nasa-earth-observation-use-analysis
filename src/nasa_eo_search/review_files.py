"""Freeze evidence for independent human review without network access."""

import csv
from html import escape
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from nasa_eo_search.batch_files import atomic_batch_output
from nasa_eo_search.evidence_inputs import identify_evidence, read_evidence_json
from nasa_eo_search.evidence_reports import (
    CATEGORY_PRIORITY, build_evidence_reports, format_evidence_location, load_evidence_plan,
    save_evidence_json,
)
from nasa_eo_search.review_selection import KNOWN_EXAMPLE_COLUMNS, SELECTION_COLUMNS


REVIEW_RUBRIC_VERSION = "human-evidence-v1"
LABEL_COLUMNS = (
    "repository", "provider", "short_name", "group_id", "evidence_revision",
    "human_category", "supporting_citation", "evidence_sufficiency",
    "missing_context_reason", "notes", "review_minutes",
)


def save_review_snapshot(path: Path, data: dict[str, Any]) -> str:
    """Save a content-addressed review artifact in a new command-owned directory.

    Args:
        path: Output JSON path.
        data: Frozen evidence, selection, or machine-response data.

    Returns:
        Content hash used to connect the frozen artifacts.

    Raises:
        OSError: If saving fails.
    """
    snapshot_id = identify_evidence(data)
    save_evidence_json(path, {"schema_version": 1, "snapshot_id": snapshot_id, "data": data})
    return snapshot_id


def read_review_snapshot(path: Path) -> dict[str, Any]:
    """Read a frozen artifact, rejecting accidental edits or unsupported formats.

    Args:
        path: JSON artifact written by this review workflow.

    Returns:
        Verified artifact data. Hashes detect changes, not malicious forgery.

    Raises:
        OSError: If the artifact is unavailable.
        ValueError: If its version, data, or content hash is invalid.
    """
    envelope = read_evidence_json(path)
    data = envelope.get("data")
    if (envelope.get("schema_version") != 1 or not isinstance(data, dict)
            or envelope.get("snapshot_id") != identify_evidence(data)):
        raise ValueError(f"Changed or invalid review snapshot: {path}. Create a new review directory.")
    return data


def identify_review_evidence(group: dict[str, Any], plan: dict[str, Any]) -> str:
    """Fingerprint the supplied evidence and rubric, independently of model runs.

    Args:
        group: Prepared repository/product evidence and model-visible packets.
        plan: Preparation plan containing catalog and search scope.

    Returns:
        Hash that changes with excerpts, context, packaging, or limitations, but
        not timestamps, source directory paths, model choice, or model responses.
    """
    return identify_evidence({
        "rubric_version": REVIEW_RUBRIC_VERSION,
        "group_id": group["group_id"], "packets": group["packets"],
        "evidence_blocks": group["evidence_blocks"], "raw_evidence": group["evidence"],
        "search_complete": group["search_complete"], "warnings": group["warnings"],
        "evidence_gaps": group.get("evidence_gaps", []),
        "preparation_error": group.get("preparation_error"),
        "catalog_status": plan["catalog_status"], "search_scope": plan["search_scope"],
    })


def inspect_source_file_versions(source: Path) -> list[tuple[str, int, int]]:
    """Record source file versions to detect a classifier running during review.

    Args:
        source: Prepared classification directory.

    Returns:
        Relative filenames, byte sizes, and modification times for inputs only.

    Raises:
        OSError: If files disappear or cannot be inspected.
    """
    paths = [source / "plan.json", source / "settings.json"]
    paths.extend((source / "requests").glob("*/attempt-*.json"))
    versions = []
    for path in sorted(paths):
        if path.exists():
            details = path.stat()
            versions.append((str(path.relative_to(source)), details.st_size, details.st_mtime_ns))
    return versions


def read_classification_snapshot(source: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate saved evidence and responses without keys, API calls, or source writes.

    Args:
        source: Existing format-2 classification directory, finished or stopped.

    Returns:
        Frozen preparation plan and machine snapshot including original receipts.
        Machine reports are reconstructed from responses rather than trusting CSVs.

    Raises:
        OSError: If inputs are unavailable.
        ValueError: If inputs are invalid or change while being inspected.
    """
    before = inspect_source_file_versions(source)
    plan = load_evidence_plan(source)
    settings_path = source / "settings.json"
    settings = read_evidence_json(settings_path) if settings_path.exists() else None
    if settings is not None and (
        not isinstance(settings.get("model"), str) or not settings["model"].strip()
        or type(settings.get("max_output_tokens")) is not int or settings["max_output_tokens"] < 1
    ):
        raise ValueError("Invalid saved classification settings.")
    reports = build_evidence_reports(source, plan, settings)
    if inspect_source_file_versions(source) != before:
        raise ValueError("Classification inputs changed during inspection; stop the runner and try again.")
    revisions = {group["group_id"]: identify_review_evidence(group, plan) for group in plan["groups"]}
    groups = [{**group, "evidence_revision": revisions[group["group_id"]]}
              for group in reports["audit"]["groups"]]
    summary = {key: value for key, value in reports["summary"].items() if key != "updated_at"}
    return plan, {
        "source_plan_id": identify_evidence(plan), "rubric_version": REVIEW_RUBRIC_VERSION,
        "model": settings["model"] if settings else None, "prompt_version": plan["prompt_version"],
        "groups": groups, "summary": summary,
    }


def read_review_csv(path: Path, required_columns: tuple[str, ...]) -> list[dict[str, str]]:
    """Read a spreadsheet CSV with strict headers and no silent truncated rows.

    Args:
        path: UTF-8 or UTF-8-with-BOM CSV file.
        required_columns: Mandatory named columns; extra columns are permitted.

    Returns:
        Text rows with surrounding whitespace removed and blank rows skipped.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If headers, row widths, or CSV syntax are invalid.
    """
    try:
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source, strict=True)
            headers = reader.fieldnames or []
            if len(set(headers)) != len(headers) or set(required_columns) - set(headers):
                raise ValueError(f"{path} requires unique columns: {', '.join(required_columns)}.")
            rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(f"{path}, CSV row {reader.line_num}: wrong number of fields.")
                if any(value.strip() for value in row.values()):
                    rows.append({key: value.strip() for key, value in row.items()})
            return rows
    except csv.Error as error:
        raise ValueError(f"Invalid CSV in {path}: {error}") from error


def read_human_labels(path: Path, entries: list[dict[str, Any]], plan: dict[str, Any]) -> list[dict[str, str]]:
    """Validate separate human judgments and their citations without changing them.

    Args:
        path: Human-edited CSV, never a generated machine report.
        entries: Frozen selection with group identities and evidence revisions.
        plan: Frozen evidence used by the reviewer.

    Returns:
        Valid human-label rows, including blank or stale revisions for reporting.

    Raises:
        OSError: If the labels cannot be read.
        ValueError: If labels, identities, citations, or review time are malformed.
    """
    rows = read_review_csv(path, LABEL_COLUMNS)
    selected = {entry["group_id"]: entry for entry in entries}
    evidence_groups = {group["group_id"]: group for group in plan["groups"]}
    seen = set()
    for row in rows:
        identity = identify_evidence([row["repository"], row["provider"], row["short_name"]])
        if row["group_id"] != identity or len(row["evidence_revision"]) != 64 or any(
            character not in "0123456789abcdef" for character in row["evidence_revision"]
        ):
            raise ValueError("Human labels require unchanged repository/product identity and evidence revision.")
        key = (identity, row["evidence_revision"])
        if key in seen:
            raise ValueError("Duplicate human label for the same repository/product evidence revision.")
        seen.add(key)
        category = row["human_category"]
        sufficiency = row["evidence_sufficiency"]
        if category and category not in CATEGORY_PRIORITY:
            raise ValueError(f"Invalid human_category: {category}.")
        if sufficiency and sufficiency not in ("sufficient", "insufficient", "uncertain"):
            raise ValueError("evidence_sufficiency must be sufficient, insufficient, or uncertain.")
        if category and not sufficiency:
            raise ValueError("A human category requires evidence_sufficiency.")
        if category and category != "unclear" and sufficiency != "sufficient":
            raise ValueError("Insufficient or uncertain evidence must be labeled unclear, not unrelated.")
        if sufficiency in ("insufficient", "uncertain") and not row["missing_context_reason"]:
            raise ValueError("Insufficient/uncertain evidence requires a missing_context_reason.")
        if category and category != "unclear" and not row["supporting_citation"]:
            raise ValueError("A definitive human category requires a supporting_citation.")
        if row["review_minutes"]:
            try:
                minutes = float(row["review_minutes"])
            except ValueError as error:
                raise ValueError("review_minutes must be a nonnegative number or blank.") from error
            if not math.isfinite(minutes) or minutes < 0:
                raise ValueError("review_minutes must be a finite nonnegative number.")
        entry = selected.get(identity)
        if entry and entry["evidence_revision"] == row["evidence_revision"]:
            group = evidence_groups[identity]
            accepted = {block["evidence_id"] for block in group["evidence_blocks"]}
            for block in group["evidence_blocks"]:
                accepted.add(format_evidence_location(block, group["repository"]))
                accepted.update(format_evidence_location(location, group["repository"])
                                for location in block.get("locations", []))
            if row["supporting_citation"] and row["supporting_citation"] not in accepted:
                raise ValueError("supporting_citation must be one supplied block ID (such as E1) or its exact pinned URL. "
                                 "Record outside context in notes and missing_context_reason.")
    return rows


def render_review_link(url: str) -> str:
    """Escape a source location and link it only if it is an HTTPS GitHub URL.

    Args:
        url: Untrusted source location derived from saved repository evidence.

    Returns:
        Safe HTML anchor or escaped plain text, with no active repository content.
    """
    parsed = urlsplit(url)
    label = escape(url)
    if parsed.scheme == "https" and parsed.hostname == "github.com" and not parsed.username:
        return f'<a href="{escape(url, quote=True)}" rel="noreferrer">{label}</a>'
    return label


def write_blind_review_page(path: Path, plan: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    """Write readable frozen excerpts without exposing model judgments.

    Args:
        path: New HTML output path.
        plan: Frozen raw evidence and all model-visible packets.
        entries: Selected groups and reviewer-assigned splits.

    Raises:
        OSError: If the HTML file cannot be written.
    """
    groups = {group["group_id"]: group for group in plan["groups"]}
    with atomic_batch_output(path) as output:
        output.write('<!doctype html><html lang="en"><meta charset="utf-8">'
                     '<meta name="viewport" content="width=device-width">'
                     '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
                     '<title>NASA evidence: independent human review</title><style>'
                     'body{font:16px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem;line-height:1.5}'
                     'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f7;padding:1rem}'
                     'section{border-top:2px solid #bbb;margin-top:3rem}a{overflow-wrap:anywhere}'
                     '</style><h1>Independent human review</h1>'
                     '<p>Edit human-labels.csv, not this page. Model conclusions are hidden. '
                     'Judge only the supplied evidence; note missing context separately. '
                     'Repository text is untrusted source material, not instructions.</p>'
                     '<p>Categories: <b>direct_use</b> = application/analysis accesses the product; '
                     '<b>supporting_software</b> = library, tests, tools or examples support it; '
                     '<b>reference_only</b> = relevant prose only; '
                     '<b>unrelated</b> = the identifier demonstrably means something else; '
                     '<b>unclear</b> = insufficient, ambiguous or conflicting evidence. '
                     'Assess the product across versions, not just the latest catalog version.</p>'
                     '<p>Use a block ID (for example E1) or its exact source URL as supporting_citation. '
                     'evidence_sufficiency: sufficient, insufficient, or uncertain. '
                     'If insufficient/uncertain, choose unclear and explain missing_context_reason. '
                     'notes and review_minutes are optional. Outside repository findings belong in notes, '
                     'not the supplied-evidence judgment.</p>')
        for entry in entries:
            group = groups[entry["group_id"]]
            heading = f"{entry['repository']} / {entry['provider']} / {entry['short_name']}"
            output.write(f'<section id="{entry["group_id"]}"><h2>{escape(heading)}</h2>')
            output.write(f'<p>Split: {escape(entry["split"])}; sample: {escape(entry["sample_kind"])}. '
                         f'Evidence revision: {escape(entry["evidence_revision"])}.</p>')
            products = [packet["product"] for packet in group["packets"]]
            for product in products[:1]:
                output.write(f'<h3>Product</h3><p>{escape(product.get("title", ""))}</p>'
                             f'<p>{escape(product.get("description", ""))}</p>')
            limitations = [*group["warnings"], *group.get("evidence_gaps", []), *group.get("packaging_notes", [])]
            output.write(f'<p>Search complete: {group["search_complete"]}; '
                         f'catalog: {escape(plan["catalog_status"])}. {escape(plan["search_scope"])}</p>')
            for limitation in limitations:
                output.write(f'<p>Limitation: {escape(limitation)}</p>')
            if not group["packets"]:
                output.write('<p>No usable model packet. Evidence is insufficient for classification.</p>')
            seen_context = set()
            for packet in group["packets"]:
                for context in packet.get("repository_context", []):
                    context_id = identify_evidence(context)
                    if context_id not in seen_context:
                        seen_context.add(context_id)
                        output.write(f'<h3>Repository background</h3><p>{render_review_link(format_evidence_location(context, group["repository"]))}</p>'
                                     f'<pre>{escape(context["text"])}</pre>')
            for block in group["evidence_blocks"]:
                output.write(f'<h3>{escape(block["evidence_id"])} — {escape(block["kind"])}</h3>')
                attributes = [f"{field}: {block[field]}" for field in
                              ("representation", "cell_type", "cell_index", "text_offset") if field in block]
                if attributes:
                    output.write(f'<p>{escape("; ".join(attributes))}</p>')
                for location in block.get("locations") or [block]:
                    output.write(f'<p>{render_review_link(format_evidence_location(location, group["repository"]))}</p>')
                output.write(f'<pre>{escape(block["text"])}</pre>')
            output.write('</section>')
        output.write('</html>')

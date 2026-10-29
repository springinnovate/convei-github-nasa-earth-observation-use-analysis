"""Add bounded enclosing source code to newly prepared file assessments."""

from pathlib import Path
from typing import Any

from nasa_eo_search.evidence_inputs import read_evidence_json
from nasa_eo_search.notebook_context import add_notebook_classification_context
from nasa_eo_search.snapshot_evidence import (
    MAX_EVIDENCE_CHARACTERS, MATCHING_WINDOW_CHARACTERS,
    build_snapshot_excerpt_citation, read_matched_files_from_snapshot_zip,
    validate_saved_snapshot_file,
)
from nasa_eo_search.source_context import select_enclosing_source_lines


MAX_ADDITIONAL_CONTEXT_CHARACTERS = 6000


def read_repository_classification_sources(source_plan: dict[str, Any], repository: str) -> dict[str, list[str]]:
    """Read matched files from one verified ZIP for new classification context.

    Compare the manifest with the source identity already captured by the search
    reader, then verify the scan and archive checksums again before reading.
    Nothing is extracted or executed. Call once per repository and release the
    returned text when moving to the next repository.

    Args:
        source_plan: Validated GitHub search groups and frozen snapshot identities.
        repository: Repository whose matched files need enclosing code.

    Returns:
        Complete source lines keyed by repository-relative file path.

    Raises:
        ValueError: If snapshot identity changed, a checksum differs, or a saved
            file is missing or unsafe to read.
        OSError: If a saved artifact cannot be read.
    """
    directory = Path(source_plan["batch_directory"])
    manifest = read_evidence_json(directory / "snapshots.json")
    snapshots = [record for record in manifest["repositories"] if record["repository"] == repository]
    identities = [record["snapshot"] for record in source_plan["source_product_queries"]
                  if record["snapshot"]["repository"] == repository]
    if len(snapshots) != 1 or len(identities) != 1:
        raise ValueError("Snapshot identity changed before preparing classification context.")
    snapshot, expected_identity = snapshots[0], identities[0]
    if any(snapshot.get(field) != value for field, value in expected_identity.items()):
        raise ValueError("Snapshot identity changed before preparing classification context.")
    archive_path = validate_saved_snapshot_file(
        directory, snapshot["archive_relative_path"], expected_identity["archive_sha256"])
    scan_path = validate_saved_snapshot_file(
        directory, snapshot["scan_relative_path"], expected_identity["scan_sha256"])
    requested_paths = {record["file"] for group in source_plan["groups"]
                       if group["repository"] == repository for record in group["files"]}
    return read_matched_files_from_snapshot_zip(archive_path, read_evidence_json(scan_path), requested_paths)


def add_enclosing_classification_context(group: dict[str, Any], source_lines: list[str]) -> None:
    """Add cited structural context without changing original matching excerpts.

    Use enclosing definitions and local configuration relationships to explain
    what a matched identifier does. Keep at most 6,000 additional text characters,
    and add nothing beyond a combined 12,000-character evidence budget. Existing
    evidence is never discarded, including larger legacy Sourcegraph excerpts.
    Every new excerpt keeps its original file, commit, line, and character offset.
    Report parsing fallbacks and omitted text in the assessment's evidence gaps.
    Notebook sections use decoded source cells; stored outputs are not added.

    Args:
        group: One file/product assessment with raw evidence and evidence_gaps;
            modified in place. Original match_row and matching_lines stay intact.
        source_lines: Complete lines from the same pinned file. Saved matching
            excerpts must agree with these lines before any context is added.
    """
    file_record = group["files"][0]
    for matching_line in file_record["matching_lines"]:
        number = matching_line["line_number"]
        offset = matching_line.get("character_offset", 0)
        text = matching_line["text"]
        if not (0 < number <= len(source_lines)
                and source_lines[number - 1][offset:offset + len(text)] == text):
            group["evidence_gaps"].append("Enclosing code omitted: saved matches differ from the pinned source text.")
            return
    if file_record["file"].lower().endswith(".ipynb"):
        add_notebook_classification_context(group, source_lines, MAX_ADDITIONAL_CONTEXT_CHARACTERS,
                                            MAX_EVIDENCE_CHARACTERS, MATCHING_WINDOW_CHARACTERS)
        return
    selection = select_enclosing_source_lines(
        file_record["file"], source_lines,
        [line["line_number"] for line in file_record["matching_lines"]])
    group["evidence_gaps"].extend(selection["notes"])
    existing_numbers = {item["line_number"] for item in group["evidence"]
                        if item["file"] == file_record["file"] and item["commit"] == file_record["commit"]}
    remaining_characters = min(MAX_ADDITIONAL_CONTEXT_CHARACTERS, max(
        0, MAX_EVIDENCE_CHARACTERS - sum(len(item["text"]) for item in group["evidence"])))
    omitted_text = False
    for number in selection["line_numbers"]:
        if number in existing_numbers:
            continue
        line_text = source_lines[number - 1]
        if not remaining_characters:
            omitted_text = True
            break
        text = line_text[:min(MATCHING_WINDOW_CHARACTERS, remaining_characters)]
        omitted_text |= len(text) < len(line_text)
        remaining_characters -= len(text)
        group["evidence"].append(build_snapshot_excerpt_citation(
            group["repository"], file_record["commit"], file_record["file"],
            number, 0, text, "enclosing_code"))
        existing_numbers.add(number)
    if omitted_text:
        group["evidence_gaps"].append(
            "Enclosing code truncated: at most 6,000 added text characters, 600 per line, "
            "within the 12,000-character combined evidence budget; excerpts may omit behavior.")

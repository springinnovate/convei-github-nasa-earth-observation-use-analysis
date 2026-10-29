"""Add bounded notebook source cells with pinned raw-source citations."""

from bisect import bisect_right
import json
import re
from typing import Any

from nasa_eo_search.dataset_identity import find_product_identifier_occurrences
from nasa_eo_search.evidence_notebooks import decode_notebook_source_cells
from nasa_eo_search.snapshot_evidence import build_snapshot_excerpt_citation


MAX_NOTEBOOK_BYTES = 2_000_000
MAX_NOTEBOOK_CELLS = 512


def anchor_notebook_source_matches(cells: list[dict[str, Any]], group: dict[str, Any], contents: str) -> None:
    """Locate literal search terms inside source values on compact matching lines.

    A raw line can contain both source and outputs. Require an accepted product
    identifier within the source value itself before promoting that cell to a
    matching cell. Output-only names therefore cannot pull in unrelated code.

    Args:
        cells: Decoded cells, updated only when literal source contains a match.
        group: Saved product/file evidence and optional dataset-identity rules.
        contents: Verified raw notebook JSON used for exact source offsets.
    """
    file_record = group["files"][0]
    terms = {source["term"] for source in file_record.get("sources", [])}
    if not terms and group.get("product", {}).get("short_name"):
        terms.add(group["product"]["short_name"])
    matching_lines = {item["line_number"] for item in group["evidence"] if item["kind"] == "matching_line"}
    for cell in cells:
        for mapping in cell["source_line_map"]:
            if (any(mapping["line_number"] <= number <= mapping["end_line"] for number in matching_lines)
                    and any(find_product_identifier_occurrences(
                        term, contents[mapping["raw_start"]:mapping["raw_end"]], group.get("identifier_identity_rules"),
                    ) for term in terms)):
                cell["kind"] = "matching_line"
                break


def notebook_heading_level(cell: dict[str, Any]) -> int | None:
    """Find the highest Markdown heading level within a source cell.

    Args:
        cell: Decoded notebook source cell with type and text.

    Returns:
        Smallest heading level, or None for code and heading-free prose.
    """
    if cell["cell_type"] != "markdown":
        return None
    headings = re.findall(r"(?m)^\s{0,3}(#{1,6})[ \t]+", cell["text"])
    return min(map(len, headings)) if headings else None


def select_notebook_context_cells(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Choose matched cells and bounded sections that explain their code use.

    Include the first prose cell for purpose, then source cells in each matched
    section. A following heading of equal or higher level ends that section;
    this keeps adjacent dataset demonstrations separate. Context is not itself
    evidence that the target product supplies another cell's values.

    Args:
        cells: Source cells in original notebook order, with matching cells marked.

    Returns:
        Matched cells first, purpose prose next, then section context in file order.
        No context is selected when matches cannot be placed in source cells.
    """
    anchors = [number for number, cell in enumerate(cells) if cell["kind"] == "matching_line"]
    if not anchors:
        return []
    selected_indices = set(anchors)
    for anchor in anchors:
        heading_index = next((number for number in range(anchor, -1, -1)
                              if notebook_heading_level(cells[number]) is not None), None)
        heading_level = notebook_heading_level(cells[heading_index]) if heading_index is not None else None
        first_index = heading_index if heading_index is not None else 0
        last_index = len(cells)
        if heading_level is not None:
            last_index = next((number for number in range(anchor + 1, len(cells))
                               if notebook_heading_level(cells[number]) is not None
                               and notebook_heading_level(cells[number]) <= heading_level), len(cells))
        selected_indices.update(range(first_index, last_index))
    purpose_index = next((number for number, cell in enumerate(cells)
                          if cell["cell_type"] == "markdown" and cell["text"].strip()), None)
    priority_indices = [*anchors]
    if purpose_index is not None:
        priority_indices.append(purpose_index)
    priority_indices.extend(sorted(selected_indices))
    return [cells[number] for number in dict.fromkeys(priority_indices)]


def create_notebook_source_citations(
    cell: dict[str, Any], contents: str, line_offsets: list[int], maximum_line_characters: int,
) -> list[dict[str, Any]]:
    """Copy only raw source-value fragments into immutable audit citations.

    Args:
        cell: Decoded cell with raw offsets for each source string.
        contents: Original complete notebook JSON, including unselected outputs.
        line_offsets: Character position of each original source line.
        maximum_line_characters: Maximum characters per raw citation fragment.

    Returns:
        Exact raw source-string fragments with file, revision and character offsets.
        Stored outputs and metadata are never included in these new citations.
    """
    citations = []
    for source_mapping in cell["source_line_map"]:
        position = source_mapping["raw_start"]
        while position < source_mapping["raw_end"]:
            line_number = bisect_right(line_offsets, position)
            line_end = (line_offsets[line_number] - 1 if line_number < len(line_offsets) else len(contents))
            fragment_end = min(source_mapping["raw_end"], line_end, position + maximum_line_characters)
            if fragment_end > position:
                citations.append(build_snapshot_excerpt_citation(
                    cell["repository"], cell["commit"], cell["file"], line_number,
                    position - line_offsets[line_number - 1], contents[position:fragment_end], "enclosing_code"))
                position = fragment_end
            else:
                position += 1
    return citations


def map_notebook_source_citations(
    cell: dict[str, Any], citations: list[dict[str, Any]], group: dict[str, Any],
    contents: str, line_offsets: list[int],
) -> None:
    """Limit matching citation IDs to exact decoded identifier spans within a cell.

    A compact notebook puts unrelated source strings on the same raw line.
    Track accepted literal occurrences through JSON decoding rather than copying
    that line's matching IDs to every source part or later packet fragment.

    Args:
        cell: Newly decoded cell whose source map and context policy are updated.
        citations: Added enclosing-code citations covering the cell's raw strings.
        group: Original file/product evidence and dataset-identity rules.
        contents: Verified notebook JSON used to map exact raw character ranges.
        line_offsets: Character position of each original raw line.
    """
    file_record = group["files"][0]
    terms = {source["term"] for source in file_record.get("sources", [])}
    if not terms and group.get("product", {}).get("short_name"):
        terms.add(group["product"]["short_name"])
    original_matches = [item for item in group["evidence"] if item["kind"] == "matching_line"]
    source_map = []
    contains_cited_match = False
    for mapping in cell["source_line_map"]:
        decoded_text = cell["text"][mapping["text_start"]:mapping["text_end"]]
        raw_text = contents[mapping["raw_start"]:mapping["raw_end"]]
        boundaries = {mapping["text_start"]: mapping["raw_start"] + 1,
                      mapping["text_end"]: mapping["raw_end"] - 1}
        matched_ranges = []
        if cell["kind"] == "matching_line":
            for term in terms:
                decoded_occurrences = set(find_product_identifier_occurrences(
                    term, decoded_text, group.get("identifier_identity_rules")))
                for raw_start, raw_end in find_product_identifier_occurrences(
                    term, raw_text, group.get("identifier_identity_rules"),
                ):
                    try:
                        decoded_start = len(json.loads(raw_text[:raw_start] + '"'))
                    except (ValueError, TypeError):
                        continue
                    decoded_end = decoded_start + len(term)
                    if (decoded_start, decoded_end) not in decoded_occurrences:
                        continue
                    absolute_start = mapping["raw_start"] + raw_start
                    absolute_end = mapping["raw_start"] + raw_end
                    matching_ids = []
                    for evidence in original_matches:
                        evidence_start = line_offsets[evidence["line_number"] - 1] + evidence.get("character_offset", 0)
                        if evidence_start <= absolute_start < absolute_end <= evidence_start + len(evidence["text"]):
                            matching_ids.append(evidence["evidence_id"])
                    if matching_ids:
                        text_start, text_end = mapping["text_start"] + decoded_start, mapping["text_start"] + decoded_end
                        boundaries.update({text_start: absolute_start, text_end: absolute_end})
                        matched_ranges.append((text_start, text_end, matching_ids))
        ordered_boundaries = sorted(boundaries)
        for text_start, text_end in zip(ordered_boundaries, ordered_boundaries[1:]):
            raw_start, raw_end = boundaries[text_start], boundaries[text_end]
            source_ids = []
            for citation in citations:
                citation_start = line_offsets[citation["line_number"] - 1] + citation["character_offset"]
                if citation_start < raw_end and citation_start + len(citation["text"]) > raw_start:
                    source_ids.append(citation["evidence_id"])
            for matching_start, matching_end, matching_ids in matched_ranges:
                if matching_start < text_end and matching_end > text_start:
                    source_ids.extend(matching_ids)
                    contains_cited_match = True
            line_number = bisect_right(line_offsets, raw_start)
            source_map.append({
                "line_number": line_number, "end_line": bisect_right(line_offsets, raw_end - 1),
                "character_offset": raw_start - line_offsets[line_number - 1],
                "text_start": text_start, "text_end": text_end,
                "source_ids": list(dict.fromkeys(source_ids)),
            })
    cell["source_line_map"] = source_map
    cell["source_ids"] = list(dict.fromkeys(source_id for mapping in source_map for source_id in mapping["source_ids"]))
    cell["kind"] = "matching_line" if contains_cited_match else "enclosing_code"
    cell["context_kind"] = "enclosing_code"


def add_notebook_classification_context(
    group: dict[str, Any], source_lines: list[str], maximum_added_characters: int,
    maximum_total_characters: int, maximum_line_characters: int,
) -> None:
    """Add selected source cells without executing code or adding notebook outputs.

    Preserve all original matching evidence. Every decoded cell retains precise
    raw-source mappings. Whole cells that exceed the remaining raw/decoded text
    budget are omitted with a note rather than clipped into unsupported claims.

    Args:
        group: One file/product assessment, updated with source cells and citations.
        source_lines: Verified lines from the same pinned notebook file.
        maximum_added_characters: Additional raw or decoded text budget.
        maximum_total_characters: Combined raw evidence character ceiling.
        maximum_line_characters: Maximum length of each additional raw fragment.
    """
    file_record = group["files"][0]
    contents = "\n".join(source_lines)
    if len(contents.encode("utf-8", errors="replace")) > MAX_NOTEBOOK_BYTES:
        group["evidence_gaps"].append("Notebook source decoding omitted: source exceeds the 2 MB parsing limit.")
        return
    cells, _, gaps = decode_notebook_source_cells(contents, file_record, group["evidence"], include_context=True)
    group["evidence_gaps"].extend(gaps)
    anchor_notebook_source_matches(cells[:MAX_NOTEBOOK_CELLS], group, contents)
    selected_cells = select_notebook_context_cells(cells[:MAX_NOTEBOOK_CELLS])
    if not selected_cells:
        group["evidence_gaps"].append("Notebook context omitted: matching text could not be anchored to complete source cells.")
        return
    if len(cells) > MAX_NOTEBOOK_CELLS:
        group["evidence_gaps"].append(f"Notebook context considers the first {MAX_NOTEBOOK_CELLS} source cells.")
    remaining = min(maximum_added_characters, max(0, maximum_total_characters
                                                - sum(len(item["text"]) for item in group["evidence"])))
    retained_cells = group.setdefault("notebook_cells", [])
    retained_indices = {cell["cell_index"] for cell in retained_cells
                        if cell["file"] == file_record["file"] and cell["commit"] == file_record["commit"]}
    line_offsets = [0, *[match.end() for match in re.finditer("\n", contents)]]
    represented_lines = set()
    omitted_cells = 0
    for cell in selected_cells:
        if cell["cell_index"] in retained_indices or cell["cell_type"] not in {"code", "markdown"} or not cell["text"].strip():
            continue
        citations = create_notebook_source_citations(cell, contents, line_offsets, maximum_line_characters)
        character_cost = max(len(cell["text"]), sum(len(item["text"]) for item in citations))
        if character_cost > remaining or not citations:
            omitted_cells += 1
            continue
        remaining -= character_cost
        group["evidence"].extend(citations)
        map_notebook_source_citations(cell, citations, group, contents, line_offsets)
        represented_lines.update(cell.pop("complete_source_lines"))
        retained_cells.append(cell)
    group.setdefault("notebook_decoded_lines", []).append({
        "file": file_record["file"], "commit": file_record["commit"], "line_numbers": sorted(represented_lines),
    })
    group["evidence_gaps"].append(
        "Notebook context follows matched source sections; cells are supplied as context, "
        "not proof that every operation uses the target product. Stored outputs and metadata were not added.")
    if omitted_cells:
        group["evidence_gaps"].append(
            f"Notebook context omitted {omitted_cells} cells to keep at most {maximum_added_characters:,} added "
            f"and {maximum_total_characters:,} total text characters; later operations may be missing.")

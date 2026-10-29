"""Decode notebook source cells while preserving immutable raw-source citations."""

from bisect import bisect_right
import json
import re
from typing import Any


def locate_json_object_fields(text: str, start: int) -> dict[str, tuple[Any, int, int]]:
    """Locate an object's values without confusing escaped strings with syntax.

    Args:
        text: Complete JSON document.
        start: Character offset of the object's opening brace.

    Returns:
        Field values and their start/end character offsets.

    Raises:
        ValueError: If the object has duplicate names or malformed JSON.
    """
    decoder = json.JSONDecoder()
    position = start + 1
    fields = {}
    while True:
        while position < len(text) and text[position].isspace():
            position += 1
        if text[position] == "}":
            return fields
        name, position = decoder.raw_decode(text, position)
        if not isinstance(name, str) or name in fields:
            raise ValueError("Notebook contains invalid or duplicate object fields.")
        while text[position].isspace():
            position += 1
        if text[position] != ":":
            raise ValueError("Notebook field is missing a colon.")
        position += 1
        while text[position].isspace():
            position += 1
        value, end = decoder.raw_decode(text, position)
        fields[name] = (value, position, end)
        position = end
        while text[position].isspace():
            position += 1
        if text[position] == ",":
            position += 1
        elif text[position] != "}":
            raise ValueError("Notebook object has malformed separators.")


def decode_notebook_source_cells(
    contents: str, file_record: dict[str, Any], evidence: list[dict[str, Any]],
    *, include_context: bool = False,
) -> tuple[list[dict[str, Any]], set[int], list[str]]:
    """Extract matched notebook cells, retaining raw line spans for every citation.

    Decode source strings only, never execute cells. Matches in outputs, metadata,
    or unsupported layouts are deliberately left for the raw-evidence fallback.

    Args:
        contents: Complete downloaded notebook JSON.
        file_record: Immutable repository, file, and commit identity.
        evidence: Saved raw matching and surrounding line fragments.
        include_context: Also decode unmatched source cells for bounded context
            selection. Their kind remains enclosing_code, and raw offsets are
            supplied so callers can retain exact source citations.

    Returns:
        Decoded cell blocks, raw line numbers represented by complete source
        lines, and explicit limitations requiring raw-evidence fallback.
    """
    blocks = []
    represented_lines = set()
    try:
        notebook = json.loads(contents)
        if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
            raise ValueError("Notebook does not contain a cells array.")
        root_start = len(contents) - len(contents.lstrip())
        root_fields = locate_json_object_fields(contents, root_start)
        cells, position, unused_end = root_fields["cells"]
        position += 1
        line_offsets = [0, *[match.end() for match in re.finditer("\n", contents)]]
        matching_numbers = {item["line_number"] for item in evidence if item["kind"] == "matching_line"}
        source_lines = contents.splitlines()
        decoder = json.JSONDecoder()
        for cell_index, cell in enumerate(cells):
            while contents[position].isspace() or contents[position] == ",":
                position += 1
            cell_start = position
            unused_value, position = decoder.raw_decode(contents, position)
            if not isinstance(cell, dict):
                raise ValueError("Notebook cells must be objects.")
            fields = locate_json_object_fields(contents, cell_start)
            if "source" not in fields:
                continue
            source, source_start, source_end = fields["source"]
            start_line = bisect_right(line_offsets, source_start)
            end_line = bisect_right(line_offsets, source_end - 1)
            complete_source_lines = {
                number for number in range(start_line, end_line + 1)
                if source_start <= line_offsets[number - 1]
                and line_offsets[number - 1] + len(source_lines[number - 1]) <= source_end
            }
            # One minified raw line can contain source, outputs, and metadata.
            # Line-only search evidence cannot establish which field matched.
            contains_match = bool(complete_source_lines.intersection(matching_numbers))
            if not contains_match and not include_context:
                continue
            if isinstance(source, str):
                cell_text = source
                source_parts = [(source, source_start, source_end)]
            elif isinstance(source, list) and all(isinstance(part, str) for part in source):
                cell_text = "".join(source)
                source_parts = []
                part_position = source_start + 1
                for part in source:
                    while contents[part_position].isspace() or contents[part_position] == ",":
                        part_position += 1
                    unused_part, part_end = decoder.raw_decode(contents, part_position)
                    source_parts.append((part, part_position, part_end))
                    part_position = part_end
            else:
                continue
            source_ids = [item["evidence_id"] for item in evidence
                          if start_line <= item["line_number"] <= end_line]
            source_line_map = []
            text_offset = 0
            for part, part_start, part_end in source_parts:
                part_start_line = bisect_right(line_offsets, part_start)
                part_end_line = bisect_right(line_offsets, part_end - 1)
                source_line_map.append({
                    "line_number": part_start_line, "end_line": part_end_line,
                    "character_offset": part_start - line_offsets[part_start_line - 1],
                    "text_start": text_offset, "text_end": text_offset + len(part),
                    "source_ids": [item["evidence_id"] for item in evidence
                                   if part_start_line <= item["line_number"] <= part_end_line],
                    **({"raw_start": part_start, "raw_end": part_end} if include_context else {}),
                })
                text_offset += len(part)
            blocks.append({
                **{name: file_record[name] for name in ("repository", "file", "commit")},
                "line_number": start_line, "end_line": end_line, "text": cell_text,
                "kind": "matching_line" if contains_match else "enclosing_code", "cell_index": cell_index,
                "cell_type": cell.get("cell_type", "unknown"), "source_ids": source_ids,
                "representation": "notebook_source_cell", "source_line_map": source_line_map,
                **({"complete_source_lines": sorted(complete_source_lines)} if include_context else {}),
            })
            # Only omit raw lines wholly inside this source field. A compact
            # one-line notebook can also contain output matches on the same line.
            represented_lines.update(complete_source_lines)
        remaining = matching_numbers - represented_lines
        gaps = ([f"{file_record['file']}: notebook matches outside complete decoded source lines "
                 "are retained as raw JSON evidence (outputs, metadata, or compact layout)."] if remaining else [])
        return blocks, represented_lines, gaps
    except (ValueError, KeyError, IndexError, RecursionError, TypeError):
        return [], set(), [f"{file_record['file']}: notebook could not be decoded; original matching text is retained."]


def decode_saved_notebook_line(text: str) -> tuple[str, bool]:
    """Decode a saved JSON string line when complete notebook cells are unavailable.

    Args:
        text: Reassembled raw line from an archived notebook excerpt.

    Returns:
        Readable decoded text and whether the entire line was a JSON string item.
        Unsupported or partial JSON is returned unchanged, without discarding it.
    """
    candidate = text.strip()
    if candidate.endswith(","):
        candidate = candidate[:-1].rstrip()
    if not candidate.startswith('"'):
        return text, False
    try:
        decoded = json.loads(candidate)
    except (ValueError, RecursionError):
        return text, False
    return (decoded, True) if isinstance(decoded, str) else (text, False)

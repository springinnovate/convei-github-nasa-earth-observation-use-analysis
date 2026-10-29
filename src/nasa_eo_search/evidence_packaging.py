"""Build compact, lossless matched-code packets with a separate provenance audit."""

from collections import defaultdict
from typing import Any

from nasa_eo_search.evidence_inputs import serialize_evidence
from nasa_eo_search.evidence_notebooks import decode_saved_notebook_line


def separate_conflicting_line_fragments(
    fragments: list[dict[str, Any]], original_lines: list[str],
) -> tuple[list[list[dict[str, Any]]], bool]:
    """Keep fragments from contradictory saved lines out of the same text variant.

    Args:
        fragments: Raw citations for one file, revision, line number, and kind.
        original_lines: Complete saved matching-line alternatives, when available.

    Returns:
        Independently reassemblable fragment groups and whether offsets conflict.
        Original text is used only to associate existing fragments, never to fill
        missing characters. Unassignable fragments remain separate and unchanged.
    """
    texts_by_offset: dict[int, set[str]] = defaultdict(set)
    for fragment in fragments:
        texts_by_offset[fragment.get("character_offset", 0)].add(fragment["text"])
    conflicts = len(set(original_lines)) > 1 or any(len(texts) > 1 for texts in texts_by_offset.values())
    if not conflicts:
        return [fragments], False
    fragment_groups = []
    assigned_ids = set()
    for original_line in sorted(set(original_lines)):
        matching_fragments = []
        for fragment in fragments:
            offset = fragment.get("character_offset", 0)
            if original_line[offset:offset + len(fragment["text"])] == fragment["text"]:
                matching_fragments.append(fragment)
                assigned_ids.add(fragment["evidence_id"])
        if matching_fragments:
            fragment_groups.append(matching_fragments)
    fragment_groups.extend([fragment] for fragment in fragments if fragment["evidence_id"] not in assigned_ids)
    return fragment_groups, True


def reassemble_evidence_lines(
    evidence: list[dict[str, Any]], matching_files: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Join adjacent excerpts from the same source line, keeping their citations.

    The ``evidence`` argument supplies every excerpt to include in the result.
    Complete lines in ``matching_files`` are used only to check whether two
    excerpts came from conflicting versions of a line. A shortened excerpt
    cannot serve as a complete-line comparison, so it is left out of that
    lookup, not removed from the evidence being returned.

    Args:
        evidence: Excerpts with text, source location, character offset, and
            citation ID. This list is not modified.
        matching_files: Optional file records with matching-line text. Only
            whole lines are used to distinguish conflicting source text;
            records marked ``excerpt`` or carrying ``character_offset`` are
            not whole-line comparisons, even when their offset is zero.

    Returns:
        Ordered line records with all raw citation IDs and character offsets.
        Missing character ranges remain separate records rather than being joined.
        Conflicting variants without original provenance remain separate fragments.
    """
    complete_lines_by_location: dict[tuple[str, str, int], list[str]] = defaultdict(list)
    for file_record in matching_files or []:
        for matching_line in file_record.get("matching_lines", []):
            if matching_line.get("excerpt") or "character_offset" in matching_line:
                # This lookup is only for comparing whole lines. The excerpt
                # still enters fragments_by_line through `evidence` below.
                # Treating two different windows as whole lines would falsely
                # report conflicting source text at the same file/line.
                continue
            complete_lines_by_location[(file_record["file"], file_record.get("commit") or "",
                                        matching_line["line_number"])].append(matching_line["text"])
    fragments_by_line = defaultdict(list)
    for item in evidence:
        fragments_by_line[(item["file"], item["commit"] or "", item["line_number"], item["kind"])].append(item)
    lines = []
    for line_identity, fragments in sorted(fragments_by_line.items()):
        original_lines = complete_lines_by_location.get(line_identity[:3], []) if line_identity[3] == "matching_line" else []
        fragment_groups, conflicting_fragments = separate_conflicting_line_fragments(fragments, original_lines)
        for fragment_group in fragment_groups:
            current_line = None
            for fragment in sorted(fragment_group, key=lambda item: item.get("character_offset", 0)):
                offset = fragment.get("character_offset", 0)
                if current_line is None or current_line["character_offset"] + len(current_line["text"]) != offset:
                    current_line = {name: fragment[name] for name in ("repository", "file", "commit", "line_number", "kind")}
                    current_line.update(text="", character_offset=offset, source_ids=[])
                    if conflicting_fragments:
                        current_line["conflicting_fragments"] = True
                    lines.append(current_line)
                current_line["text"] += fragment["text"]
                current_line["source_ids"].append(fragment["evidence_id"])
    return lines


def make_block_location(block: dict[str, Any]) -> dict[str, Any]:
    """Describe a complete source occurrence for a deduplicated audit block.

    Args:
        block: Readable evidence block before or after deduplication.

    Returns:
        Source identity, raw citation IDs, and optional notebook coordinates.
    """
    names = ("repository", "file", "commit", "line_number", "end_line", "source_ids",
             "cell_index", "cell_type", "representation", "source_line_map")
    return {name: block[name] for name in names if name in block}


def build_readable_evidence_blocks(group: dict[str, Any]) -> list[dict[str, Any]]:
    """Combine neighboring lines and deduplicate exact text with all locations.

    Args:
        group: Enriched repository/product group; raw evidence is never modified.

    Returns:
        Readable matched-code blocks with deterministic short citation IDs.
        Material notebook decoding limitations are added to evidence_gaps.
    """
    lines = reassemble_evidence_lines(group["evidence"], group.get("files"))
    decoded_lines = {(entry["file"], entry["commit"], number)
                     for entry in group.get("notebook_decoded_lines", []) for number in entry["line_numbers"]}
    blocks = [{**cell, "source_ids": list(cell["source_ids"])} for cell in group.get("notebook_cells", [])]
    current_block = None
    gaps = set(group.get("evidence_gaps", []))
    for line in lines:
        if line["kind"] == "readme_context" or (line["file"], line["commit"], line["line_number"]) in decoded_lines:
            continue
        line_text = line["text"]
        representation = "source_lines"
        if line.get("conflicting_fragments"):
            gaps.add(f"{line['file']}:{line['line_number']}: saved matches disagree at this revision and line; "
                     "original variants are kept separate, or fragments remain separate when their origin is uncertain.")
        if line["character_offset"]:
            gaps.add(f"{line['file']}:{line['line_number']}: source line starts at character offset "
                     f"{line['character_offset']}; missing earlier characters were not reconstructed.")
        if line["file"].lower().endswith(".ipynb"):
            line_text, decoded = decode_saved_notebook_line(line_text)
            representation = "decoded_notebook_fragment" if decoded else "raw_notebook_json"
            if line["kind"] == "matching_line":
                gaps.add(f"{line['file']}: saved notebook fragments do not establish complete cell boundaries; "
                         "source, outputs, and metadata may be mixed. Raw evidence is retained.")
        identity = (line["file"], line["commit"], representation)
        if (current_block is None or identity != current_block["identity"]
                or line["line_number"] != current_block["end_line"] + 1
                or line["character_offset"] != 0):
            current_block = {
                **{name: line[name] for name in ("repository", "file", "commit", "line_number", "kind")},
                "end_line": line["line_number"], "text": "", "source_ids": [],
                "source_line_map": [], "representation": representation, "identity": identity,
            }
            if line["character_offset"]:
                current_block["character_offset"] = line["character_offset"]
            blocks.append(current_block)
        elif representation != "decoded_notebook_fragment":
            current_block["text"] += "\n"
        text_start = len(current_block["text"])
        current_block["text"] += line_text
        current_block["end_line"] = line["line_number"]
        current_block["source_ids"].extend(line["source_ids"])
        current_block["source_line_map"].append({
            "line_number": line["line_number"], "character_offset": line["character_offset"],
            "text_start": text_start, "text_end": len(current_block["text"]),
            "source_ids": line["source_ids"],
        })
        if line["kind"] == "matching_line":
            current_block["kind"] = "matching_line"
    deduplicated = {}
    for block in sorted(blocks, key=lambda item: (item["file"], item["commit"] or "", item["line_number"], item.get("cell_index", -1))):
        block.pop("identity", None)
        if block["kind"] != "matching_line":
            group["packaging_notes"].append("Context outside matched blocks or decoded notebook cells is retained in raw audit evidence only.")
            continue
        identity = (block["text"], block["kind"], block.get("cell_type"), block["representation"])
        if identity not in deduplicated:
            deduplicated[identity] = {**block, "evidence_id": f"E{len(deduplicated) + 1}", "locations": []}
        retained = deduplicated[identity]
        retained["locations"].append(make_block_location(block))
        retained["source_ids"] = list(dict.fromkeys([*retained["source_ids"], *block["source_ids"]]))
    group["evidence_gaps"] = sorted(gaps)
    return list(deduplicated.values())


def compact_product_description(group: dict[str, Any]) -> dict[str, Any]:
    """Describe a product family once without repeating its full catalog records.

    Args:
        group: Group whose full catalog product remains unchanged in the audit.

    Returns:
        Product identity, one bounded description, and all known catalog versions.
        Selected or shortened descriptions are disclosed in packaging_notes.
    """
    product = group["product"]
    collections = product.get("collections", [])
    titles = list(dict.fromkeys(collection.get("title", "") for collection in collections if collection.get("title")))
    descriptions = list(dict.fromkeys(collection.get("summary", "") for collection in collections if collection.get("summary")))
    title = titles[0] if titles else ""
    description = descriptions[0] if descriptions else ""
    if len(titles) > 1 or len(descriptions) > 1:
        group["packaging_notes"].append("Model input uses one catalog title/description for the product family; all collections remain in the audit.")
    if len(title) > 300 or len(description) > 1000:
        group["packaging_notes"].append("Model product title/description is limited to 300/1000 characters; complete metadata remains in the audit.")
    return {
        "provider": product["provider"], "short_name": product["short_name"],
        "title": title[:300], "description": description[:1000],
        "description_truncated": len(title) > 300 or len(description) > 1000,
        "catalog_versions": sorted({collection["version"] for collection in collections if collection.get("version")}),
        "classification_scope": "Product family across versions; catalog versions are not a compatibility requirement.",
    }


def build_repository_context(group: dict[str, Any], maximum_characters: int = 1000) -> list[dict[str, Any]]:
    """Attach bounded README background without creating context-only requests.

    Args:
        group: Group with raw README line fragments preserved in evidence.
        maximum_characters: Combined text allowance across README revisions.

    Returns:
        Compact README excerpts; clipping is disclosed in packaging_notes.
    """
    readme_lines = reassemble_evidence_lines([item for item in group["evidence"] if item["kind"] == "readme_context"])
    contexts = []
    remaining = maximum_characters
    for line in readme_lines:
        if remaining <= 0:
            break
        if not contexts or (contexts[-1]["file"], contexts[-1]["commit"]) != (line["file"], line["commit"]):
            contexts.append({"file": line["file"], "commit": line["commit"], "line_number": line["line_number"], "text": ""})
        addition = ("\n" if contexts[-1]["text"] else "") + line["text"]
        contexts[-1]["text"] += addition[:remaining]
        remaining -= len(addition)
    total_characters = sum(len(line["text"]) + 1 for line in readme_lines)
    if total_characters > maximum_characters:
        group["packaging_notes"].append(f"README background is limited to {maximum_characters} characters per packet; full saved excerpts remain in the audit.")
    return contexts


def compact_evidence_block(block: dict[str, Any]) -> dict[str, Any]:
    """Remove audit-only hashes while keeping readable evidence and source context.

    Args:
        block: Full audit block, including every deduplicated source occurrence.

    Returns:
        Compact model-facing block with optional alternate source locations.
    """
    names = ("evidence_id", "file", "commit", "line_number", "end_line", "text", "kind",
             "cell_index", "cell_type", "representation", "text_offset", "character_offset")
    compact = {name: block[name] for name in names if name in block}
    if len(block["locations"]) > 1:
        compact["locations"] = [{name: location[name] for name in ("file", "commit", "line_number", "end_line")}
                                for location in block["locations"]]
    return compact


def measure_packet_bytes(packet: dict[str, Any]) -> int:
    """Measure the exact UTF-8 JSON input size used by the request builder.

    Args:
        packet: JSON-compatible model input.

    Returns:
        Serialized input size in bytes, not characters or estimated tokens.
    """
    return len(serialize_evidence(packet).encode("utf-8"))


def slice_evidence_block(
    block: dict[str, Any], offset: int, length: int, fragment_number: int, matching_source_ids: set[str],
) -> dict[str, Any]:
    """Map one text fragment back to its source lines and matching-line status.

    Args:
        block: Full readable block with original text offsets and source mappings.
        offset: First retained character in the original readable block.
        length: Number of retained characters.
        fragment_number: One-based suffix for this block's short citation ID.
        matching_source_ids: Raw evidence identifiers established as search matches.

    Returns:
        Fragment with narrowed locations and surrounding_code status when it
        contains only mapped context. Text offset mappings remain audit-visible.
    """
    fragment = {**block, "evidence_id": f"{block['evidence_id']}.{fragment_number}",
                "text_offset": offset, "text": block["text"][offset:offset + length]}
    locations = []
    for location in block["locations"]:
        line_map = [entry for entry in location.get("source_line_map", [])
                    if entry["text_start"] < offset + length and entry["text_end"] > offset]
        if line_map:
            locations.append({
                **location, "line_number": min(entry["line_number"] for entry in line_map),
                "end_line": max(entry.get("end_line", entry["line_number"]) for entry in line_map),
                "source_ids": list(dict.fromkeys(source_id for entry in line_map for source_id in entry["source_ids"])),
                "source_line_map": line_map,
            })
        else:
            # Keep a conservative range when no finer mapping exists. This also
            # covers a fragment containing only a separator between source lines.
            locations.append(location)
    fragment["locations"] = locations
    fragment["source_ids"] = list(dict.fromkeys(source_id for location in locations for source_id in location["source_ids"]))
    for name in ("line_number", "end_line", "source_line_map"):
        if name in locations[0]:
            fragment[name] = locations[0][name]
    fragment["kind"] = "matching_line" if matching_source_ids.intersection(fragment["source_ids"]) else "surrounding_code"
    return fragment


def split_evidence_block(
    block: dict[str, Any], metadata: dict[str, Any], maximum_bytes: int, matching_source_ids: set[str],
) -> list[dict[str, Any]]:
    """Split oversized readable text without losing characters or source mappings.

    Args:
        block: Full audit block to split only if its compact representation is large.
        metadata: Shared packet metadata, including bounded repository context.
        maximum_bytes: Exact serialized packet byte allowance.
        matching_source_ids: Raw search-match IDs used to identify context-only pieces.

    Returns:
        Original block when it fits, otherwise ordered lossless text fragments.

    Raises:
        ValueError: If metadata, source locations, and one character cannot fit.
    """
    if measure_packet_bytes({**metadata, "evidence": [compact_evidence_block(block)]}) <= maximum_bytes:
        return [block]
    pieces = []
    offset = 0
    while offset < len(block["text"]):
        lower, upper = 0, len(block["text"]) - offset
        while lower < upper:
            midpoint = (lower + upper + 1) // 2
            candidate = slice_evidence_block(block, offset, midpoint, len(pieces) + 1, matching_source_ids)
            if measure_packet_bytes({**metadata, "evidence": [compact_evidence_block(candidate)]}) <= maximum_bytes:
                lower = midpoint
            else:
                upper = midpoint - 1
        if lower == 0:
            raise ValueError("Product metadata or source locations exceed --packet-bytes; increase it and prepare a new directory.")
        pieces.append(slice_evidence_block(block, offset, lower, len(pieces) + 1, matching_source_ids))
        offset += lower
    if not pieces:
        raise ValueError("Product metadata plus evidence exceeds --packet-bytes; increase it and prepare a new directory.")
    return pieces


def package_evidence_blocks(group: dict[str, Any], max_packet_bytes: int) -> list[dict[str, Any]]:
    """Create version-two inputs and a complete citation audit without network calls.

    Args:
        group: Enriched group, including raw evidence from a new or archived plan.
        max_packet_bytes: Maximum serialized UTF-8 bytes in each input packet.

    Returns:
        Deterministic packets containing matched evidence and README background.
        Empty groups stay blocked; all raw evidence and catalog data remain intact.

    Raises:
        ValueError: If required metadata or source locations cannot fit the cap.
    """
    group["packaging_notes"] = []
    group["evidence_blocks"] = []
    if not any(item["kind"] == "matching_line" for item in group["evidence"]):
        return []
    blocks = build_readable_evidence_blocks(group)
    metadata = {
        "format_version": 2, "repository": group["repository"], "product": compact_product_description(group),
        "search_complete": group["search_complete"], "repository_context": build_repository_context(group),
        "scope": "Assess only supplied evidence; other packets or repository files may contain additional evidence.",
    }
    group["repository_context"] = metadata["repository_context"]
    packets = []
    current_packet = {**metadata, "evidence": []}
    matching_source_ids = {item["evidence_id"] for item in group["evidence"] if item["kind"] == "matching_line"}
    for block in blocks:
        for fragment in split_evidence_block(block, metadata, max_packet_bytes, matching_source_ids):
            group["evidence_blocks"].append(fragment)
            candidate = {**metadata, "evidence": [*current_packet["evidence"], compact_evidence_block(fragment)]}
            if measure_packet_bytes(candidate) > max_packet_bytes:
                packets.append(current_packet)
                current_packet = {**metadata, "evidence": [compact_evidence_block(fragment)]}
            else:
                current_packet = candidate
    if current_packet["evidence"]:
        packets.append(current_packet)
    group["packaging_notes"] = sorted(set(group["packaging_notes"]))
    return packets

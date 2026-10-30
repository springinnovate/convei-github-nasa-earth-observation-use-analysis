"""Select document headings and purpose prose without interpreting code blocks."""

import re


MAX_FRONT_MATTER_LINES = 200
MAX_INTRODUCTION_LINES = 8
MAX_INTRODUCTION_SPAN = 20
MAX_PURPOSE_CHARACTERS = 2400


def read_document_structure(source_lines: list[str]) -> tuple[list[dict], list[int], set[int], list[str]]:
    """Locate headings and a front-matter title in Markdown or R Markdown.

    This bounded text scan does not evaluate front matter, inline expressions,
    or fenced code. ATX headings use leading number signs; Setext headings use
    an underline of equals signs or hyphens. Indented code cannot form headings.

    Args:
        source_lines: Original document lines in their saved order.

    Returns:
        Headings with original start/end lines and levels, title lines, lines
        excluded from prose interpretation, and notes about parsing limitations.
    """
    headings = []
    title_lines = []
    excluded_lines = set()
    notes = []
    first_content = 0
    if source_lines and source_lines[0].lstrip("\ufeff").strip() == "---":
        closing = next((index for index in range(1, min(len(source_lines), MAX_FRONT_MATTER_LINES))
                        if source_lines[index].strip() in {"---", "..."}), None)
        if closing is None:
            notes.append("Document headings omitted: front matter has no closing delimiter within 200 lines.")
            return [], [], set(range(1, len(source_lines) + 1)), notes
        excluded_lines.update(range(1, closing + 2))
        first_content = closing + 1
        for index in range(1, closing):
            title = re.match(r"^title\s*:\s*(.*)", source_lines[index], re.IGNORECASE)
            if title:
                title_lines.append(index + 1)
                if title.group(1).strip() in {"|", ">", "|-", ">-", ""}:
                    for continuation in range(index + 1, min(closing, index + 5)):
                        if not source_lines[continuation].startswith((" ", "\t")):
                            break
                        title_lines.append(continuation + 1)
                break
    fence_character = None
    fence_length = 0
    for index in range(first_content, len(source_lines)):
        line = source_lines[index].rstrip("\r\n")
        number = index + 1
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence_character is not None:
            excluded_lines.add(number)
            if (fence and fence.group(1)[0] == fence_character
                    and len(fence.group(1)) >= fence_length and not fence.group(2).strip()):
                fence_character = None
            continue
        if fence and not (fence.group(1)[0] == "`" and "`" in fence.group(2)):
            fence_character, fence_length = fence.group(1)[0], len(fence.group(1))
            excluded_lines.add(number)
            continue
        if line.startswith(("    ", "\t")):
            excluded_lines.add(number)
            continue
        atx_heading = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+|$)(.*)$", line)
        if atx_heading:
            headings.append({"line_number": number, "end_line": number,
                             "level": len(atx_heading.group(1)), "title": atx_heading.group(2).strip(" #\t")})
            continue
        underline = re.fullmatch(r" {0,3}(=+|-+)[ \t]*", line)
        previous = source_lines[index - 1].strip() if index > first_content else ""
        if (underline and previous and number - 1 not in excluded_lines
                and not re.match(r"^(?:#{1,6}(?:\s|$)|>|[-+*]\s|\d+[.)]\s)", previous)
                and not (headings and headings[-1]["end_line"] == number - 1)):
            headings.append({"line_number": number - 1, "end_line": number,
                             "level": 1 if underline.group(1).startswith("=") else 2, "title": previous})
    if fence_character is not None:
        notes.append("Document has an unclosed code fence; later text is not interpreted as headings.")
    return headings, title_lines, excluded_lines, notes


def read_section_introduction(
    source_lines: list[str], start_line: int, end_line: int, excluded_lines: set[int],
) -> list[int]:
    """Select short leading prose from a section before its code or subsections.

    Args:
        source_lines: Original document text.
        start_line: First possible introductory line, one-based.
        end_line: Last line before the next heading or document end.
        excluded_lines: Front matter and code lines that cannot supply prose.

    Returns:
        Up to eight nonempty prose lines within the first twenty source lines.
        A code block ends the introduction instead of allowing text after it.
    """
    selected_lines = []
    for number in range(start_line, min(end_line + 1, start_line + MAX_INTRODUCTION_SPAN)):
        if number in excluded_lines:
            break
        if source_lines[number - 1].strip():
            selected_lines.append(number)
            if len(selected_lines) >= MAX_INTRODUCTION_LINES:
                break
    return selected_lines


def select_document_context_lines(source_lines: list[str], matches: list[int]) -> dict:
    """Prioritize applicable headings and purpose text before nearby code.

    A match gets its ancestor headings and their leading prose, never the
    introduction to a sibling section. A first section explicitly titled
    Introduction or Overview can supply document-wide purpose. Front-matter
    titles remain raw source text, and all coordinates refer to the saved file.

    Args:
        source_lines: Complete saved Markdown or R Markdown document.
        matches: Valid one-based matching lines in ascending order.

    Returns:
        Priority-ordered source lines and limitation notes. Purpose prose uses
        at most 2,400 characters; the caller enforces total line/text limits.
    """
    headings, title_lines, excluded_lines, notes = read_document_structure(source_lines)
    selected_headings = []
    nearby_lines = []
    for match in matches:
        ancestors = []
        for index, heading in enumerate(headings):
            if heading["line_number"] > match:
                break
            while ancestors and headings[ancestors[-1]]["level"] >= heading["level"]:
                ancestors.pop()
            ancestors.append(index)
        selected_headings.extend(ancestors)
        section_start = headings[ancestors[-1]]["line_number"] if ancestors else 1
        section_end = next((heading["line_number"] - 1 for heading in headings
                            if heading["line_number"] > match), len(source_lines))
        nearby_lines.extend(range(max(section_start, match - 10), min(section_end, match + 10) + 1))
    selected_headings = list(dict.fromkeys(selected_headings))
    introduction_heading = next((index for index, heading in enumerate(headings[:2])
                                 if heading["line_number"] <= 80
                                 and re.fullmatch(r"(?:introduction|overview)", heading["title"], re.IGNORECASE)
                                 and (index == 0 or (0 in selected_headings
                                                    and headings[0]["level"] < heading["level"]))), None)
    if introduction_heading is not None:
        selected_headings.append(introduction_heading)
    selected_headings = list(dict.fromkeys(selected_headings))
    priority_lines = list(title_lines)
    purpose_lines = []
    for index in selected_headings:
        heading = headings[index]
        priority_lines.extend(range(heading["line_number"], heading["end_line"] + 1))
        end_line = headings[index + 1]["line_number"] - 1 if index + 1 < len(headings) else len(source_lines)
        purpose_lines.extend(read_section_introduction(
            source_lines, heading["end_line"] + 1, end_line, excluded_lines))
    purpose_characters = 0
    omitted_purpose = False
    for number in dict.fromkeys(purpose_lines):
        if purpose_characters + len(source_lines[number - 1]) > MAX_PURPOSE_CHARACTERS:
            omitted_purpose = True
            continue
        priority_lines.append(number)
        purpose_characters += len(source_lines[number - 1])
    if omitted_purpose:
        notes.append("Document introduction text is limited to 2,400 characters; some purpose text is omitted.")
    notes.append("Document context includes applicable headings and up to eight introductory prose lines "
                 "within twenty source lines per section; neighboring sections are not expanded.")
    return {"line_numbers": list(dict.fromkeys([*priority_lines, *matches, *nearby_lines])), "notes": notes}

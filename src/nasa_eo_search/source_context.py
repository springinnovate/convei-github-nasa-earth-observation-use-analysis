"""Select bounded, same-file structural evidence without executing source code."""

import ast
import json
from pathlib import PurePosixPath
import re

from nasa_eo_search.document_context import select_document_context_lines


MAX_SOURCE_BYTES = 2_000_000
MAX_SOURCE_LINES = 50_000
MAX_SELECTED_LINES = 400
MAX_STRUCTURAL_MATCHES = 64
MAX_CONSUMERS = 3
NEARBY_LINE_COUNT = 10
MAX_COMPLETE_SCRIPT_LINES = 160
MAX_COMPLETE_SCRIPT_CHARACTERS = 6000
MAX_FILE_HEADER_CHARACTERS = 600
SCRIPT_EXTENSIONS = {".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".sh", ".r"}
PYTHON_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def select_enclosing_source_lines(
    file_path: str,
    source_lines: list[str],
    matching_line_numbers: list[int],
) -> dict:
    """Find bounded structural context for matches in a saved source file.

    Call this after locating literal matches. Python is parsed into an abstract
    syntax tree, never imported or executed. YAML and JSON use conservative
    text structure. Returned line numbers retain the original file coordinates
    and are ordered by evidence priority, not file order, so callers can apply
    their own character budget without losing distant definition headers.
    Small scripts can include their complete source within the added-context
    character budget. Python containing definitions retains selective structural
    context. Markdown preserves enclosing headings and bounded purpose prose.
    Unsupported, malformed, or oversized source receives an explicit
    fallback, with a bounded file-purpose header when available.

    Args:
        file_path: Repository-relative path used to identify the source format.
        source_lines: Original file lines, with or without newline characters.
        matching_line_numbers: One-based line numbers containing search matches.

    Returns:
        A dictionary containing ``line_numbers`` (at most 400 unique, valid
        original line numbers) and ``notes`` explaining fallback or truncation.
    """
    matches = sorted({
        line_number for line_number in matching_line_numbers
        if isinstance(line_number, int) and not isinstance(line_number, bool)
        and 1 <= line_number <= len(source_lines)
    })
    result = {"line_numbers": [], "notes": []}
    if not matches:
        return result
    if len(source_lines) > MAX_SOURCE_LINES or sum(map(len, source_lines)) > MAX_SOURCE_BYTES:
        return _select_nearby_fallback(source_lines, matches, "Source exceeds the structural parsing limit.")
    source_text = "\n".join(line.rstrip("\r\n") for line in source_lines)
    if len(source_text.encode("utf-8", errors="replace")) > MAX_SOURCE_BYTES:
        return _select_nearby_fallback(source_lines, matches, "Source exceeds the structural parsing byte limit.")
    extension = PurePosixPath(file_path.replace("\\", "/")).suffix.lower()
    if extension in {".md", ".markdown", ".rmd"}:
        document_selection = select_document_context_lines(source_lines, matches[:MAX_STRUCTURAL_MATCHES])
        result["notes"].extend(document_selection["notes"])
        if len(matches) > MAX_STRUCTURAL_MATCHES:
            result["notes"].append(f"Document context examines the first {MAX_STRUCTURAL_MATCHES} matching lines.")
        _append_source_lines(result, document_selection["line_numbers"], len(source_lines))
        return result
    if extension not in {".py", ".pyi", ".yaml", ".yml", ".json"}:
        fallback = _select_nearby_fallback(source_lines, matches, f"Structural context is unsupported for {extension or 'this file type'}.")
        _add_script_file_context(source_lines, extension, fallback)
        return fallback
    if len(matches) > MAX_STRUCTURAL_MATCHES:
        result["notes"].append(f"Structural selection examines the first {MAX_STRUCTURAL_MATCHES} matching lines in this file.")
        matches = matches[:MAX_STRUCTURAL_MATCHES]
    try:
        if extension in {".py", ".pyi"}:
            _select_python_context(source_text, source_lines, matches, result)
        elif extension in {".yaml", ".yml", ".json"}:
            if extension == ".json":
                json.loads(source_text)
            _select_configuration_context(source_lines, matches, result, extension != ".json")
    except (SyntaxError, ValueError, RecursionError) as error:
        fallback = _select_nearby_fallback(source_lines, matches, f"Structural parsing failed ({type(error).__name__}).")
        fallback["notes"].extend(result["notes"])
        _add_script_file_context(source_lines, extension, fallback)
        return fallback
    _add_script_file_context(source_lines, extension, result)
    return result


def _add_script_file_context(source_lines: list[str], extension: str, result: dict) -> None:
    """Add bounded file-purpose comments and complete small script context.

    Complete text helps connect top-level defaults to later parsing, retrieval,
    and output operations, including scripts with syntax errors. This selects
    evidence for inspection without claiming to resolve a program's data flow.
    Python definitions keep the existing selective context so unrelated methods
    and shadowed names are not pulled into an otherwise focused assessment.

    Args:
        source_lines: Original source lines, never executed.
        extension: Lowercase file suffix identifying a script format.
        result: Mutable line selection and limitations from structural selection
            or nearby fallback. Existing coordinates retain their priority.
    """
    if extension not in SCRIPT_EXTENSIONS:
        return
    contains_python_definitions = extension in {".py", ".pyi"} and any(
        re.match(r"^[ \t]*(?:async[ \t]+def|def|class)\b", source_line)
        for source_line in source_lines
    )
    if contains_python_definitions and any("Structural parsing failed" in note for note in result["notes"]):
        return
    header_numbers = []
    header_characters = 0
    for number, source_line in enumerate(source_lines[:8], 1):
        stripped_line = source_line.strip()
        if stripped_line and not stripped_line.startswith(("#", "//", "/*", "*", "*/")):
            break
        if header_characters + len(source_line) > MAX_FILE_HEADER_CHARACTERS:
            break
        if stripped_line:
            header_numbers.append(number)
            header_characters += len(source_line)
    if header_numbers:
        # Keep established definition/consumer priority, then show purpose before
        # long bodies can consume the caller's remaining character budget.
        original_numbers = result["line_numbers"]
        result["line_numbers"] = []
        _append_source_lines(result, original_numbers[:8], len(source_lines))
        _append_source_lines(result, header_numbers, len(source_lines))
        _append_source_lines(result, original_numbers[8:], len(source_lines))
    complete_script = (
        not contains_python_definitions
        and len(source_lines) <= MAX_COMPLETE_SCRIPT_LINES
        and sum(map(len, source_lines)) <= MAX_COMPLETE_SCRIPT_CHARACTERS
    )
    if complete_script:
        _append_source_lines(result, range(1, len(source_lines) + 1), len(source_lines))
        result["notes"].append(
            "Complete small-script context may include unrelated operations; "
            "inclusion alone does not establish a connection to the matched product.")
        result["notes"] = [
            note.replace("Using nearby lines only.",
                         "Including complete small-script text; syntax and data flow are unverified.")
            for note in result["notes"]
        ]
    elif header_numbers:
        result["notes"] = [
            note.replace("Using nearby lines only.", "Using nearby lines and a bounded file header.")
            for note in result["notes"]
        ]


def _append_source_lines(result: dict, line_numbers: range | list[int], source_length: int) -> None:
    """Append valid, unique lines in priority order within the shared line limit.

    Args:
        result: Mutable selector result containing selected lines and notes.
        line_numbers: Original one-based source coordinates in priority order.
        source_length: Number of original source lines.
    """
    selected = set(result["line_numbers"])
    for line_number in line_numbers:
        if line_number in selected or not 1 <= line_number <= source_length:
            continue
        if len(selected) >= MAX_SELECTED_LINES:
            note = f"Structural context is limited to {MAX_SELECTED_LINES} selected lines."
            if note not in result["notes"]:
                result["notes"].append(note)
            break
        result["line_numbers"].append(line_number)
        selected.add(line_number)


def _select_nearby_fallback(source_lines: list[str], matches: list[int], reason: str) -> dict:
    """Return nearby original lines when structural selection is unavailable.

    Args:
        source_lines: Original file lines.
        matches: Valid one-based matching line numbers.
        reason: Explanation for falling back to nearby context.

    Returns:
        A bounded selection with the fallback reason recorded in its notes.
    """
    result = {"line_numbers": [], "notes": [f"{reason} Using nearby lines only."]}
    _append_source_lines(result, matches, len(source_lines))
    for line_number in matches:
        _append_source_lines(result, range(line_number - NEARBY_LINE_COUNT, line_number + NEARBY_LINE_COUNT + 1), len(source_lines))
    return result


def _append_definition_header(result: dict, definition: ast.AST, source_length: int) -> None:
    """Prioritize a Python definition and its decorators or multiline header.

    Args:
        result: Mutable selector result.
        definition: Function or class syntax-tree node.
        source_length: Number of original source lines.
    """
    _append_source_lines(result, [definition.lineno], source_length)
    for decorator in definition.decorator_list:
        _append_source_lines(result, range(decorator.lineno, min(decorator.end_lineno, decorator.lineno + 12) + 1), source_length)
    header_end = max(definition.lineno, definition.body[0].lineno - 1)
    _append_source_lines(result, range(definition.lineno, min(header_end, definition.lineno + 16) + 1), source_length)


def _python_node_start_line(node: ast.AST) -> int:
    """Find the first source line of a node, including definition decorators.

    Args:
        node: Syntax-tree node with original source positions.

    Returns:
        One-based start line including any function or class decorators.
    """
    if isinstance(node, PYTHON_DEFINITIONS):
        return min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
    return node.lineno


def _append_bounded_range(result: dict, start: int, end: int, source_length: int, maximum_lines: int = 80) -> None:
    """Append a structural body while recording any omitted middle portion.

    Args:
        result: Mutable selector result.
        start: First original one-based source line.
        end: Last original one-based source line.
        source_length: Number of original source lines.
        maximum_lines: Maximum lines selected from this body.
    """
    if end - start + 1 <= maximum_lines:
        _append_source_lines(result, range(start, end + 1), source_length)
        return
    _append_source_lines(result, range(start, start + maximum_lines - 3), source_length)
    _append_source_lines(result, range(end - 2, end + 1), source_length)
    note = f"Structural block at lines {start}-{end} is excerpted; its middle is omitted."
    if note not in result["notes"]:
        result["notes"].append(note)


def _select_python_context(source_text: str, source_lines: list[str], matches: list[int], result: dict) -> None:
    """Collect Python definitions, matching assignments, and bounded consumers.

    Args:
        source_text: Bounded source text to parse without executing it.
        source_lines: Original file lines.
        matches: Valid one-based matching line numbers.
        result: Mutable selector result.

    Raises:
        SyntaxError: If Python cannot parse the source.
        ValueError: If the source contains invalid parser input.
        RecursionError: If the parser's nesting limit is exceeded.
    """
    module = ast.parse(source_text)
    parents = {child: parent for parent in ast.walk(module) for child in ast.iter_child_nodes(parent)}
    positioned_nodes = [node for node in parents if hasattr(node, "lineno") and getattr(node, "end_lineno", None)]
    bodies = []
    consumers = []
    for match_line in matches:
        enclosing_nodes = [node for node in positioned_nodes if _python_node_start_line(node) <= match_line <= node.end_lineno]
        definitions = sorted(
            (node for node in enclosing_nodes if isinstance(node, PYTHON_DEFINITIONS)),
            key=lambda node: node.end_lineno - node.lineno,
        )
        for definition in definitions[:8]:
            _append_definition_header(result, definition, len(source_lines))
        callable_definition = next((node for node in definitions if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
        if callable_definition is not None:
            bodies.append(callable_definition)
        assignments = [node for node in enclosing_nodes if isinstance(node, (ast.Assign, ast.AnnAssign))]
        if assignments:
            assignment = min(assignments, key=lambda node: node.end_lineno - node.lineno)
            _append_source_lines(result, [assignment.lineno, assignment.end_lineno], len(source_lines))
            bodies.append(assignment)
            scope = parents[assignment]
            if isinstance(scope, (ast.Module, ast.ClassDef)):
                assignment_consumers = _find_assignment_consumers(assignment, scope)
                if len(assignment_consumers) > MAX_CONSUMERS:
                    result["notes"].append(f"Assignment at line {assignment.lineno} has additional consumers; only {MAX_CONSUMERS} are included.")
                for consumer, reference_lines in assignment_consumers[:MAX_CONSUMERS]:
                    if consumer not in consumers:
                        consumers.append(consumer)
                    if isinstance(consumer, PYTHON_DEFINITIONS):
                        _append_definition_header(result, consumer, len(source_lines))
                    else:
                        _append_source_lines(result, [consumer.lineno], len(source_lines))
                    for reference_line in reference_lines[:8]:
                        _append_source_lines(result, range(max(consumer.lineno, reference_line - 2), min(consumer.end_lineno, reference_line + 2) + 1), len(source_lines))
    _append_source_lines(result, matches, len(source_lines))
    for consumer in consumers:
        _append_bounded_range(result, consumer.lineno, consumer.end_lineno, len(source_lines))
    for match_line in matches:
        _append_source_lines(result, range(match_line - NEARBY_LINE_COUNT, match_line + NEARBY_LINE_COUNT + 1), len(source_lines))
    for body in dict.fromkeys(bodies):
        _append_bounded_range(result, body.lineno, body.end_lineno, len(source_lines))


def _find_assignment_consumers(assignment: ast.AST, scope: ast.AST) -> list[tuple[ast.AST, list[int]]]:
    """Find direct same-scope statements or methods reading assigned names.

    Args:
        assignment: Matched module-level or class-level assignment node.
        scope: Its immediate module or class parent.

    Returns:
        Source-ordered pairs of direct child nodes and their reference lines.
        Class attributes must be qualified by the class or a non-static
        method's first argument. Locally shadowed global names and assignment
        declarations themselves are excluded.
    """
    targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
    names = {node.id for target in targets for node in ast.walk(target) if isinstance(node, ast.Name)}
    consumers = []
    for statement in scope.body:
        if statement is assignment:
            continue
        if isinstance(scope, ast.Module):
            references = [node for node in ast.walk(statement) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names]
            # A locally assigned name or argument is not evidence of this global.
            local_names = {node.id for node in ast.walk(statement) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
            local_names.update(node.arg for node in ast.walk(statement) if isinstance(node, ast.arg))
            for imported_node in ast.walk(statement):
                if isinstance(imported_node, (ast.Import, ast.ImportFrom)):
                    for imported_name in imported_node.names:
                        bound_name = imported_name.asname or imported_name.name.split(".")[0]
                        local_names.update(names if bound_name == "*" else {bound_name})
            references = [node for node in references if node.id not in local_names]
        else:
            qualifiers = {scope.name}
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                positional_arguments = statement.args.posonlyargs + statement.args.args
                is_static_method = any(
                    isinstance(decorator, ast.Name) and decorator.id == "staticmethod"
                    or isinstance(decorator, ast.Attribute) and decorator.attr == "staticmethod"
                    for decorator in statement.decorator_list
                )
                if positional_arguments and not is_static_method:
                    qualifiers.add(positional_arguments[0].arg)
            references = [
                node for node in ast.walk(statement)
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                and node.attr in names and isinstance(node.value, ast.Name)
                and node.value.id in qualifiers
            ]
        if references:
            consumers.append((statement, sorted({node.lineno for node in references})))
    return consumers


def _mask_quoted_configuration_text(source_line: str) -> str:
    """Hide quoted strings and comments while keeping structural coordinates.

    Args:
        source_line: One original YAML or JSON source line.

    Returns:
        A same-length line with quoted content and comments replaced by spaces.
        This conservative lexer handles single-line quoted scalars only.
    """
    characters = list(source_line)
    quote = None
    escaped = False
    for index, character in enumerate(source_line):
        if quote:
            characters[index] = " "
            if escaped:
                escaped = False
            elif character == "\\" and quote == '"':
                escaped = True
            elif character == quote:
                quote = None
        elif character in {'"', "'"}:
            quote = character
            characters[index] = " "
        elif character == "#":
            characters[index:] = " " * (len(characters) - index)
            break
    return "".join(characters)


def _find_configuration_brackets(masked_lines: list[str]) -> list[tuple[int, int]]:
    """Locate balanced flow-container ranges without interpreting their values.

    Args:
        masked_lines: Lines with quoted strings and comments hidden.

    Returns:
        Pairs of original one-based start and end lines for each container.

    Raises:
        ValueError: If brackets are unbalanced or mismatched.
    """
    stack = []
    ranges = []
    for line_number, source_line in enumerate(masked_lines, 1):
        for character in source_line:
            if character in "[{":
                stack.append((character, line_number))
            elif character in "]}":
                if not stack or stack[-1][0] != {"]": "[", "}": "{"}[character]:
                    raise ValueError("Unbalanced configuration brackets")
                _, start_line = stack.pop()
                ranges.append((start_line, line_number))
    if stack:
        raise ValueError("Unclosed configuration brackets")
    return ranges


def _configuration_indent(source_line: str) -> int:
    """Return leading whitespace width for conservative indentation comparisons.

    Args:
        source_line: Original or masked configuration line.

    Returns:
        Number of leading whitespace characters, treating each tab as one.
    """
    return len(source_line) - len(source_line.lstrip())


def _find_indented_block_end(masked_lines: list[str], start_line: int) -> int:
    """Find the last line before a following YAML sibling or enclosing item.

    Args:
        masked_lines: Source with quoted text and comments hidden.
        start_line: Original one-based block header line.

    Returns:
        Inclusive original end line for the indentation-defined block.
    """
    starting_indent = _configuration_indent(masked_lines[start_line - 1])
    for line_number in range(start_line + 1, len(masked_lines) + 1):
        candidate = masked_lines[line_number - 1]
        if candidate.strip() and _configuration_indent(candidate) <= starting_indent:
            return line_number - 1
    return len(masked_lines)


def _find_yaml_consumer_block(masked_lines: list[str], alias_line: int) -> tuple[int, int]:
    """Find the nearest containing YAML list item or mapping for an alias read.

    Args:
        masked_lines: Source with quoted text and comments hidden.
        alias_line: Original one-based line using an anchor alias.

    Returns:
        Original inclusive bounds of the smallest useful containing block.
    """
    alias_indent = _configuration_indent(masked_lines[alias_line - 1])
    nearest_mapping = alias_line
    for line_number in range(alias_line, 0, -1):
        candidate = masked_lines[line_number - 1]
        if not candidate.strip() or _configuration_indent(candidate) > alias_indent:
            continue
        if _find_indented_block_end(masked_lines, line_number) < alias_line:
            continue
        if re.match(r"^\s*-\s+.+:", candidate):
            return line_number, _find_indented_block_end(masked_lines, line_number)
        if ":" in candidate and nearest_mapping == alias_line:
            nearest_mapping = line_number
        if candidate and not candidate[0].isspace():
            break
    return nearest_mapping, _find_indented_block_end(masked_lines, nearest_mapping)


def _select_configuration_context(source_lines: list[str], matches: list[int], result: dict, is_yaml: bool) -> None:
    """Select bounded configuration blocks and linked YAML anchor consumers.

    Args:
        source_lines: Original YAML or JSON file lines.
        matches: Valid one-based matching line numbers.
        result: Mutable selector result.
        is_yaml: Whether YAML indentation and anchors should be considered.

    Raises:
        ValueError: If flow-container brackets cannot be balanced safely.
    """
    masked_lines = [_mask_quoted_configuration_text(line) for line in source_lines]
    bracket_ranges = _find_configuration_brackets(masked_lines)
    structural_ranges = []
    consumer_ranges = []
    for match_line in matches:
        enclosing_ranges = [bounds for bounds in bracket_ranges if bounds[0] <= match_line <= bounds[1]]
        if enclosing_ranges:
            start_line, end_line = min(enclosing_ranges, key=lambda bounds: bounds[1] - bounds[0])
            structural_ranges.append((start_line, end_line))
            _append_source_lines(result, [start_line, end_line], len(source_lines))
        if not is_yaml:
            continue
        # Keep enclosing mapping labels even when their matching list is long.
        match_indent = _configuration_indent(source_lines[match_line - 1])
        header_count = 0
        for header_line in range(match_line, 0, -1):
            header = masked_lines[header_line - 1]
            if not header.strip() or ":" not in header or _configuration_indent(header) >= match_indent:
                continue
            if _find_indented_block_end(masked_lines, header_line) < match_line:
                continue
            _append_source_lines(result, [header_line], len(source_lines))
            match_indent = _configuration_indent(header)
            header_count += 1
            if header_count >= 8:
                break
        for anchor_line, anchor_source in enumerate(masked_lines, 1):
            anchor_match = re.search(r"(?:^|[\s:\[{,])&([A-Za-z0-9_-]+)(?=$|[\s,}\]])", anchor_source)
            if not anchor_match or anchor_line > match_line:
                continue
            anchor_ranges = [bounds for bounds in bracket_ranges if bounds[0] == anchor_line]
            anchor_end = max((bounds[1] for bounds in anchor_ranges), default=_find_indented_block_end(masked_lines, anchor_line))
            if match_line > anchor_end:
                continue
            _append_source_lines(result, [anchor_line, anchor_end], len(source_lines))
            alias_pattern = re.compile(r"(?:^|[\s:\[{,])\*" + re.escape(anchor_match.group(1)) + r"(?=$|[\s,}\]])")
            redeclaration_pattern = re.compile(r"(?:^|[\s:\[{,])&" + re.escape(anchor_match.group(1)) + r"(?=$|[\s,}\]])")
            next_declaration = next(
                (line_number for line_number, line in enumerate(masked_lines, 1)
                 if line_number > anchor_line and redeclaration_pattern.search(line)),
                len(masked_lines) + 1,
            )
            alias_lines = [
                line_number for line_number, line in enumerate(masked_lines, 1)
                if anchor_line <= line_number < next_declaration and alias_pattern.search(line)
            ]
            if len(alias_lines) > MAX_CONSUMERS:
                result["notes"].append(f"Anchor at line {anchor_line} has additional aliases; only {MAX_CONSUMERS} consumer blocks are included.")
            for alias_line in alias_lines[:MAX_CONSUMERS]:
                consumer_range = _find_yaml_consumer_block(masked_lines, alias_line)
                consumer_ranges.append(consumer_range)
                _append_source_lines(result, [consumer_range[0], alias_line], len(source_lines))
    _append_source_lines(result, matches, len(source_lines))
    for start_line, end_line in dict.fromkeys(consumer_ranges):
        _append_bounded_range(result, start_line, end_line, len(source_lines))
    for match_line in matches:
        _append_source_lines(result, range(match_line - NEARBY_LINE_COUNT, match_line + NEARBY_LINE_COUNT + 1), len(source_lines))
    for start_line, end_line in dict.fromkeys(structural_ranges):
        _append_bounded_range(result, start_line, end_line, len(source_lines))

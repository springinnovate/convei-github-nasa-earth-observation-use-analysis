"""Find dataset identifiers in the text files of a downloaded repository ZIP.

Files are read directly from the ZIP, never extracted or executed. Callers set
the maximum file size, total decompressed bytes, and number of files to inspect;
every excluded file is listed in the returned scan result.
"""

from collections import deque
from functools import lru_cache
import ntpath
from pathlib import Path
import stat
import zipfile
import zlib


ASCII_LOWERCASE = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
ASCII_ALPHANUMERIC = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
MATCHING_RULE = "literal; ASCII case-insensitive; ASCII non-alphanumeric boundaries; underscore separates terms"


@lru_cache(maxsize=1)
def build_literal_term_matcher(terms: tuple[str, ...]) -> tuple:
    """Build a matcher that can find all catalog terms in one pass over a line.

    Call this once for a set of dataset identifiers, then pass the result to
    ``find_literal_terms_in_line`` for each source line. The matcher uses the
    Aho-Corasick algorithm to share work between overlapping terms instead of
    searching the whole line separately for every term. Only the latest set of
    terms is cached. Only ASCII letters are lowercased, so source offsets stay
    unchanged even when the line contains Unicode characters.

    Args:
        terms: Nonempty literal identifiers in their original spelling, checked
            by ``scan_repository_zip`` before this function is called.

    Returns:
        A tuple of character transitions, fallback states, completed terms,
        and links to matching suffixes. This is the ``matcher`` argument for
        ``find_literal_terms_in_line``; callers do not need to unpack it.
    """
    transitions: list[dict[str, int]] = [{}]
    failure_links = [0]
    terminal_terms: list[list[str]] = [[]]
    terminal_suffix_links = [0]
    for term in terms:
        state = 0
        for character in term.translate(ASCII_LOWERCASE):
            next_state = transitions[state].get(character)
            if next_state is None:
                next_state = len(transitions)
                transitions[state][character] = next_state
                transitions.append({})
                failure_links.append(0)
                terminal_terms.append([])
                terminal_suffix_links.append(0)
            state = next_state
        terminal_terms[state].append(term)

    pending_states = deque(transitions[0].values())
    while pending_states:
        parent_state = pending_states.popleft()
        for character, child_state in transitions[parent_state].items():
            pending_states.append(child_state)
            fallback_state = failure_links[parent_state]
            while fallback_state and character not in transitions[fallback_state]:
                fallback_state = failure_links[fallback_state]
            fallback_state = transitions[fallback_state].get(character, 0)
            failure_links[child_state] = fallback_state
            terminal_suffix_links[child_state] = (
                fallback_state if terminal_terms[fallback_state]
                else terminal_suffix_links[fallback_state]
            )
    return transitions, failure_links, terminal_terms, terminal_suffix_links


def find_literal_terms_in_line(line: str, matcher: tuple) -> set[str]:
    """Find catalog terms in one line, ignoring ASCII letter case.

    Each match must touch a line edge or a character other than an ASCII letter
    or digit on both sides. An underscore therefore separates identifiers:
    ``ATL03`` matches ``ATL03_007`` but not ``MATL03`` or ``ATL030``.

    Args:
        line: Original decoded file line, without its newline terminator.
        matcher: Search tables returned by ``build_literal_term_matcher``.

    Returns:
        The set of matching terms in their original catalog spelling. A term
        appears once in the set even if it occurs several times in the line.
    """
    transitions, failure_links, terminal_terms, terminal_suffix_links = matcher
    normalized_line = line.translate(ASCII_LOWERCASE)
    state = 0
    matching_terms: set[str] = set()
    for character_position, character in enumerate(normalized_line):
        while state and character not in transitions[state]:
            state = failure_links[state]
        state = transitions[state].get(character, 0)
        terminal_state = state
        while terminal_state:
            for term in terminal_terms[terminal_state]:
                start_position = character_position + 1 - len(term)
                end_position = character_position + 1
                if (
                    (start_position == 0 or line[start_position - 1] not in ASCII_ALPHANUMERIC)
                    and (end_position == len(line) or line[end_position] not in ASCII_ALPHANUMERIC)
                ):
                    matching_terms.add(term)
            terminal_state = terminal_suffix_links[terminal_state]
    return matching_terms


def inspect_zip_member_path(member: zipfile.ZipInfo) -> tuple[list[str], str]:
    """Check a ZIP entry's path and file type before reading its contents.

    Reject absolute paths, parent-directory components, Windows drive names,
    control characters, symbolic links, and special files. The scanner and
    classifier use this same check when opening a saved repository ZIP.

    Args:
        member: ZIP entry metadata, including its original stored filename.

    Returns:
        A list of slash-separated path components and an empty reason when
        accepted. Otherwise, an empty list and a reason such as
        ``unsafe_archive_path`` or ``symbolic_link_not_followed``.
    """
    member_name = member.orig_filename
    if (
        not member_name or any(ord(character) < 32 for character in member_name)
        or ":" in member_name or "\\" in member_name
        or member_name.startswith("/") or ntpath.splitdrive(member_name)[0]
    ):
        return [], "unsafe_archive_path"
    components = member_name.rstrip("/").split("/")
    if any(component in ("", ".", "..") for component in components):
        return [], "unsafe_archive_path"
    # Checking every component also rejects drive-prefixed names under a root.
    if any(ntpath.splitdrive(component)[0] for component in components):
        return [], "unsafe_archive_path"
    file_mode = member.external_attr >> 16
    if stat.S_ISLNK(file_mode):
        return [], "symbolic_link_not_followed"
    if stat.S_IFMT(file_mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
        return [], "special_file_not_read"
    return components, ""


def scan_repository_zip(
    zip_path: Path | str,
    terms: list[str],
    max_file_bytes: int = 2_000_000,
    max_uncompressed_bytes: int = 200_000_000,
    max_files: int = 20_000,
) -> dict:
    """Scan a repository ZIP for every supplied dataset identifier.

    Call this after downloading a ZIP for a recorded Git commit. It reads text
    files directly from the ZIP, searches all terms together, and returns each
    matching line with its original text and one-based line number. It does not
    extract files or execute code. If all entries share a top-level directory,
    that directory is removed from reported file paths.

    The result lists files excluded because of unsafe names, unreadable content,
    unsupported encoding, or the supplied resource limits. Git Large File
    Storage (LFS) pointer files and submodule contents are also reported as
    missing source content. Any exclusion makes the scan ``incomplete``.

    Args:
        zip_path: Path to the downloaded repository ZIP.
        terms: Nonempty literal strings; newlines and NUL characters are invalid.
        max_file_bytes: Maximum uncompressed bytes read from any individual file.
        max_uncompressed_bytes: Aggregate maximum uncompressed bytes read.
        max_files: Maximum number of nondirectory entries considered for reading.

    Returns:
        A dictionary with ``matches`` grouped by file and term, ``skipped``
        entries with reasons, file/byte counts, and a ``complete`` or
        ``incomplete`` status. Complete means this ZIP was scanned without
        exclusions; it says nothing about other repositories, branches, history,
        or data that the code would download when run. A malformed ZIP returns
        ``incomplete`` with an ``invalid_archive`` reason.

    Raises:
        ValueError: Terms or resource limits are invalid.
        OSError: The ZIP file cannot be opened or its metadata cannot be read.
    """
    for limit_name, limit_value in (
        ("max_file_bytes", max_file_bytes),
        ("max_uncompressed_bytes", max_uncompressed_bytes),
        ("max_files", max_files),
    ):
        if type(limit_value) is not int or limit_value < 1:
            raise ValueError(f"{limit_name} must be a positive integer.")
    if not isinstance(terms, (list, tuple)) or not terms:
        raise ValueError("At least one literal search term is required.")
    if any(not isinstance(term, str) or not term or any(character in term for character in "\r\n\x00") for term in terms):
        raise ValueError("Search terms must be nonempty strings without newlines or NUL characters.")
    unique_terms = tuple(dict.fromkeys(terms))
    matcher = build_literal_term_matcher(unique_terms)
    repository_zip_path = Path(zip_path)
    result = {
        "status": "complete", "matches": [], "skipped": [],
        "files_scanned": 0, "files_considered": 0, "bytes_scanned": 0,
        "bytes_read": 0, "uncompressed_bytes_budget_used": 0,
        "archive_bytes": repository_zip_path.stat().st_size,
        "declared_uncompressed_bytes": 0, "term_count": len(unique_terms),
        "matching_rule": MATCHING_RULE, "archive_root": "",
        "limits": {"max_file_bytes": max_file_bytes,
                   "max_uncompressed_bytes": max_uncompressed_bytes, "max_files": max_files},
    }
    try:
        repository_zip = zipfile.ZipFile(repository_zip_path)
    except zipfile.BadZipFile as error:
        result["status"] = "incomplete"
        result["skipped"].append({"file": "", "reason": "invalid_archive", "detail": str(error)})
        return result

    with repository_zip:
        zip_members = repository_zip.infolist()
        inspected_members = [
            (member, *inspect_zip_member_path(member)) for member in zip_members
        ]
        safe_paths = [components for _, components, reason in inspected_members if not reason]
        common_roots = {components[0] for components in safe_paths}
        if len(common_roots) == 1 and all(
            len(components) > 1 or member.is_dir()
            for member, components, reason in inspected_members if not reason
        ):
            result["archive_root"] = next(iter(common_roots))
        seen_files: set[str] = set()
        ambiguous_files: set[str] = set()
        for member, components, unsafe_reason in inspected_members:
            result["declared_uncompressed_bytes"] += member.file_size
            if unsafe_reason:
                result["skipped"].append({"file": member.orig_filename, "reason": unsafe_reason})
                continue
            if member.is_dir():
                continue
            file_path = "/".join(components[1:] if result["archive_root"] else components)
            result["files_considered"] += 1
            skip_reason = ""
            if file_path in seen_files:
                skip_reason = "duplicate_archive_path"
                if file_path not in ambiguous_files:
                    # No single source file can be identified at this path.
                    # Preserve the ZIP and explicit gap, not a first-entry guess.
                    result["matches"] = [match for match in result["matches"] if match["file"] != file_path]
                    ambiguous_files.add(file_path)
            elif result["files_considered"] > max_files:
                skip_reason = "file_count_limit"
            elif member.flag_bits & 1:
                skip_reason = "encrypted_file"
            elif member.file_size > max_file_bytes:
                skip_reason = "file_size_limit"
            elif member.file_size > max_uncompressed_bytes - result["uncompressed_bytes_budget_used"]:
                skip_reason = "archive_uncompressed_size_limit"
            seen_files.add(file_path)
            if skip_reason:
                result["skipped"].append({"file": file_path, "reason": skip_reason})
                continue
            # Reserve even a corrupt member's declared bytes: repeated failed
            # reads must not bypass the aggregate decompression budget.
            result["uncompressed_bytes_budget_used"] += member.file_size
            try:
                with repository_zip.open(member) as input_file:
                    file_bytes = input_file.read(member.file_size)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError, zlib.error) as error:
                result["skipped"].append({"file": file_path, "reason": "archive_member_read_error", "detail": str(error)})
                continue
            result["bytes_read"] += len(file_bytes)
            if len(file_bytes) > max_file_bytes or result["bytes_read"] > max_uncompressed_bytes:
                result["skipped"].append({"file": file_path, "reason": "actual_uncompressed_size_limit"})
                continue
            if any(character in file_bytes for character in (b"\x00", b"\x01", b"\x02", b"\x03", b"\x04", b"\x05", b"\x06", b"\x07", b"\x08")):
                result["skipped"].append({"file": file_path, "reason": "binary_file"})
                continue
            try:
                file_text = file_bytes.decode("utf-8-sig")
            except UnicodeDecodeError:
                result["skipped"].append({"file": file_path, "reason": "non_utf8_file"})
                continue
            if file_text.startswith("version https://git-lfs.github.com/spec/v1\n") or file_text.startswith("version https://git-lfs.github.com/spec/v1\r\n"):
                result["skipped"].append({"file": file_path, "reason": "git_lfs_content_not_in_snapshot"})
                continue
            if components[-1] == ".gitmodules":
                result["skipped"].append({"file": file_path, "reason": "submodule_contents_not_in_snapshot"})
            result["files_scanned"] += 1
            result["bytes_scanned"] += len(file_bytes)
            matching_lines_by_term: dict[str, list[dict]] = {}
            for line_number, line in enumerate(file_text.split("\n"), 1):
                original_line = line.removesuffix("\r")
                for term in sorted(find_literal_terms_in_line(original_line, matcher)):
                    matching_lines_by_term.setdefault(term, []).append({"line_number": line_number, "text": original_line})
            result["matches"].extend(
                {"file": file_path, "term": term, "matching_lines": matching_lines}
                for term, matching_lines in matching_lines_by_term.items()
            )
    result["status"] = "incomplete" if result["skipped"] else "complete"
    return result

"""Turn saved repository ZIPs into source excerpts for CSV reports and GPT.

Each dataset/file gets at most 12 excerpts containing matches and 12,000 text
characters in total, including nearby code. Long lines are shortened around the
matching term. Warnings identify omitted text; the saved ZIP and full scan results
are unchanged. Source filenames, commit IDs, line numbers, and character offsets
let a reader check each excerpt against the downloaded code.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path, PurePosixPath
import re
from typing import Any
from urllib.parse import quote
import zipfile
import zlib

from nasa_eo_search.catalog_csv import build_product_csv_row
from nasa_eo_search.dataset_identity import build_catalog_identity_rules, find_product_identifier_occurrences
from nasa_eo_search.evidence_inputs import identify_evidence, read_evidence_json
from nasa_eo_search.pipeline_catalog import read_pipeline_catalog
from nasa_eo_search.pipeline_tables import FILE_MATCH_COLUMNS
from nasa_eo_search.repository_discovery import validate_repository_name
from nasa_eo_search.repository_scan import MATCHING_RULE, inspect_zip_member_path


SNAPSHOT_MATCH_COLUMNS = FILE_MATCH_COLUMNS
MAX_EVIDENCE_CHARACTERS = 12_000
MAX_MATCHING_WINDOWS = 12
MATCHING_WINDOW_CHARACTERS = 600
CONTEXT_LINE_COUNT = 10
MAX_SNAPSHOT_FILE_BYTES = 2_000_000
CATALOG_IDENTITY_POLICY = "longest-catalog-identifier-v1"


def hash_snapshot_file(path: Path) -> str:
    """Calculate a saved file's SHA-256 checksum, reading one MiB at a time.

    Args:
        path: Saved catalog JSON, scan JSON, or repository ZIP to check.

    Returns:
        Lowercase hexadecimal checksum of the complete file contents.

    Raises:
        OSError: If the file cannot be read.
    """
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_saved_snapshot_file(directory: Path, relative_path: str, expected_digest: str) -> Path:
    """Check that a saved file is inside the search directory and unchanged.

    Call this before reading a catalog, scan result, or repository ZIP listed in
    the search metadata. The recorded checksum must match the file's contents.
    Paths outside the search directory and paths through symbolic links fail.

    Args:
        directory: Search output directory that must contain the file.
        relative_path: File path recorded in plan.json or snapshots.json, using
            forward slashes and relative to directory.
        expected_digest: SHA-256 checksum recorded when the file was saved.

    Returns:
        Checked file path inside the search output directory.

    Raises:
        ValueError: If the file path is invalid, missing, or crosses a symbolic
            link, or if its contents no longer match the recorded checksum.
        OSError: If the file cannot be read.
    """
    if (not isinstance(relative_path, str) or not relative_path or "\\" in relative_path
            or ":" in relative_path or relative_path.startswith("/")
            or any(part in ("", ".", "..") for part in relative_path.split("/"))
            or any(ord(character) < 32 for character in relative_path)
            or not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)):
        raise ValueError("Snapshot manifest contains an unsafe artifact path or digest.")
    path = directory.joinpath(*PurePosixPath(relative_path).parts)
    for parent in (path, *path.parents):
        if parent == directory:
            break
        if parent.is_symlink():
            raise ValueError("Snapshot artifacts cannot use symbolic links.")
    if not path.resolve().is_relative_to(directory.resolve()) or not path.is_file():
        raise ValueError("Snapshot artifact is missing or outside its output directory.")
    if hash_snapshot_file(path) != expected_digest:
        raise ValueError(f"Snapshot artifact hash differs: {relative_path}.")
    return path


def build_snapshot_excerpt_citation(repository: str, commit: str, file_path: str,
                                    line_number: int, character_offset: int, text: str, kind: str) -> dict:
    """Attach a source location and repeatable citation ID to one text excerpt.

    Args:
        repository: Public repository name, such as github.com/pydap/pydap.
        commit: Full Git commit ID used to download the repository ZIP.
        file_path: Source file path relative to the repository root.
        line_number: One-based source line number.
        character_offset: Zero-based character position where text begins in
            that line. This counts Unicode characters, not encoded bytes.
        text: Unmodified contiguous text from the source line.
        kind: "matching_line" for a match, "surrounding_code" for nearby lines,
            or "enclosing_code" for structural context added for classification.

    Returns:
        Dictionary containing the supplied location and text plus evidence_id,
        a content-derived ID used to cite the excerpt in classification results.
    """
    citation = {"repository": repository, "commit": commit, "file": file_path,
                "line_number": line_number, "character_offset": character_offset,
                "text": text, "kind": kind}
    citation["evidence_id"] = identify_evidence(citation)
    return citation


def validate_snapshot_matching_lines(source_lines: list[str], matches_by_term: dict[str, list[dict]]) -> None:
    """Verify every broad scan hit against the pinned source before filtering.

    Args:
        source_lines: Complete decoded lines from the verified repository ZIP.
        matches_by_term: Saved literal terms and their full matching-line records.

    Raises:
        ValueError: A saved line is malformed, absent, changed, or lacks its term.
    """
    for term, matching_lines in matches_by_term.items():
        if not matching_lines:
            raise ValueError("Snapshot scan match has no matching lines.")
        for matching_line in matching_lines:
            if not isinstance(matching_line, dict):
                raise ValueError("Snapshot matching lines must be objects.")
            number, text = matching_line.get("line_number"), matching_line.get("text")
            if (type(number) is not int or number < 1 or number > len(source_lines)
                    or not isinstance(text, str) or text != source_lines[number - 1]
                    or not find_product_identifier_occurrences(term, text)):
                raise ValueError("Saved snapshot match differs from its pinned source file.")


def build_snapshot_file_excerpts(repository: str, commit: str, file_path: str,
                                 source_lines: list[str], matches_by_term: dict[str, list[dict]],
                                 identifier_identity_rules: dict | None = None) -> dict:
    """Build size-limited match excerpts and nearby code from one source file.

    First check that every saved match agrees with the ZIP's source text. Then
    select up to 12 excerpts centered on matching terms, even on very long lines.
    Each excerpt is normally at most 600 characters, or up to 1,000 for a longer
    term. Matching text uses at most 9,000 characters. Up to ten lines before and
    after each selected match can supply context, subject to the overall 12,000
    character limit. The returned truncated flag reports omitted text.

    This only builds report/model input; it does not change the saved source or
    scan results. Character offsets always refer to the complete source line.

    Args:
        repository: Public repository name, such as github.com/pydap/pydap.
        commit: Full Git commit ID of the downloaded repository ZIP.
        file_path: Matched source path relative to the repository root.
        source_lines: Complete decoded source lines, without newline terminators.
        matches_by_term: Catalog terms mapped to their saved matching_lines
            records, each containing one-based line_number and full line text.
        identifier_identity_rules: Longer catalog identifiers and their ownership
            for this product. Excerpt windows center only on surviving occurrences.

    Returns:
        Dictionary with matching_lines for the CSV, evidence citations for
        matching and nearby text, a truncated flag, and counts of original
        matching lines, selected excerpts, and selected text characters.

    Raises:
        ValueError: If a saved matching line is missing, malformed, differs from
            source_lines, or does not contain its associated catalog term.
    """
    validate_snapshot_matching_lines(source_lines, matches_by_term)
    matching_numbers: set[int] = set()
    terms_by_number: dict[int, set[str]] = {}
    for term, matching_lines in matches_by_term.items():
        for matching_line in matching_lines:
            number, text = matching_line.get("line_number"), matching_line.get("text")
            if not find_product_identifier_occurrences(term, text, identifier_identity_rules):
                continue
            matching_numbers.add(number)
            terms_by_number.setdefault(number, set()).add(term)
    windows: list[tuple[int, int, int]] = []
    truncated = False
    for number in sorted(matching_numbers):
        line_text = source_lines[number - 1]
        if identifier_identity_rules:
            occurrences = sorted({span for term in terms_by_number[number]
                                  for span in find_product_identifier_occurrences(term, line_text, identifier_identity_rules)},
                                 key=lambda span: (span[0], -span[1]))
        else:
            # Preserve historical non-overlapping window selection exactly so
            # unchanged source can reuse frozen evidence and paid responses.
            pattern = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(
                re.escape(term) for term in sorted(terms_by_number[number], key=lambda value: (-len(value), value))
            ) + r")(?![A-Za-z0-9])", re.IGNORECASE | re.ASCII)
            occurrences = [(match.start(), match.end()) for match in pattern.finditer(line_text)]
        for occurrence_start, occurrence_end in occurrences:
            if any(saved_number == number and start <= occurrence_start and occurrence_end <= end
                   for saved_number, start, end in windows):
                continue
            if len(windows) == MAX_MATCHING_WINDOWS:
                truncated = True
                break
            width = min(1000, max(MATCHING_WINDOW_CHARACTERS, occurrence_end - occurrence_start))
            start = max(0, occurrence_start - max(0, width - (occurrence_end - occurrence_start)) // 2)
            end = min(len(line_text), start + width)
            windows.append((number, start, end))
        if len(windows) == MAX_MATCHING_WINDOWS and number != max(matching_numbers):
            truncated = True
            break
    evidence = []
    excerpts = []
    remaining_characters = MAX_EVIDENCE_CHARACTERS
    remaining_matching_characters = 9000
    for number, start, end in windows:
        original_end = end
        end = min(end, start + remaining_matching_characters)
        text = source_lines[number - 1][start:end]
        if not text:
            truncated = True
            break
        remaining_characters -= len(text)
        remaining_matching_characters -= len(text)
        excerpts.append({"line_number": number, "character_offset": start, "text": text, "excerpt": True})
        evidence.append(build_snapshot_excerpt_citation(repository, commit, file_path, number, start, text, "matching_line"))
        if start or end < len(source_lines[number - 1]) or end < original_end:
            truncated = True
    context_numbers = sorted({number for matching_number, _, _ in windows
                              for number in range(max(1, matching_number - CONTEXT_LINE_COUNT),
                                                  min(len(source_lines), matching_number + CONTEXT_LINE_COUNT) + 1)
                              if number not in matching_numbers})
    for number in context_numbers:
        line_text = source_lines[number - 1]
        if not remaining_characters:
            truncated = True
            break
        text = line_text[:min(MATCHING_WINDOW_CHARACTERS, remaining_characters)]
        if len(text) < len(line_text):
            truncated = True
        remaining_characters -= len(text)
        evidence.append(build_snapshot_excerpt_citation(repository, commit, file_path, number, 0, text, "surrounding_code"))
    if {item["line_number"] for item in excerpts} != matching_numbers:
        truncated = True
    return {"matching_lines": excerpts, "evidence": evidence, "truncated": truncated,
            "matching_line_count": len(matching_numbers), "matching_windows": len(excerpts),
            "evidence_characters": MAX_EVIDENCE_CHARACTERS - remaining_characters}


def read_matched_files_from_snapshot_zip(zip_path: Path, scan: dict, requested_files: set[str]) -> dict[str, list[str]]:
    """Read the requested matched files from a saved repository ZIP into memory.

    Return their complete UTF-8 source lines for excerpt selection. Nothing is
    extracted or executed. Each requested file must occur exactly once and be
    no larger than 2,000,000 uncompressed bytes.

    Args:
        zip_path: Saved repository ZIP whose checksum was already verified.
        scan: Saved scan result whose checksum was already verified. Its
            archive_root names the ZIP's optional enclosing directory.
        requested_files: Matched paths relative to the repository root, without
            the ZIP's enclosing directory name.

    Returns:
        Mapping from each requested path to its complete decoded source lines.
        An empty request returns an empty mapping without opening the ZIP.

    Raises:
        ValueError: If a requested file is missing, duplicated, encrypted,
            oversized, not UTF-8 text, or uses an unsafe path.
        OSError: If the ZIP cannot be read.
        zipfile.BadZipFile: If ZIP headers or file checksums are invalid.
        RuntimeError: If a ZIP member cannot be decompressed or decrypted.
        zlib.error: If a compressed member is damaged.
    """
    if not requested_files:
        return {}
    result: dict[str, list[str]] = {}
    archive_root = scan.get("archive_root", "")
    if not isinstance(archive_root, str) or "/" in archive_root or "\\" in archive_root:
        raise ValueError("Snapshot scan has an invalid archive root.")
    with zipfile.ZipFile(zip_path) as repository_zip:
        for member in repository_zip.infolist():
            components, unsafe_reason = inspect_zip_member_path(member)
            if unsafe_reason or member.is_dir():
                continue
            if archive_root and components[0] != archive_root:
                continue
            relative_path = "/".join(components[1:] if archive_root else components)
            if relative_path not in requested_files:
                continue
            if relative_path in result or member.file_size > MAX_SNAPSHOT_FILE_BYTES or member.flag_bits & 1:
                raise ValueError("Snapshot matched file is duplicated, encrypted, or too large.")
            with repository_zip.open(member) as source:
                raw = source.read(MAX_SNAPSHOT_FILE_BYTES + 1)
            if len(raw) > MAX_SNAPSHOT_FILE_BYTES or b"\x00" in raw:
                raise ValueError("Snapshot matched file exceeds the safe text limit.")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as error:
                raise ValueError("Snapshot matched file is not UTF-8 text.") from error
            result[relative_path] = [line.removesuffix("\r") for line in text.split("\n")]
    if set(result) != requested_files:
        raise ValueError("A snapshot matched file is missing or unsafe in the saved archive.")
    return result


def build_snapshot_match_row(group: dict, file_record: dict) -> dict[str, str]:
    """Build one matches.csv row for a dataset and matched repository file.

    Args:
        group: Repository name, catalog product metadata, and search warnings.
        file_record: File path, commit ID, selected matching_lines, matching
            search terms, and snapshot warnings for this dataset/file pair.

    Returns:
        Row using FILE_MATCH_COLUMNS. The snippet labels each excerpt with its
        original line number and character range; source_url links to the first
        selected line at the recorded commit. Classification retains this row.
    """
    excerpts = file_record["matching_lines"]
    product = group["product"]
    first_line = min(item["line_number"] for item in excerpts)
    warnings = list(group["warnings"])
    warnings.extend(file_record["snapshot_warnings"])
    return {
        "match_id": identify_evidence([group["repository"], product["provider"], product["short_name"],
                                       file_record["file"], file_record["commit"]]),
        "provider": product["provider"], "short_name": product["short_name"],
        "dataset_name": "; ".join(dict.fromkeys(collection["title"] for collection in product["collections"])),
        "repository": group["repository"], "file": file_record["file"], "commit": file_record["commit"],
        "line_numbers": "; ".join(str(number) for number in sorted({item["line_number"] for item in excerpts})),
        "snippet": "\n".join(f"{item['line_number']} [chars {item['character_offset']}:{item['character_offset'] + len(item['text'])}]: {item['text']}"
                              for item in excerpts),
        "source_url": file_record["file_url"] + f"#L{first_line}",
        "search_terms": "; ".join(sorted(source["term"] for source in file_record["sources"])),
        "search_complete": "false", "warnings": "\n".join(warnings),
    }


def read_snapshot_evidence_groups(output_directory: Path, catalog_override: Path | None = None,
                                  *, allow_running: bool = False, apply_catalog_identity: bool = True) -> dict[str, Any]:
    """Read saved GitHub search files and group excerpts by repository/dataset.

    Read plan.json, summary.json, catalog.json, and snapshots.json; verify each
    recorded file checksum before opening its scan JSON or repository ZIP.
    Check the full saved matching lines against the ZIP contents, associate
    each term with its catalog dataset, and build size-limited excerpts using
    build_snapshot_file_excerpts. The returned groups serve both CSV reporting
    and file-level classification without downloading any additional source.

    This uses the catalog copy in the search directory, so moving the directory
    does not require the original catalog. Generated CSVs and summary timestamps
    are not part of the source identity used to resume classification.

    Args:
        output_directory: Directory written by the GitHub search command.
        catalog_override: Optional catalog directory to check against the saved
            catalog copy. It must contain exactly the same metadata.
        allow_running: True only when the search command is updating its reports.
            Classification leaves this False to reject a search still running.
        apply_catalog_identity: Resolve overlapping identifiers using the frozen
            catalog. False reconstructs the original broad associations solely
            when resuming a legacy frozen classification plan.

    Returns:
        Dictionary accepted by file classification: groups contains one entry
        per repository/dataset, with per-file excerpts, citation locations, and
        CSV rows. Other fields describe the catalog, source files, repository
        statuses, and search limits. No files or network resources are changed.

    Raises:
        ValueError: If saved metadata is invalid or inconsistent, file checksums
            differ, matched text disagrees with the ZIP, or inputs change while
            being read. Also raised when classification tries a running search.
        OSError: If a saved JSON file or repository ZIP cannot be read.
    """
    directory = output_directory.resolve()
    plan = read_evidence_json(directory / "plan.json")
    if (plan.get("backend") != "github" or type(plan.get("schema_version")) is not int
            or plan["schema_version"] != 1 or plan.get("report_unit") != "file"
            or not isinstance(plan.get("catalog_directory"), str) or not plan["catalog_directory"].strip()):
        raise ValueError("Expected a schema-version-1 GitHub snapshot search plan.")
    summary = read_evidence_json(directory / "summary.json")
    if not allow_running and summary.get("status") not in {"complete", "incomplete", "failed", "interrupted"}:
        raise ValueError("Finish or stop the GitHub search before classifying its snapshots.")
    catalog_path = validate_saved_snapshot_file(directory, "catalog.json", plan.get("catalog_sha256"))
    catalog = read_evidence_json(catalog_path)
    catalog_summary, products = catalog.get("summary"), catalog.get("products")
    if (not isinstance(catalog_summary, dict) or not isinstance(products, list)
            or catalog_summary != plan.get("catalog_summary")
            or type(catalog_summary.get("schema_version")) is not int or catalog_summary["schema_version"] != 1
            or catalog_summary.get("status") not in {"complete", "incomplete", "failed", "interrupted"}
            or type(catalog_summary.get("products_saved")) is not int
            or catalog_summary.get("products_saved") != len(products)):
        raise ValueError("Frozen snapshot catalog is inconsistent with its search plan.")
    products_by_term: dict[str, list[dict]] = {}
    product_keys = set()
    for product in products:
        build_product_csv_row(product)
        identity = (product["provider"], product["short_name"])
        if identity in product_keys:
            raise ValueError("Frozen snapshot catalog contains duplicate products.")
        product_keys.add(identity)
        for signature in product["candidate_signatures"]:
            products_by_term.setdefault(signature["term"], []).append(product)
    catalog_identity_rules = build_catalog_identity_rules(products) if apply_catalog_identity else {}
    if catalog_override is not None:
        override_summary, override_products = read_pipeline_catalog(catalog_override)
        if override_summary != catalog_summary or override_products != products:
            raise ValueError("Catalog override differs from the frozen snapshot catalog.")
    manifest = read_evidence_json(directory / "snapshots.json")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1 or not isinstance(manifest.get("repositories"), list):
        raise ValueError("Expected a schema-version-1 snapshot manifest.")
    groups: dict[tuple[str, str, str], dict] = {}
    excluded_product_files = 0
    excluded_identifier_occurrences = 0
    snapshot_identities = []
    seen_repositories = set()
    for snapshot in manifest["repositories"]:
        if not isinstance(snapshot, dict):
            raise ValueError("Snapshot manifest records must be objects.")
        if not isinstance(snapshot.get("repository"), str):
            raise ValueError("Snapshot repository identity must be text.")
        repository = "github.com/" + validate_repository_name(snapshot.get("repository", ""))
        if repository.lower() in seen_repositories:
            raise ValueError("Snapshot manifest contains duplicate repositories.")
        seen_repositories.add(repository.lower())
        status = snapshot.get("status")
        if status not in {"pending", "pinned", "downloading", "downloaded", "complete", "incomplete", "error", "interrupted", "not_selected_repository_cap", "not_downloaded_total_byte_cap"}:
            raise ValueError("Snapshot manifest contains an unknown repository status.")
        zip_relative_path = snapshot.get("archive_relative_path")
        scan_relative = snapshot.get("scan_relative_path")
        zip_digest, scan_digest = snapshot.get("archive_sha256"), snapshot.get("scan_sha256")
        if bool(zip_relative_path) != bool(zip_digest) or bool(scan_relative) != bool(scan_digest) or scan_relative and not zip_relative_path:
            raise ValueError("Snapshot artifact paths and hashes must be paired.")
        commit = snapshot.get("commit")
        if (zip_relative_path or scan_relative or commit) and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)):
            raise ValueError("Snapshot requires an immutable commit identifier.")
        snapshot_identities.append({name: snapshot.get(name) for name in (
            "repository", "commit", "status", "archive_sha256", "scan_sha256", "discovered_by", "error",
        )})
        zip_path = validate_saved_snapshot_file(directory, zip_relative_path, zip_digest) if zip_relative_path else None
        if not scan_relative:
            if status in {"complete", "incomplete"}:
                raise ValueError("Finished snapshot record is missing its raw scan.")
            continue
        scan_path = validate_saved_snapshot_file(directory, scan_relative, scan_digest)
        scan = read_evidence_json(scan_path)
        if (scan.get("status") not in {"complete", "incomplete"} or not isinstance(scan.get("matches"), list)
                or not isinstance(scan.get("skipped"), list)
                or scan.get("matching_rule") != MATCHING_RULE
                or type(scan.get("term_count")) is not int or scan["term_count"] != len(products_by_term)):
            raise ValueError("Snapshot scan receipt is malformed.")
        if status in {"complete", "incomplete"} and status != scan["status"]:
            raise ValueError("Snapshot status differs from its raw scan receipt.")
        file_matches: dict[str, dict[str, list[dict]]] = {}
        for match in scan["matches"]:
            if (not isinstance(match, dict) or not isinstance(match.get("file"), str)
                    or not isinstance(match.get("term"), str) or match["term"] not in products_by_term
                    or not isinstance(match.get("matching_lines"), list)):
                raise ValueError("Snapshot scan has an invalid file, term, or matching lines.")
            file_path, term = match["file"], match["term"]
            if (not file_path or file_path.startswith("/") or "\\" in file_path or ":" in file_path
                    or any(part in {"", ".", ".."} for part in file_path.split("/"))
                    or any(ord(character) < 32 for character in file_path)):
                raise ValueError("Snapshot scan contains an unsafe matched path.")
            if term in file_matches.setdefault(file_path, {}):
                raise ValueError("Snapshot scan repeats a file/term record.")
            file_matches[file_path][term] = match["matching_lines"]
        try:
            source_files = read_matched_files_from_snapshot_zip(zip_path, scan, set(file_matches))
        except (zipfile.BadZipFile, RuntimeError, zlib.error) as error:
            raise ValueError("Matched snapshot source cannot be read reliably.") from error
        for file_path, matches_by_term in sorted(file_matches.items()):
            validate_snapshot_matching_lines(source_files[file_path], matches_by_term)
            product_matches: dict[tuple[str, str], dict] = {}
            for term, matching_lines in matches_by_term.items():
                for product in products_by_term[term]:
                    key = (product["provider"], product["short_name"])
                    rules = catalog_identity_rules.get(key, {})
                    surviving_lines = []
                    rejected_occurrences = 0
                    for line in matching_lines:
                        original_occurrences = find_product_identifier_occurrences(term, line["text"])
                        surviving_occurrences = find_product_identifier_occurrences(term, line["text"], rules)
                        rejected_occurrences += len(original_occurrences) - len(surviving_occurrences)
                        if surviving_occurrences:
                            surviving_lines.append(line)
                    excluded_identifier_occurrences += rejected_occurrences
                    association = product_matches.setdefault(key, {"product": product, "matches": {}, "identity_rules": {}})
                    if surviving_lines:
                        association["matches"][term] = surviving_lines
                    if rejected_occurrences:
                        association["identity_rules"][term] = rules[term]
            for product_key, association in sorted(product_matches.items()):
                if not association["matches"]:
                    excluded_product_files += 1
                    continue
                group_key = (repository, *product_key)
                group = groups.setdefault(group_key, {
                    "group_id": identify_evidence(group_key), "repository": repository,
                    "product": association["product"], "files": [], "search_complete": False,
                    "warnings": ["Bounded GitHub discovery is not complete public-repository coverage; matches are candidate references, not verified use."],
                })
                rules = association["identity_rules"]
                file_excerpts = build_snapshot_file_excerpts(repository, commit, file_path, source_files[file_path], association["matches"], rules)
                snapshot_warnings = [f"Pinned snapshot scan status: {scan['status']}; {len(scan['skipped'])} files excluded.",
                                     f"Evidence truncated: {'yes' if file_excerpts['truncated'] else 'no'}; at most {MAX_MATCHING_WINDOWS} hit windows and {MAX_EVIDENCE_CHARACTERS} text characters. Full evidence retained in {scan_relative} and {zip_relative_path}.",
                                     "Snippet offsets are zero-based Unicode character ranges; line numbers are one-based. Context comes only from the saved snapshot."]
                if catalog_summary.get("status") != "complete":
                    snapshot_warnings.append("The source catalog is partial.")
                record = {"repository": repository, "commit": commit, "file": file_path,
                          "file_url": f"https://{repository}/blob/{commit}/{quote(file_path, safe='/')}",
                          "matching_lines": file_excerpts["matching_lines"],
                          "sources": [{"term": term, "status": scan["status"], "latest_attempt": True} for term in sorted(association["matches"])],
                          "snapshot_evidence": file_excerpts["evidence"], "snapshot_evidence_truncated": file_excerpts["truncated"],
                          "snapshot_warnings": snapshot_warnings}
                if rules:
                    record["identifier_identity_rules"] = rules
                    record["identifier_valid_occurrences"] = {
                        term: [{"line_number": line["line_number"], "character_offset": start, "end_offset": end}
                               for line in lines
                               for start, end in find_product_identifier_occurrences(term, line["text"], rules)]
                        for term, lines in association["matches"].items()
                    }
                record["snapshot_match_row"] = build_snapshot_match_row(group, record)
                group["files"].append(record)
    if read_evidence_json(directory / "plan.json") != plan or read_evidence_json(directory / "snapshots.json") != manifest:
        raise ValueError("Snapshot sources changed while reading; stop the search and retry.")
    return {**({"dataset_identity_policy": CATALOG_IDENTITY_POLICY,
                "identity_filtering": {"excluded_product_file_associations": excluded_product_files,
                                       "excluded_identifier_occurrences": excluded_identifier_occurrences}}
               if apply_catalog_identity else {}),
            "backend": "github", "groups": [group for _, group in sorted(groups.items())],
            "catalog_status": catalog_summary["status"], "catalog_summary": catalog_summary,
            "search_scope": "Bounded public GitHub discovery; pinned local snapshot text; no exhaustive GitHub coverage or verified-use claim.",
            "selected_products": len(products), "source_batch_summary": summary,
            "source_product_queries": [{"status": item["status"], "snapshot": item} for item in snapshot_identities],
            "batch_directory": str(directory), "catalog_directory": plan.get("catalog_directory", "")}


def snapshot_match_rows(source_plan: dict) -> list[dict[str, str]]:
    """Copy the prepared matches.csv rows out of the repository/dataset groups.

    Args:
        source_plan: Result of read_snapshot_evidence_groups, including each
            file's snapshot_match_row.

    Returns:
        Independent row dictionaries in repository/dataset/file order. The
        search report and classifier use these same columns and values.
    """
    return [copy.deepcopy(record["snapshot_match_row"]) for group in source_plan["groups"] for record in group["files"]]

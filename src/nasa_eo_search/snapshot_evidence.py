"""Validate pinned local snapshots and build bounded, citation-ready file evidence."""

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
from nasa_eo_search.evidence_inputs import identify_evidence, read_evidence_json
from nasa_eo_search.pipeline_catalog import read_pipeline_catalog
from nasa_eo_search.pipeline_tables import FILE_MATCH_COLUMNS
from nasa_eo_search.trial_discovery import validate_repository_name
from nasa_eo_search.trial_scan import MATCHING_RULE, inspect_archive_member_path


SNAPSHOT_MATCH_COLUMNS = FILE_MATCH_COLUMNS
MAX_EVIDENCE_CHARACTERS = 12_000
MAX_MATCHING_WINDOWS = 12
MATCHING_WINDOW_CHARACTERS = 600
CONTEXT_LINE_COUNT = 10
MAX_SNAPSHOT_FILE_BYTES = 2_000_000


def hash_snapshot_file(path: Path) -> str:
    """Hash a saved artifact in bounded chunks without loading it into memory.

    Args:
        path: Existing regular file whose bytes define the saved artifact.

    Returns:
        Lowercase SHA-256 digest.

    Raises:
        OSError: If the artifact cannot be read.
    """
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_snapshot_artifact(directory: Path, relative_path: str, expected_digest: str) -> Path:
    """Validate a local artifact path and hash before trusting saved evidence.

    Args:
        directory: Search output directory that must contain the artifact.
        relative_path: POSIX relative path recorded in the frozen manifest.
        expected_digest: Recorded lowercase SHA-256 digest.

    Returns:
        Resolved artifact path inside the search output.

    Raises:
        ValueError: If a path is unsafe, symbolic, missing, or hash-inconsistent.
        OSError: If bytes cannot be inspected.
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


def make_snapshot_citation(repository: str, commit: str, file_path: str,
                           line_number: int, character_offset: int, text: str, kind: str) -> dict:
    """Identify an exact source excerpt without inventing omitted characters.

    Args:
        repository: Public repository identity.
        commit: Pinned revision.
        file_path: Repository-relative file path.
        line_number: One-based source line number.
        character_offset: Zero-based Unicode character offset within that line.
        text: Original contiguous source excerpt.
        kind: Matching-line or surrounding-code evidence type.

    Returns:
        Stable citation with its true location and preserved source text.
    """
    citation = {"repository": repository, "commit": commit, "file": file_path,
                "line_number": line_number, "character_offset": character_offset,
                "text": text, "kind": kind}
    citation["evidence_id"] = identify_evidence(citation)
    return citation


def bound_snapshot_file_evidence(repository: str, commit: str, file_path: str,
                                source_lines: list[str], matches_by_term: dict[str, list[dict]]) -> dict:
    """Select small actual-hit windows and nearby code from one pinned file.

    Matching windows are centered on actual term occurrences, including those
    far into a long line. At most twelve windows and 12,000 combined text
    characters are retained. Raw archives and scan receipts remain unchanged.

    Args:
        repository: Public repository identity.
        commit: Immutable source revision.
        file_path: Matched repository-relative path.
        source_lines: Decoded original file split on newline characters.
        matches_by_term: Original scan lines associated with each catalog term.

    Returns:
        Bounded matching-line records, matching/context citations, and explicit
        truncation metadata. Offsets refer to original Unicode characters.

    Raises:
        ValueError: If saved matching lines or terms disagree with the archive.
    """
    matching_numbers: set[int] = set()
    terms_by_number: dict[int, set[str]] = {}
    for term, matching_lines in matches_by_term.items():
        if not matching_lines:
            raise ValueError("Snapshot scan match has no matching lines.")
        pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", re.IGNORECASE | re.ASCII)
        for matching_line in matching_lines:
            if not isinstance(matching_line, dict):
                raise ValueError("Snapshot matching lines must be objects.")
            number, text = matching_line.get("line_number"), matching_line.get("text")
            if (type(number) is not int or number < 1 or number > len(source_lines)
                    or not isinstance(text, str) or text != source_lines[number - 1]
                    or pattern.search(text) is None):
                raise ValueError("Saved snapshot match differs from its pinned source file.")
            matching_numbers.add(number)
            terms_by_number.setdefault(number, set()).add(term)
    windows: list[tuple[int, int, int]] = []
    truncated = False
    for number in sorted(matching_numbers):
        line_text = source_lines[number - 1]
        pattern = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(
            re.escape(term) for term in sorted(terms_by_number[number], key=lambda value: (-len(value), value))
        ) + r")(?![A-Za-z0-9])", re.IGNORECASE | re.ASCII)
        for occurrence in pattern.finditer(line_text):
            if any(saved_number == number and start <= occurrence.start() and occurrence.end() <= end
                   for saved_number, start, end in windows):
                continue
            if len(windows) == MAX_MATCHING_WINDOWS:
                truncated = True
                break
            width = min(1000, max(MATCHING_WINDOW_CHARACTERS, occurrence.end() - occurrence.start()))
            start = max(0, occurrence.start() - max(0, width - (occurrence.end() - occurrence.start())) // 2)
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
        evidence.append(make_snapshot_citation(repository, commit, file_path, number, start, text, "matching_line"))
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
        evidence.append(make_snapshot_citation(repository, commit, file_path, number, 0, text, "surrounding_code"))
    if {item["line_number"] for item in excerpts} != matching_numbers:
        truncated = True
    return {"matching_lines": excerpts, "evidence": evidence, "truncated": truncated,
            "matching_line_count": len(matching_numbers), "matching_windows": len(excerpts),
            "evidence_characters": MAX_EVIDENCE_CHARACTERS - remaining_characters}


def read_snapshot_source_files(archive_path: Path, scan: dict, requested_files: set[str]) -> dict[str, list[str]]:
    """Read only requested safe text members without extraction or code execution.

    Args:
        archive_path: Hash-verified saved ZIP snapshot.
        scan: Hash-verified original scan receipt, including archive root.
        requested_files: Repository-relative paths with saved matches.

    Returns:
        Original decoded source lines for every requested matched file.

    Raises:
        ValueError: If a file is unsafe, duplicated, missing, oversized, or invalid.
        OSError: If the archive cannot be read.
    """
    if not requested_files:
        return {}
    result: dict[str, list[str]] = {}
    archive_root = scan.get("archive_root", "")
    if not isinstance(archive_root, str) or "/" in archive_root or "\\" in archive_root:
        raise ValueError("Snapshot scan has an invalid archive root.")
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            components, unsafe_reason = inspect_archive_member_path(member)
            if unsafe_reason or member.is_dir():
                continue
            if archive_root and components[0] != archive_root:
                continue
            relative_path = "/".join(components[1:] if archive_root else components)
            if relative_path not in requested_files:
                continue
            if relative_path in result or member.file_size > MAX_SNAPSHOT_FILE_BYTES or member.flag_bits & 1:
                raise ValueError("Snapshot matched file is duplicated, encrypted, or too large.")
            with archive.open(member) as source:
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
    """Render bounded source excerpts with true offsets in existing CSV columns.

    Args:
        group: Repository/product identity and coverage warnings.
        file_record: Bounded local evidence for one pinned file.

    Returns:
        Stable original match-table row reused unchanged by classification.
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
                                  *, allow_running: bool = False) -> dict[str, Any]:
    """Validate saved GitHub snapshots and build bounded local classification input.

    The frozen catalog, archive, and raw scan hashes are checked before evidence
    is accepted. Classification uses only local pinned files, not public context
    requests. Generated CSVs and operational timestamps do not define evidence
    identity. A relocated unchanged search remains usable without its old catalog.

    Args:
        output_directory: Saved GitHub search output containing plan and manifests.
        catalog_override: Optional original catalog to verify against frozen data.
        allow_running: Allow report publication during collection; classification
            must leave this false and use a finished or interrupted search.

    Returns:
        The source-plan shape used by existing file classification, with bounded
        file records, local citations, original rows, and snapshot coverage.

    Raises:
        ValueError: If schemas, hashes, catalog, paths, or source lines disagree.
        OSError: If saved artifacts cannot be read.
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
    catalog_path = resolve_snapshot_artifact(directory, "catalog.json", plan.get("catalog_sha256"))
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
    if catalog_override is not None:
        override_summary, override_products = read_pipeline_catalog(catalog_override)
        if override_summary != catalog_summary or override_products != products:
            raise ValueError("Catalog override differs from the frozen snapshot catalog.")
    manifest = read_evidence_json(directory / "snapshots.json")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1 or not isinstance(manifest.get("repositories"), list):
        raise ValueError("Expected a schema-version-1 snapshot manifest.")
    groups: dict[tuple[str, str, str], dict] = {}
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
        archive_relative = snapshot.get("archive_relative_path")
        scan_relative = snapshot.get("scan_relative_path")
        archive_digest, scan_digest = snapshot.get("archive_sha256"), snapshot.get("scan_sha256")
        if bool(archive_relative) != bool(archive_digest) or bool(scan_relative) != bool(scan_digest) or scan_relative and not archive_relative:
            raise ValueError("Snapshot artifact paths and hashes must be paired.")
        commit = snapshot.get("commit")
        if (archive_relative or scan_relative or commit) and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)):
            raise ValueError("Snapshot requires an immutable commit identifier.")
        snapshot_identities.append({name: snapshot.get(name) for name in (
            "repository", "commit", "status", "archive_sha256", "scan_sha256", "discovered_by", "error",
        )})
        archive_path = resolve_snapshot_artifact(directory, archive_relative, archive_digest) if archive_relative else None
        if not scan_relative:
            if status in {"complete", "incomplete"}:
                raise ValueError("Finished snapshot record is missing its raw scan.")
            continue
        scan_path = resolve_snapshot_artifact(directory, scan_relative, scan_digest)
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
            source_files = read_snapshot_source_files(archive_path, scan, set(file_matches))
        except (zipfile.BadZipFile, RuntimeError, zlib.error) as error:
            raise ValueError("Matched snapshot source cannot be read reliably.") from error
        for file_path, matches_by_term in sorted(file_matches.items()):
            product_matches: dict[tuple[str, str], dict] = {}
            for term, matching_lines in matches_by_term.items():
                for product in products_by_term[term]:
                    key = (product["provider"], product["short_name"])
                    product_matches.setdefault(key, {"product": product, "matches": {}})["matches"][term] = matching_lines
            for product_key, association in sorted(product_matches.items()):
                group_key = (repository, *product_key)
                group = groups.setdefault(group_key, {
                    "group_id": identify_evidence(group_key), "repository": repository,
                    "product": association["product"], "files": [], "search_complete": False,
                    "warnings": ["Bounded GitHub discovery is not complete public-repository coverage; matches are candidate references, not verified use."],
                })
                bounded = bound_snapshot_file_evidence(repository, commit, file_path, source_files[file_path], association["matches"])
                snapshot_warnings = [f"Pinned snapshot scan status: {scan['status']}; {len(scan['skipped'])} files excluded.",
                                     f"Evidence truncated: {'yes' if bounded['truncated'] else 'no'}; at most {MAX_MATCHING_WINDOWS} hit windows and {MAX_EVIDENCE_CHARACTERS} text characters. Full evidence retained in {scan_relative} and {archive_relative}.",
                                     "Snippet offsets are zero-based Unicode character ranges; line numbers are one-based. Context comes only from the saved snapshot."]
                if catalog_summary.get("status") != "complete":
                    snapshot_warnings.append("The source catalog is partial.")
                record = {"repository": repository, "commit": commit, "file": file_path,
                          "file_url": f"https://{repository}/blob/{commit}/{quote(file_path, safe='/')}",
                          "matching_lines": bounded["matching_lines"],
                          "sources": [{"term": term, "status": scan["status"], "latest_attempt": True} for term in sorted(association["matches"])],
                          "snapshot_evidence": bounded["evidence"], "snapshot_evidence_truncated": bounded["truncated"],
                          "snapshot_warnings": snapshot_warnings}
                record["snapshot_match_row"] = build_snapshot_match_row(group, record)
                group["files"].append(record)
    if read_evidence_json(directory / "plan.json") != plan or read_evidence_json(directory / "snapshots.json") != manifest:
        raise ValueError("Snapshot sources changed while reading; stop the search and retry.")
    return {"backend": "github", "groups": [group for _, group in sorted(groups.items())],
            "catalog_status": catalog_summary["status"], "catalog_summary": catalog_summary,
            "search_scope": "Bounded public GitHub discovery; pinned local snapshot text; no exhaustive GitHub coverage or verified-use claim.",
            "selected_products": len(products), "source_batch_summary": summary,
            "source_product_queries": [{"status": item["status"], "snapshot": item} for item in snapshot_identities],
            "batch_directory": str(directory), "catalog_directory": plan.get("catalog_directory", "")}


def snapshot_match_rows(source_plan: dict) -> list[dict[str, str]]:
    """Return the exact bounded original rows consumed by file classification.

    Args:
        source_plan: Validated result of ``read_snapshot_evidence_groups``.

    Returns:
        Independent copies of rows in stable repository/product/file order.
    """
    return [copy.deepcopy(record["snapshot_match_row"]) for group in source_plan["groups"] for record in group["files"]]

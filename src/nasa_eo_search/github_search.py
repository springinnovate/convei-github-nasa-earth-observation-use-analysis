"""Find public GitHub repositories, download their code, and report dataset matches.

This module implements the GitHub backend of ``nasa-eo search``. A repository
snapshot is a ZIP containing its files at one recorded Git commit. Search-page
responses and download/scan progress are saved so the same command can resume
without repeating successful requests. Small and large runs use this same code;
command-line options set the request, repository, and download-size limits.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.request import HTTPSHandler, Request, build_opener

from nasa_eo_search.batch_files import atomic_batch_output, lock_batch_directory
from nasa_eo_search.batch_reports import write_protected_csv_rows
from nasa_eo_search.evidence_context import RejectEvidenceRedirects
from nasa_eo_search.pipeline_catalog import read_pipeline_catalog
from nasa_eo_search.sourcegraph import build_tls_context
from nasa_eo_search.repository_discovery import (
    resolve_public_repository_commit, search_github_code, search_grep_app,
    validate_repository_name, validate_search_term,
)
from nasa_eo_search.repository_scan import MATCHING_RULE, scan_repository_zip


PLAN_DEFAULTS = {"github_pages": 10, "max_repositories": 6, "max_archive_mb": 100,
                 "max_total_mb": 300, "include_grep_app": False}
DISCOVERY_COLUMNS = ("provider", "term", "page", "attempt", "status", "repository", "file", "url")
SEARCH_COLUMNS = ("provider", "term", "status", "pages_saved", "candidate_files",
                  "auth_visible_total_count", "search_complete", "warnings")
REPOSITORY_COLUMNS = ("repository", "discovered_by", "selected", "commit", "status",
                      "archive_bytes", "files_scanned", "bytes_scanned", "skipped_files", "error")
RECEIPT_STATUSES = {"running", "returned", "capped", "incomplete", "limited", "error"}
SNAPSHOT_STATUSES = {"pending", "pinned", "downloading", "downloaded", "complete", "incomplete",
                     "error", "interrupted", "not_selected_repository_cap"}


def save_github_json(path: Path, value: Any) -> None:
    """Save a search record as JSON, replacing its file only after writing finishes.

    Args:
        path: Destination inside the search output directory.
        value: Request record, repository progress, settings, or summary to save.

    Raises:
        OSError: If the temporary file cannot be written or moved into place.
    """

    with atomic_batch_output(path) as output:
        json.dump(value, output, indent=2, ensure_ascii=True)


def hash_json_value(value: Any) -> str:
    """Calculate a reproducible SHA-256 checksum for a JSON-serializable value.

    Dictionary key order and JSON indentation do not affect this checksum.
    It is used to identify requests and check saved settings and responses.

    Args:
        value: Settings or a request record containing JSON-compatible values.

    Returns:
        Lowercase hexadecimal checksum of consistently formatted UTF-8 JSON.
    """

    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("utf-8")).hexdigest()


def hash_snapshot_file(path: Path) -> str:
    """Calculate the SHA-256 checksum of a saved ZIP, scan result, or catalog.

    Read one megabyte at a time so the whole file need not fit in memory.

    Args:
        path: File to read without changing it.

    Returns:
        Lowercase hexadecimal checksum of the complete file.

    Raises:
        OSError: If the file cannot be read.
    """

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_snapshot_path(output: Path, relative_path: str) -> Path:
    """Resolve a saved ZIP or JSON path and check that it stays inside the run.

    Args:
        output: Search output directory that must contain the file.
        relative_path: Path stored in a search record, using forward slashes.

    Returns:
        File path inside ``output``. The file need not exist yet.

    Raises:
        ValueError: If a path is absolute, contains parent-directory references,
            leaves ``output``, or passes through a symbolic link.
    """

    if not isinstance(relative_path, str) or not relative_path or "\\" in relative_path:
        raise ValueError("Invalid saved snapshot path.")
    components = relative_path.split("/")
    if any(component in {"", ".", ".."} or ":" in component for component in components):
        raise ValueError("Unsafe saved snapshot path.")
    path = output.joinpath(*components)
    if not path.resolve().is_relative_to(output.resolve()) or path.resolve() == output.resolve():
        raise ValueError("Snapshot path leaves its output directory.")
    for component_path in (path, *path.parents):
        if component_path == output:
            break
        if component_path.is_symlink():
            raise ValueError("Snapshot paths cannot contain symbolic links.")
    return path


def build_github_plan(arguments: argparse.Namespace, catalog: dict, saved: dict | None = None) -> dict:
    """Build the search settings for a new run or validate settings on resume.

    Select which dataset short names to send to GitHub and record the page,
    repository, and ZIP-size limits. Local scanning still uses every dataset in
    the catalog. When resuming, omitted options use their saved values; a changed
    catalog or changed fixed option is rejected rather than mixed with old results.

    Args:
        arguments: Parsed search options, including catalog and output paths.
        catalog: Complete catalog dictionary with ``summary`` and ``products``.
        saved: Contents of an existing ``plan.json``, or None for a new run.

    Returns:
        Settings to save as ``plan.json``, or the unchanged compatible saved
        settings. The per-invocation request limit is not part of this file.

    Raises:
        ValueError: If the selected names or limits are invalid, or the saved
            settings/catalog differ from the requested run.
    """

    if saved is not None and (saved.get("backend") != "github" or saved.get("schema_version") != 1
                              or saved.get("report_unit") != "file"):
        raise ValueError("Output is not a compatible GitHub snapshot search.")
    if saved is not None and (not isinstance(saved.get("discovery_terms"), list)
                              or any(name not in saved for name in PLAN_DEFAULTS)):
        raise ValueError("Saved GitHub plan is missing its discovery selection or limits.")
    available_names = sorted({product["short_name"] for product in catalog["products"]})
    requested_names = getattr(arguments, "short_name", None)
    names = (list(dict.fromkeys(requested_names)) if requested_names is not None
             else saved["discovery_terms"] if saved else available_names)
    if not names or set(names) - set(available_names):
        raise ValueError("Discovery short names must be present in the saved catalog.")
    for term in names:
        validate_search_term(term)
    limits = {}
    for name, default in PLAN_DEFAULTS.items():
        requested = getattr(arguments, name, None)
        limits[name] = requested if requested is not None else saved[name] if saved else default
    if type(limits["include_grep_app"]) is not bool:
        raise ValueError("include_grep_app must be boolean.")
    for name in ("github_pages", "max_repositories", "max_archive_mb", "max_total_mb"):
        if type(limits[name]) is not int or limits[name] < 1:
            raise ValueError(f"{name} must be a positive integer.")
    if limits["github_pages"] > 10:
        raise ValueError("GitHub exposes at most ten 100-result pages per query.")
    plan = {
        "schema_version": 1, "backend": "github", "report_unit": "file",
        "catalog_directory": str(arguments.catalog.resolve()), "catalog_summary": catalog["summary"],
        "catalog_fingerprint": hash_json_value(catalog), "discovery_terms": names, **limits,
        "matching_rule": MATCHING_RULE,
        "scope": "Bounded GitHub discovery and pinned public default-branch snapshots; not all GitHub repositories, branches, or history.",
    }
    if saved is not None:
        comparable_saved = {key: saved.get(key) for key in plan if key != "catalog_directory"}
        comparable_new = {key: value for key, value in plan.items() if key != "catalog_directory"}
        if comparable_saved != comparable_new:
            raise ValueError("Saved search selection, catalog, or limits differ; choose a new output directory.")
        return saved
    return plan


def discovery_receipt_path(output: Path, provider: str, term: str, page: int, attempt: int) -> Path:
    """Build the JSON filename for one attempt to fetch a search-result page.

    Hash the provider, dataset term, and page number so dataset names never become
    directory names. Include the attempt number to keep failed retries separate.

    Args:
        output: Search output directory.
        provider: ``github`` or ``grep.app``.
        term: Literal dataset short name.
        page: One-based provider page number.
        attempt: One-based request-attempt number.

    Returns:
        File path under ``output/discovery``; no file is created here.
    """

    identity = hash_json_value([provider, term, page])
    return output / "discovery" / f"{identity}-attempt-{attempt:06d}.json"


def save_discovery_receipt(path: Path, receipt: dict) -> None:
    """Save one search-page request record and a checksum of its contents.

    A receipt is the saved record of a request: its provider, term, page, attempt,
    status, and returned public matches. Save it with status ``running`` before
    the request, then replace it with the response so interruptions remain visible.

    Args:
        path: JSON filename for this page and attempt.
        receipt: Request information and, if available, its public-only response.

    Raises:
        OSError: If the request record cannot be saved.
    """

    payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    save_github_json(path, {**payload, "receipt_sha256": hash_json_value(payload)})


def read_discovery_receipts(output: Path, plan: dict) -> list[dict]:
    """Read saved search-page request records and verify their checksums and fields.

    Call this before resuming. Reject changed files, invalid filenames, and
    records for a different search so they cannot be mistaken for completed work.

    Args:
        output: Search output directory with request JSON files in ``discovery``.
        plan: Saved settings listing the allowed terms, providers, and page limit.

    Returns:
        Request records sorted by term, provider, page, and attempt. Failed
        attempts are included even if a later retry succeeded.

    Raises:
        OSError: If the request directory or its files cannot be read.
        ValueError: If a file is malformed, renamed, changed, or inconsistent
            with the saved search settings.
    """

    receipts = []
    providers = {"github", "grep.app"} if plan["include_grep_app"] else {"github"}
    directory = output / "discovery"
    if directory.is_symlink():
        raise ValueError("Discovery directory cannot be a symbolic link.")
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Unexpected or oversized discovery receipt.")
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            raise ValueError("Discovery receipt must be an object.")
        checksum = receipt.pop("receipt_sha256", None)
        if checksum != hash_json_value(receipt):
            raise ValueError("Discovery receipt checksum does not match; no requests made.")
        provider, term = receipt.get("provider"), receipt.get("term")
        page, attempt = receipt.get("page"), receipt.get("attempt")
        if (provider not in providers or term not in plan["discovery_terms"]
                or type(page) is not int or not 1 <= page <= plan["github_pages"]
                or (provider == "grep.app" and page != 1) or type(attempt) is not int or attempt < 1
                or receipt.get("status") not in RECEIPT_STATUSES or not isinstance(receipt.get("results"), list)):
            raise ValueError("Invalid saved discovery request metadata.")
        if discovery_receipt_path(output, provider, term, page, attempt) != path:
            raise ValueError("Discovery receipt name does not match its request.")
        for result in receipt["results"]:
            if not isinstance(result, dict):
                raise ValueError("Invalid saved discovery location.")
            validate_repository_name(result.get("repository", ""))
            if not isinstance(result.get("file"), str) or not isinstance(result.get("url"), str):
                raise ValueError("Invalid saved discovery file or URL.")
        receipts.append(receipt)
    return sorted(receipts, key=lambda row: (row["term"], row["provider"], row["page"], row["attempt"]))


def latest_discovery_receipts(receipts: list[dict]) -> dict[tuple, dict]:
    """Return the highest-numbered request attempt for each search-result page.

    Args:
        receipts: Validated request records, including earlier failed attempts.

    Returns:
        Mapping from ``(provider, term, page)`` to the latest request record.
    """

    latest = {}
    for receipt in receipts:
        identity = (receipt["provider"], receipt["term"], receipt["page"])
        if identity not in latest or receipt["attempt"] > latest[identity]["attempt"]:
            latest[identity] = receipt
    return latest


def collect_github_discovery(output: Path, plan: dict, receipts: list[dict], maximum_requests: int,
                             retry_incomplete: bool, ca_bundle: str | None) -> int:
    """Fetch unfinished search pages and save a record before and after each call.

    Request page one for each term before moving to page two. Reuse successful
    saved pages, stop at the invocation's request limit, and update ``receipts``
    in place as requests start and finish. Optional grep.app calls occur only on
    page one because that service does not expose pagination here.

    Args:
        output: Search output directory already locked by the caller.
        plan: Saved terms, enabled services, and maximum pages per term.
        receipts: Validated request records to update in place.
        maximum_requests: Maximum discovery calls during this invocation.
        retry_incomplete: Allow one new attempt for a previously failed,
            incomplete, or interrupted page during this invocation.
        ca_bundle: Optional certificate-authority file for grep.app HTTPS calls.

    Returns:
        Number of discovery calls made in this invocation.

    Raises:
        OSError: If a request record cannot be saved.
        KeyboardInterrupt: If interrupted; its ``running`` record remains saved.
    """

    latest = latest_discovery_receipts(receipts)
    calls = 0
    for page in range(1, plan["github_pages"] + 1):
        for term in plan["discovery_terms"]:
            providers = ("github", "grep.app") if page == 1 and plan["include_grep_app"] else ("github",)
            for provider in providers:
                identity = (provider, term, page)
                prior = latest.get(identity)
                if provider == "github" and page > 1:
                    previous = latest.get((provider, term, page - 1))
                    if (not previous or previous["status"] in {"running", "error", "incomplete"}
                            or not previous.get("has_more", False)):
                        continue
                if prior and (prior["status"] not in {"running", "error", "incomplete"} or not retry_incomplete):
                    continue
                if calls >= maximum_requests:
                    return calls
                attempt = prior["attempt"] + 1 if prior else 1
                path = discovery_receipt_path(output, provider, term, page, attempt)
                started = {"provider": provider, "term": term, "page": page, "attempt": attempt,
                           "status": "running", "results": [], "warnings": ["Request started; final response not yet saved."],
                           "started_at": datetime.now(timezone.utc).isoformat()}
                save_discovery_receipt(path, started)
                receipts.append(started)
                print(f"Discovering {term!r} with {provider}, page {page} ({calls + 1}/{maximum_requests})...", file=sys.stderr, flush=True)
                if provider == "github":
                    response = search_github_code(term, max_pages=1, start_page=page)
                else:
                    response = search_grep_app(term, ca_bundle_path=ca_bundle)
                finished = {**started, **response, "page": page, "attempt": attempt}
                save_discovery_receipt(path, finished)
                receipts[-1] = finished
                latest[identity] = finished
                calls += 1
    return calls


def reconcile_snapshot_repositories(receipts: list[dict], repositories: list[dict], plan: dict) -> list[dict]:
    """Add newly discovered repositories and select which ones to download.

    Keep previously selected repositories. Fill remaining slots by taking one
    candidate from each term/service in turn, up to ``max_repositories``. Only
    repositories returned by a search service are eligible; no examples are added
    by hand. Each record lists the searches that found it.

    Args:
        receipts: Saved request records, including partial public results.
        repositories: Existing repository records from ``snapshots.json``.
        plan: Saved dataset order and maximum number of repository downloads.

    Returns:
        Repository records with ``discovered_by``, ``selected``, and ``status``
        fields. Unselected candidates remain listed in the output.
    """

    by_repository = {row["repository"].lower(): dict(row) for row in repositories}
    candidate_lists = []
    for term in plan["discovery_terms"]:
        for provider in ("github", "grep.app"):
            candidates = []
            matching_receipts = sorted((receipt for receipt in receipts if receipt["term"] == term
                                        and receipt["provider"] == provider), key=lambda row: (row["page"], row["attempt"]))
            for receipt in matching_receipts:
                for match in receipt["results"]:
                    repository = match["repository"]
                    identity = repository.lower()
                    record = by_repository.setdefault(identity, {
                        "repository": repository, "selected": False, "commit": "", "status": "not_selected_repository_cap",
                        "archive_relative_path": "", "scan_relative_path": "", "archive_sha256": "", "scan_sha256": "",
                        "discovered_by": [], "error": "", "metrics": {}, "download_attempts": 0,
                    })
                    label = f"{provider}:{term}"
                    if label not in record["discovered_by"]:
                        record["discovered_by"].append(label)
                    if identity not in candidates:
                        candidates.append(identity)
            candidate_lists.append(candidates)
    selected_count = sum(row.get("selected", False) for row in by_repository.values())
    for rank in range(max((len(items) for items in candidate_lists), default=0)):
        for candidates in candidate_lists:
            if rank >= len(candidates):
                continue
            record = by_repository[candidates[rank]]
            if not record["selected"] and selected_count < plan["max_repositories"]:
                record["selected"], record["status"] = True, "pending"
                selected_count += 1
    return list(by_repository.values())


def validate_snapshot_manifest(output: Path, repositories: list[dict], plan: dict) -> None:
    """Check repository records and the ZIP/scan files listed in snapshots.json.

    Verify paths, checksums, statuses, unique repository names, and the selected
    repository limit before making more requests or overwriting reports.

    Args:
        output: Search directory containing the recorded ZIP and scan JSON files.
        repositories: The ``repositories`` list read from ``snapshots.json``.
        plan: Saved settings with the maximum selected-repository count.

    Raises:
        OSError: If a saved ZIP or scan JSON file cannot be read.
        ValueError: If a repository record is invalid, a saved file is missing or
            changed, or the number of selected repositories exceeds the limit.
    """

    seen = set()
    if not (output / "snapshots").is_dir() or (output / "snapshots").is_symlink():
        raise ValueError("Snapshot cache directory is missing or symbolic.")
    for row in repositories:
        if not isinstance(row, dict):
            raise ValueError("Invalid saved repository manifest.")
        identity = validate_repository_name(row.get("repository", "")).lower()
        if identity in seen or row.get("status") not in SNAPSHOT_STATUSES or type(row.get("selected")) is not bool:
            raise ValueError("Duplicate repository or invalid snapshot status.")
        seen.add(identity)
        if row.get("commit") and not re.fullmatch(r"[0-9a-f]{40}", row["commit"]):
            raise ValueError("Invalid pinned snapshot commit.")
        if not isinstance(row.get("metrics"), dict) or not isinstance(row.get("discovered_by"), list):
            raise ValueError("Snapshot metadata is malformed.")
        for prefix in ("archive", "scan"):
            relative = row.get(f"{prefix}_relative_path", "")
            expected_hash = row.get(f"{prefix}_sha256", "")
            if bool(relative) != bool(expected_hash):
                raise ValueError("Snapshot evidence reference is missing its checksum.")
            if relative:
                if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                    raise ValueError("Snapshot evidence checksum is malformed.")
                path = safe_snapshot_path(output, relative)
                if not path.is_file() or hash_snapshot_file(path) != expected_hash:
                    raise ValueError("Cached snapshot evidence is missing or damaged; refusing to redownload or replace it.")
        if row["status"] in {"complete", "incomplete"} and not (row.get("archive_relative_path") and row.get("scan_relative_path")):
            raise ValueError("Finished snapshot is missing its archive or scan receipt.")
    if sum(row["selected"] for row in repositories) > plan["max_repositories"]:
        raise ValueError("Saved snapshot selection exceeds its repository limit.")


def download_github_repository_zip(repository: str, commit: str, zip_destination_path: Path,
                                   maximum_download_bytes: int, ca_bundle: str | None = None) -> int:
    """Download a public repository's files at one Git commit as a ZIP file.

    Download from GitHub's codeload service without credentials or redirects.
    Write to a new ``.partial`` file and rename it to ``zip_destination_path``
    only after success. A failed download keeps its partial bytes so subsequent
    runs can count them against the total storage limit. Nothing is extracted.

    Args:
        repository: GitHub repository already checked to be public.
        commit: Full 40-character Git commit identifier to download.
        zip_destination_path: New ZIP path; an existing file is never replaced.
        maximum_download_bytes: Maximum compressed bytes to download. Detecting
            an over-limit response can retain one additional byte.
        ca_bundle: Optional certificate-authority file for the HTTPS connection.

    Returns:
        Number of bytes in the successfully downloaded ZIP.

    Raises:
        OSError: If the HTTPS request, file write, or 20-second socket timeout fails.
        ValueError: If repository/commit inputs are invalid, the ZIP already
            exists, or the byte limit or 90-second elapsed limit is exceeded.
    """

    repository_name = validate_repository_name(repository)
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or maximum_download_bytes < 1:
        raise ValueError("A pinned commit and positive remaining byte budget are required.")
    if zip_destination_path.exists():
        raise ValueError("Snapshot destination already exists; refusing to replace it.")
    request = Request(f"https://codeload.github.com/{repository_name}/zip/{commit}",
                      headers={"User-Agent": "nasa-eo-github-search/0.1"})
    opener = build_opener(RejectEvidenceRedirects(), HTTPSHandler(context=build_tls_context(ca_bundle)))
    partial_zip_path = zip_destination_path.with_suffix(".partial")
    download_started_at, downloaded_bytes = time.monotonic(), 0
    with opener.open(request, timeout=20) as response:
        declared_zip_bytes = response.headers.get("Content-Length")
        if declared_zip_bytes and int(declared_zip_bytes) > maximum_download_bytes:
            raise ValueError("Archive exceeds the compressed download budget.")
        with partial_zip_path.open("xb") as zip_output:
            while True:
                if time.monotonic() - download_started_at > 90:
                    raise ValueError("Archive exceeded the 90-second download budget.")
                zip_chunk = response.read(min(65536, maximum_download_bytes - downloaded_bytes + 1))
                if not zip_chunk:
                    break
                zip_output.write(zip_chunk)
                downloaded_bytes += len(zip_chunk)
                if downloaded_bytes > maximum_download_bytes:
                    raise ValueError("Archive exceeds the compressed download budget; partial bytes retained.")
    partial_zip_path.replace(zip_destination_path)
    return downloaded_bytes


def scan_selected_snapshots(output: Path, plan: dict, catalog: dict, repositories: list[dict],
                             retry_incomplete: bool, ca_bundle: str | None) -> None:
    """Download selected repository ZIPs and scan them for every catalog identifier.

    Record each repository's default-branch commit before downloading. Save
    progress to ``snapshots.json`` after each stage so a resumed run can reuse
    an existing ZIP or completed scan. Failed downloads and scans are recorded
    on their repository rows; other selected repositories can still proceed.

    Args:
        output: Search output directory already locked by the caller.
        plan: Saved per-ZIP and total compressed-download limits.
        catalog: Full saved catalog, not just the products used to find repositories.
        repositories: Repository records to update in place and save after each stage.
        retry_incomplete: Retry failed or interrupted downloads when True.
        ca_bundle: Optional certificate-authority file for ZIP downloads.

    Raises:
        OSError: If repository progress cannot be saved to ``snapshots.json``.
        KeyboardInterrupt: If interrupted; completed commit/download steps remain saved.
    """

    terms = list(dict.fromkeys(candidate["term"] for product in catalog["products"]
                               for candidate in product["candidate_signatures"]))
    for record in repositories:
        if not record["selected"] or record["status"] in {"complete", "incomplete"}:
            continue
        if record["status"] in {"error", "interrupted", "downloading"} and not retry_incomplete:
            continue
        try:
            if not record["commit"]:
                record["commit"] = resolve_public_repository_commit(record["repository"])
                record["status"] = "pinned"
                save_github_json(output / "snapshots.json", {"schema_version": 1, "repositories": repositories})
            if not record["archive_relative_path"]:
                stored_bytes = sum(path.stat().st_size for path in (output / "snapshots").iterdir()
                                   if path.is_file() and path.suffix in {".zip", ".partial"})
                available_bytes = min(plan["max_archive_mb"] * 1_000_000,
                                      plan["max_total_mb"] * 1_000_000 - stored_bytes)
                if available_bytes < 1:
                    raise ValueError("Aggregate compressed download budget has been exhausted.")
                record["download_attempts"] = record.get("download_attempts", 0) + 1
                zip_filename = f"{hash_json_value(record['repository'].lower())}-attempt-{record['download_attempts']:06d}.zip"
                zip_path = safe_snapshot_path(output, f"snapshots/{zip_filename}")
                record["status"] = "downloading"
                save_github_json(output / "snapshots.json", {"schema_version": 1, "repositories": repositories})
                print(f"Downloading pinned snapshot {record['repository']}...", file=sys.stderr, flush=True)
                downloaded_zip_bytes = download_github_repository_zip(
                    record["repository"], record["commit"], zip_path, available_bytes, ca_bundle,
                )
                record.update({"status": "downloaded", "archive_relative_path": f"snapshots/{zip_filename}",
                               "archive_sha256": hash_snapshot_file(zip_path), "error": ""})
                record["metrics"]["archive_bytes"] = downloaded_zip_bytes
                save_github_json(output / "snapshots.json", {"schema_version": 1, "repositories": repositories})
            zip_path = safe_snapshot_path(output, record["archive_relative_path"])
            started = time.monotonic()
            scan = scan_repository_zip(zip_path, terms)
            scan_path = zip_path.with_suffix(".scan.json")
            save_github_json(scan_path, scan)
            record.update({"status": scan["status"], "scan_relative_path": scan_path.relative_to(output).as_posix(),
                           "scan_sha256": hash_snapshot_file(scan_path), "error": ""})
            record["metrics"].update({key: scan[key] for key in ("archive_bytes", "files_scanned", "bytes_scanned", "files_considered")})
            record["metrics"].update({"skipped_files": len(scan["skipped"]), "scan_seconds": round(time.monotonic() - started, 3)})
            read_errors = {item.get("reason") for item in scan["skipped"]} & {"invalid_archive", "archive_member_read_error"}
            if read_errors:
                record["status"] = "error"
                record["error"] = "Snapshot bytes could not be read reliably: " + ", ".join(sorted(read_errors))
        except (OSError, ValueError, RuntimeError) as error:
            record["status"], record["error"] = "error", str(error)
        finally:
            save_github_json(output / "snapshots.json", {"schema_version": 1, "repositories": repositories})


def summarize_discovery_queries(plan: dict, receipts: list[dict]) -> list[dict]:
    """Summarize saved page results for each dataset term and search service.

    Use the latest attempt for each page to count distinct candidate files and
    determine whether that query finished, needs more pages, hit its page limit,
    or failed. A finished query still does not mean all of GitHub was searched.

    Args:
        plan: Saved terms, enabled services, and page limit.
        receipts: Saved request records, including partial results and failures.

    Returns:
        One ``searches.csv`` row per term/service, including page and candidate
        counts, warnings, and status. ``search_complete`` is always False.
    """

    latest = latest_discovery_receipts(receipts)
    rows = []
    for term in plan["discovery_terms"]:
        for provider in (("github", "grep.app") if plan["include_grep_app"] else ("github",)):
            pages = sorted((receipt for identity, receipt in latest.items() if identity[:2] == (provider, term)),
                           key=lambda receipt: receipt["page"])
            status = "pending"
            if pages:
                last = pages[-1]
                if any(receipt["status"] in {"error", "running"} for receipt in pages):
                    status = "error"
                elif any(receipt["status"] == "incomplete" for receipt in pages):
                    status = "incomplete"
                elif provider == "grep.app":
                    status = "limited"
                elif last.get("has_more", False):
                    status = "capped" if last["page"] >= plan["github_pages"] else "pending"
                else:
                    status = "returned"
            matches = {(item["repository"].lower(), item["file"]) for receipt in pages for item in receipt["results"]}
            warnings = list(dict.fromkeys(warning for receipt in pages for warning in receipt.get("warnings", [])))
            rows.append({"provider": provider, "term": term, "status": status, "pages_saved": len(pages),
                         "candidate_files": len(matches), "auth_visible_total_count": pages[-1].get("total_count", "") if pages else "",
                         "search_complete": False, "warnings": "\n".join(warnings)})
    return rows


def publish_github_reports(output: Path, plan: dict, catalog: dict, receipts: list[dict],
                            repositories: list[dict], requests_this_run: int,
                            forced_status: str | None = None) -> dict:
    """Write search, repository, and matching-file reports from saved run records.

    Rebuild the four CSV tables and ``summary.json``. Matching-file snippets are
    generated from verified ZIPs by the same code used to prepare classification
    input. This keeps the readable report and model input tied to the same files.

    Args:
        output: Search output directory.
        plan: Saved search settings.
        catalog: Saved dictionary containing all catalog products.
        receipts: All saved search-page request records.
        repositories: Current repository download/scan records.
        requests_this_run: Search-service calls made in this invocation.
        forced_status: Optional ``running`` or ``interrupted`` summary status.

    Returns:
        The summary dictionary written to ``summary.json``. Its status describes
        the configured work, not coverage of all public GitHub code.

    Raises:
        OSError: If saved files cannot be read or reports cannot be written.
        ValueError: If saved ZIPs or scan records fail validation.
    """

    from nasa_eo_search.snapshot_evidence import (
        SNAPSHOT_MATCH_COLUMNS, read_snapshot_evidence_groups, snapshot_match_rows,
    )

    search_rows = summarize_discovery_queries(plan, receipts)
    errors = any(row["status"] == "error" for row in search_rows + repositories)
    limited = (any(row["status"] in {"pending", "capped", "incomplete", "limited"} for row in search_rows)
               or any(row["status"] != "complete" for row in repositories))
    status = forced_status or ("failed" if errors else "incomplete" if limited else "complete")
    summary = {
        "status": status, "backend": "github", "search_complete": False,
        "scope": plan["scope"], "local_product_count": len(catalog["products"]),
        "local_term_count": len({candidate["term"] for product in catalog["products"] for candidate in product["candidate_signatures"]}),
        "discovery_queries": len(search_rows), "discovery_requests_saved": len(receipts),
        "requests_this_run": requests_this_run, "repositories_discovered": len(repositories),
        "repositories_selected": sum(row["selected"] for row in repositories),
        "repositories_scanned": sum(row["status"] in {"complete", "incomplete"} for row in repositories),
        "warnings": ["Discovery indexes, page caps, and the repository sample do not cover all public GitHub code.",
                     "Snapshot completion describes only readable saved files; exclusions are retained in scan receipts."],
    }
    save_github_json(output / "summary.json", summary)
    save_github_json(output / "snapshots.json", {"schema_version": 1, "repositories": repositories})
    discovery_rows = [{"provider": receipt["provider"], "term": receipt["term"], "page": receipt["page"],
                       "attempt": receipt["attempt"], "status": receipt["status"], **match}
                      for receipt in receipts for match in receipt["results"]]
    repository_rows = [{**row, **row["metrics"], "discovered_by": "; ".join(row["discovered_by"])} for row in repositories]
    write_protected_csv_rows(output / "discovery.csv", DISCOVERY_COLUMNS, discovery_rows)
    write_protected_csv_rows(output / "searches.csv", SEARCH_COLUMNS, search_rows)
    write_protected_csv_rows(output / "repositories.csv", REPOSITORY_COLUMNS, repository_rows)
    source_plan = read_snapshot_evidence_groups(output, allow_running=True)
    matches = snapshot_match_rows(source_plan)
    write_protected_csv_rows(output / "matches.csv", SNAPSHOT_MATCH_COLUMNS, matches)
    summary["match_rows"] = len(matches)
    save_github_json(output / "summary.json", summary)
    return summary


def search_github_catalog(arguments: argparse.Namespace) -> int:
    """Search for catalog names on GitHub, scan repository ZIPs, and write CSVs.

    This is the GitHub implementation of ``nasa-eo search``. Read the saved NASA
    catalog, fetch search pages within the requested limits, choose repositories,
    and download each at a recorded commit. Scan every catalog identifier in each
    selected ZIP and write one row per dataset/file match to ``matches.csv``.
    An existing output directory resumes from its saved pages and download/scan
    progress. Downloaded code is read as text, never executed.

    Args:
        arguments: Parsed command-line options: catalog/output directories,
            optional short names, limits, retry flag, dry-run flag, and certificate
            file. Omitted fixed options use saved values when resuming. The
            request limit defaults to ten new search-service calls per invocation.

    Returns:
        Exit status: 0 for a dry run or completed configured work; 4 when limits,
        skipped files, or pending work remain; 1 for service/download/report
        failures; or 130 after interruption.

    Raises:
        OSError: If startup cannot read the catalog or saved files, create the
            output directory, or acquire its lock.
        ValueError: If startup options are invalid or conflict with saved settings,
            or saved files are changed or damaged. They are not replaced silently.
    """

    maximum_requests = getattr(arguments, "max_requests", None)
    maximum_requests = 10 if maximum_requests is None else maximum_requests
    if type(maximum_requests) is not int or maximum_requests < 1:
        raise ValueError("max_requests must be a positive integer.")
    summary, products = read_pipeline_catalog(arguments.catalog)
    catalog = {"summary": summary, "products": products}
    output = arguments.output.resolve()
    if output.exists() and any((output / name).is_symlink() for name in ("plan.json", "catalog.json", "snapshots.json")):
        raise ValueError("Saved manifests cannot be symbolic links.")
    saved = json.loads((output / "plan.json").read_text(encoding="utf-8")) if output.exists() else None
    plan = build_github_plan(arguments, catalog, saved)
    print(f"GitHub discovery: {len(plan['discovery_terms'])} dataset names; at most {maximum_requests} new calls this run. "
          f"Local scan: all {len(products)} catalog datasets in at most {plan['max_repositories']} pinned repositories.",
          file=sys.stderr, flush=True)
    if getattr(arguments, "dry_run", False):
        return 0
    if saved is None:
        output.mkdir(parents=True, exist_ok=False)
        (output / "discovery").mkdir()
        (output / "snapshots").mkdir()
        save_github_json(output / "catalog.json", catalog)
        plan["catalog_sha256"] = hash_snapshot_file(output / "catalog.json")
        save_github_json(output / "plan.json", plan)
        save_github_json(output / "snapshots.json", {"schema_version": 1, "repositories": []})
    with lock_batch_directory(output):
        if json.loads((output / "plan.json").read_text(encoding="utf-8")) != plan:
            raise ValueError("Search plan changed while starting; retry after inspecting it.")
        frozen_catalog_path = safe_snapshot_path(output, "catalog.json")
        if hash_snapshot_file(frozen_catalog_path) != plan.get("catalog_sha256"):
            raise ValueError("Saved catalog is damaged; refusing to replace it.")
        frozen_catalog = json.loads(frozen_catalog_path.read_text(encoding="utf-8"))
        if hash_json_value(frozen_catalog) != plan["catalog_fingerprint"]:
            raise ValueError("Saved catalog does not match the frozen plan.")
        manifest = json.loads((output / "snapshots.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(manifest.get("repositories"), list):
            raise ValueError("Invalid saved snapshot manifest.")
        repositories = manifest["repositories"]
        validate_snapshot_manifest(output, repositories, plan)
        receipts = read_discovery_receipts(output, plan)
        original_receipt_count = len(receipts)
        requests_this_run = 0
        try:
            publish_github_reports(output, plan, catalog, receipts, repositories, 0, "running")
            requests_this_run = collect_github_discovery(output, plan, receipts, maximum_requests,
                                                       getattr(arguments, "retry_incomplete", False), arguments.ca_bundle)
            repositories = reconcile_snapshot_repositories(receipts, repositories, plan)
            save_github_json(output / "snapshots.json", {"schema_version": 1, "repositories": repositories})
            scan_selected_snapshots(output, plan, catalog, repositories,
                                    getattr(arguments, "retry_incomplete", False), arguments.ca_bundle)
            final_summary = publish_github_reports(output, plan, catalog, receipts, repositories, requests_this_run)
        except KeyboardInterrupt:
            repositories = reconcile_snapshot_repositories(receipts, repositories, plan)
            requests_this_run = len(receipts) - original_receipt_count
            publish_github_reports(output, plan, catalog, receipts, repositories, requests_this_run, "interrupted")
            return 130
        except (OSError, ValueError, RuntimeError) as error:
            save_github_json(output / "summary.json", {
                "status": "failed", "backend": "github", "search_complete": False,
                "scope": plan["scope"], "error": str(error),
                "requests_this_run": len(receipts) - original_receipt_count,
                "discovery_requests_saved": len(receipts),
                "warnings": ["Collection or report publication failed; saved evidence was retained."],
            })
            print(f"GitHub search failed: {error}", file=sys.stderr)
            return 1
        print(f"Saved {final_summary['match_rows']} dataset/file rows to {output / 'matches.csv'}. "
              f"Status: {final_summary['status']}; global search coverage is incomplete.", file=sys.stderr, flush=True)
        return {"complete": 0, "incomplete": 4, "failed": 1}[final_summary["status"]]

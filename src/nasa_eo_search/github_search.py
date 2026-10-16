"""Resume bounded GitHub discovery and scan immutable public source snapshots."""

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
from nasa_eo_search.trial_discovery import (
    resolve_public_repository_commit, search_github_code, search_grep_app,
    validate_repository_name, validate_search_term,
)
from nasa_eo_search.trial_scan import MATCHING_RULE, scan_repository_archive


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
    """Atomically publish JSON owned by a GitHub search run.

    Args:
        path: Destination inside the search output directory.
        value: JSON-serializable receipt, manifest, or report metadata.

    Raises:
        OSError: If the output cannot be written or published.
    """

    with atomic_batch_output(path) as output:
        json.dump(value, output, indent=2, ensure_ascii=True)


def hash_json_value(value: Any) -> str:
    """Fingerprint a JSON value independently of indentation and key order.

    Args:
        value: JSON-serializable configuration or discovery receipt.

    Returns:
        SHA-256 digest of canonical UTF-8 JSON bytes.
    """

    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("utf-8")).hexdigest()


def hash_snapshot_file(path: Path) -> str:
    """Hash a saved archive or scan without loading the entire file into memory.

    Args:
        path: Existing regular evidence file.

    Returns:
        SHA-256 digest of all file bytes.

    Raises:
        OSError: If the file cannot be read.
    """

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_snapshot_path(output: Path, relative_path: str) -> Path:
    """Resolve an evidence path without allowing traversal or symbolic links.

    Args:
        output: Search output directory containing the saved evidence.
        relative_path: Forward-slash relative path from a saved manifest.

    Returns:
        Verified path strictly below the output directory.

    Raises:
        ValueError: If a path is absolute, unsafe, or crosses a symbolic link.
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
    """Freeze discovery selection and snapshot limits while retaining all datasets.

    Args:
        arguments: Search command options; omitted persistent options retain saved values.
        catalog: Validated full catalog with summary and products.
        saved: Existing GitHub plan, or None for a new run.

    Returns:
        New plan or the exactly compatible saved plan. Request count is per
        invocation and intentionally absent from persistent configuration.

    Raises:
        ValueError: If selection, limits, backend, or frozen catalog differ.
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
    """Locate one uniquely numbered discovery attempt without using terms as paths.

    Args:
        output: Search output directory.
        provider: GitHub or Grep provider label.
        term: Literal dataset short name.
        page: One-based provider page number.
        attempt: One-based request-attempt number.

    Returns:
        Deterministic JSON path under discovery/.
    """

    identity = hash_json_value([provider, term, page])
    return output / "discovery" / f"{identity}-attempt-{attempt:06d}.json"


def save_discovery_receipt(path: Path, receipt: dict) -> None:
    """Save a request receipt with a checksum before or after its network call.

    Args:
        path: Numbered attempt path owned by this run.
        receipt: Request metadata and raw public-only provider response.

    Raises:
        OSError: If the checkpoint cannot be published.
    """

    payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    save_github_json(path, {**payload, "receipt_sha256": hash_json_value(payload)})


def read_discovery_receipts(output: Path, plan: dict) -> list[dict]:
    """Validate every saved discovery attempt before considering more requests.

    Args:
        output: Search output containing immutable numbered attempts.
        plan: Frozen discovery terms and enabled providers.

    Returns:
        Receipts sorted by term, provider, page, and attempt; older failed
        responses remain available alongside later explicit retries.

    Raises:
        OSError: If receipt files cannot be read.
        ValueError: If evidence is corrupt, renamed, or belongs to another plan.
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
    """Select the latest saved attempt for each provider, term, and page.

    Args:
        receipts: Validated attempts including historical failures.

    Returns:
        Mapping from request identity to its highest numbered attempt.
    """

    latest = {}
    for receipt in receipts:
        identity = (receipt["provider"], receipt["term"], receipt["page"])
        if identity not in latest or receipt["attempt"] > latest[identity]["attempt"]:
            latest[identity] = receipt
    return latest


def collect_github_discovery(output: Path, plan: dict, receipts: list[dict], maximum_requests: int,
                             retry_incomplete: bool, ca_bundle: str | None) -> int:
    """Request missing pages round-robin, checkpointing each call independently.

    Args:
        output: Search output directory held by the caller's exclusive lock.
        plan: Frozen discovery selection and page limits.
        receipts: Validated mutable receipt list, appended after each response.
        maximum_requests: Maximum discovery calls during this invocation.
        retry_incomplete: Explicit permission for one additional failed attempt
            per request; successful earlier pages are never fetched again.
        ca_bundle: Optional certificate bundle for Grep HTTP requests.

    Returns:
        Number of discovery calls made in this invocation.

    Raises:
        OSError: If checkpoints cannot be saved.
        KeyboardInterrupt: If interrupted; the running receipt remains durable.
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
    """Choose a stable, round-robin repository sample without injecting controls.

    Args:
        receipts: All saved public discovery responses, including partial results.
        repositories: Existing manifest records whose selected slots are retained.
        plan: Frozen discovery order and repository cap.

    Returns:
        Existing and newly discovered repositories with provenance and explicit
        selected or repository-cap statuses.
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
    """Verify all cached evidence before permitting requests or report writes.

    Args:
        output: Search output containing referenced ZIP and JSON files.
        repositories: Saved snapshot manifest rows.
        plan: Frozen maximum selected-repository count.

    Raises:
        OSError: If referenced cached evidence cannot be read.
        ValueError: If manifest identities, statuses, paths, or hashes are invalid.
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


def download_github_snapshot(repository: str, commit: str, destination: Path, maximum_bytes: int,
                             ca_bundle: str | None = None) -> int:
    """Download a fixed public archive without credentials, redirects, or extraction.

    Args:
        repository: Repository whose public visibility was verified before pinning.
        commit: Full immutable Git commit SHA.
        destination: New ZIP path; existing archives are never replaced.
        maximum_bytes: Remaining compressed-byte budget for this download.
        ca_bundle: Optional certificate authority bundle for the HTTPS request.

    Returns:
        Number of compressed bytes saved. Failed partial bytes are preserved and
        counted against the aggregate budget on subsequent invocations.

    Raises:
        OSError: If network, TLS, file operations, or the socket timeout fail.
        ValueError: If names are unsafe or byte/time limits are exceeded.
    """

    repository_name = validate_repository_name(repository)
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or maximum_bytes < 1:
        raise ValueError("A pinned commit and positive remaining byte budget are required.")
    if destination.exists():
        raise ValueError("Snapshot destination already exists; refusing to replace it.")
    request = Request(f"https://codeload.github.com/{repository_name}/zip/{commit}",
                      headers={"User-Agent": "nasa-eo-github-search/0.1"})
    opener = build_opener(RejectEvidenceRedirects(), HTTPSHandler(context=build_tls_context(ca_bundle)))
    partial = destination.with_suffix(".partial")
    started, downloaded = time.monotonic(), 0
    with opener.open(request, timeout=20) as response:
        declared_size = response.headers.get("Content-Length")
        if declared_size and int(declared_size) > maximum_bytes:
            raise ValueError("Archive exceeds the compressed download budget.")
        with partial.open("xb") as target:
            while True:
                if time.monotonic() - started > 90:
                    raise ValueError("Archive exceeded the 90-second download budget.")
                chunk = response.read(min(65536, maximum_bytes - downloaded + 1))
                if not chunk:
                    break
                target.write(chunk)
                downloaded += len(chunk)
                if downloaded > maximum_bytes:
                    raise ValueError("Archive exceeds the compressed download budget; partial bytes retained.")
    partial.replace(destination)
    return downloaded


def scan_selected_snapshots(output: Path, plan: dict, catalog: dict, repositories: list[dict],
                             retry_incomplete: bool, ca_bundle: str | None) -> None:
    """Download each selected fixed snapshot once and scan all catalog signatures.

    Args:
        output: Locked search output directory.
        plan: Frozen repository and compressed-download budgets.
        catalog: Full saved catalog, not merely the selected discovery products.
        repositories: Mutable snapshot records checkpointed after each stage.
        retry_incomplete: Whether failed network attempts may be retried explicitly.
        ca_bundle: Optional certificate bundle for archive downloads.

    Raises:
        OSError: If durable manifests cannot be published.
        KeyboardInterrupt: If interrupted; pinned/downloaded states remain saved.
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
                archive_name = f"{hash_json_value(record['repository'].lower())}-attempt-{record['download_attempts']:06d}.zip"
                archive_path = safe_snapshot_path(output, f"snapshots/{archive_name}")
                record["status"] = "downloading"
                save_github_json(output / "snapshots.json", {"schema_version": 1, "repositories": repositories})
                print(f"Downloading pinned snapshot {record['repository']}...", file=sys.stderr, flush=True)
                archive_bytes = download_github_snapshot(record["repository"], record["commit"], archive_path,
                                                         available_bytes, ca_bundle)
                record.update({"status": "downloaded", "archive_relative_path": f"snapshots/{archive_name}",
                               "archive_sha256": hash_snapshot_file(archive_path), "error": ""})
                record["metrics"]["archive_bytes"] = archive_bytes
                save_github_json(output / "snapshots.json", {"schema_version": 1, "repositories": repositories})
            archive_path = safe_snapshot_path(output, record["archive_relative_path"])
            started = time.monotonic()
            scan = scan_repository_archive(archive_path, terms)
            scan_path = archive_path.with_suffix(".scan.json")
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
    """Describe page progress and limits separately from global search coverage.

    Args:
        plan: Frozen discovery terms, providers, and page cap.
        receipts: Saved attempts, including partial results and failures.

    Returns:
        One report row per term/provider with explicit operational status.
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
    """Publish current tables and bounded local evidence from durable receipts.

    Args:
        output: Search output directory.
        plan: Frozen discovery and snapshot plan.
        catalog: Saved full catalog.
        receipts: All saved discovery attempts.
        repositories: Current snapshot records.
        requests_this_run: Count of calls made in the current invocation.
        forced_status: Running or interrupted override while work is ongoing.

    Returns:
        Saved summary whose status concerns execution, never all-GitHub coverage.

    Raises:
        OSError: If reports cannot be published.
        ValueError: If cached evidence fails the shared classification validator.
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
    """Search bounded GitHub pages, resume saved snapshots, and write file tables.

    Inputs are a finished NASA catalog, output directory, optional discovery short
    names, and explicit request/download limits. The full catalog is scanned in
    each selected immutable public snapshot without executing downloaded code.

    Args:
        arguments: Search CLI namespace. Missing persistent options use saved
            values or new-run defaults; max_requests defaults to ten per call.

    Returns:
        Zero for a dry run or operationally complete bounded run, four for
        limits/pending work, one for failures, or 130 after interruption.

    Raises:
        OSError: If catalog or evidence files cannot be accessed.
        ValueError: If inputs differ, limits are invalid, or cached evidence is
            damaged. Existing damaged evidence is never silently replaced.
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

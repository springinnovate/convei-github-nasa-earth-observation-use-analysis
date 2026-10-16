"""Compare bounded code discovery, pin public snapshots, and scan a saved catalog."""

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
from urllib.parse import quote
from urllib.request import HTTPSHandler, Request, build_opener

from nasa_eo_search.batch_files import atomic_batch_output
from nasa_eo_search.batch_reports import write_protected_csv_rows
from nasa_eo_search.evidence_context import RejectEvidenceRedirects
from nasa_eo_search.pipeline_catalog import read_pipeline_catalog
from nasa_eo_search.pipeline_tables import FILE_MATCH_COLUMNS, build_file_match_row
from nasa_eo_search.repo_preview import build_sourcegraph_product_query
from nasa_eo_search.repo_search import collect_repository_query
from nasa_eo_search.sourcegraph import build_tls_context
from nasa_eo_search.trial_discovery import (
    resolve_public_repository_commit, search_github_code, search_grep_app,
    validate_repository_name, validate_search_term,
)
from nasa_eo_search.trial_scan import MATCHING_RULE, scan_repository_archive


KNOWN_CONTROLS = {"ATL03": ["github.com/pydap/pydap"],
                  "ARIA_S1_GUNW": ["github.com/ASFHyP3/hyp3"]}
DISCOVERY_COLUMNS = ("provider", "term", "status", "repository", "file", "url")
COMPARISON_COLUMNS = (
    "provider", "term", "status", "candidate_files", "repositories",
    "repositories_exclusive_to_provider", "known_controls", "controls_discovered",
    "controls_missing", "elapsed_seconds", "warnings",
)
REPOSITORY_COLUMNS = (
    "repository", "discovered_by", "control_for", "control_terms_found", "control_terms_missing",
    "commit", "status", "archive_bytes",
    "files_scanned", "bytes_scanned", "skipped_files", "scan_seconds", "error",
)


def save_trial_json(path: Path, value: Any) -> None:
    """Atomically save a trial-owned JSON receipt without spreadsheet escaping.

    Args:
        path: File inside the new trial directory.
        value: JSON-serializable evidence or metadata.

    Raises:
        OSError: If publishing the receipt fails.
    """
    with atomic_batch_output(path) as output:
        json.dump(value, output, indent=2, ensure_ascii=True)


def search_sourcegraph_trial(term: str, output_directory: Path) -> dict[str, Any]:
    """Run one broad, bounded Sourcegraph query with its original evidence saved.

    Args:
        term: Literal dataset short name shared with the other search providers.
        output_directory: New directory for the Sourcegraph stream receipts.

    Returns:
        Normalized candidate locations, elapsed time, and explicit search limits.

    Raises:
        OSError: If saved results cannot be read.
        KeyboardInterrupt: If the user interrupts the underlying collector.
    """
    query = build_sourcegraph_product_query(term, False, collect_all=True).replace(
        "count:all timeout:60s", "count:100 timeout:10s")
    exit_status = collect_repository_query(query, output_directory, None, timeout_seconds=20)
    if exit_status == 130:
        raise KeyboardInterrupt
    summary_path = output_directory / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    results = []
    matches_path = output_directory / "matches.jsonl"
    if matches_path.exists():
        for line in matches_path.read_text(encoding="utf-8").splitlines():
            match = json.loads(line)["match"]
            if match.get("type") != "content" or not isinstance(match.get("path"), str):
                continue
            try:
                repository_name = validate_repository_name(match.get("repository", ""))
            except (ValueError, AttributeError):
                continue
            commit = str(match.get("commit", ""))
            results.append({"repository": f"github.com/{repository_name}", "file": match["path"],
                            "url": f"https://github.com/{repository_name}/blob/{quote(commit, safe='')}/{quote(match['path'])}"})
    warnings = [json.dumps(warning, ensure_ascii=True) for warning in summary.get("warnings", [])]
    warnings.append("Broad public GitHub query; 100-match target and 10-second server timeout. Global and repository-scoped index coverage can differ.")
    if summary.get("error"):
        warnings.append(summary["error"])
    return {"provider": "sourcegraph", "term": term, "query": query,
            "status": summary.get("status", "error"), "results": results,
            "elapsed_seconds": summary.get("elapsed_seconds", 0), "warnings": warnings,
            "raw": summary}


def choose_trial_repositories(receipts: list[dict], short_names: list[str], maximum: int) -> list[dict]:
    """Prioritize independent controls, then alternate across discovery queries.

    Args:
        receipts: Provider responses containing public candidate repositories.
        short_names: Selected products used to choose applicable known controls.
        maximum: Maximum repositories to download, including controls.

    Returns:
        All discovered/control repositories with provenance and selected status.
        Controls count as discovered only if an actual provider returned them.
    """
    repositories: dict[str, dict] = {}
    controls = [(term, repository) for term in short_names for repository in KNOWN_CONTROLS.get(term, [])]
    ordered_candidates = [repository for _, repository in controls]
    candidate_lists = [list(dict.fromkeys(item["repository"] for item in receipt["results"])) for receipt in receipts]
    for rank in range(max((len(items) for items in candidate_lists), default=0)):
        ordered_candidates.extend(items[rank] for items in candidate_lists if rank < len(items))
    for repository in ordered_candidates:
        repositories.setdefault(repository.lower(), {"repository": repository, "discovered_by": [], "control_for": []})
    for term, repository in controls:
        repositories[repository.lower()]["control_for"].append(term)
    for receipt in receipts:
        for repository in {item["repository"].lower() for item in receipt["results"]}:
            label = f"{receipt['provider']}:{receipt['term']}"
            if label not in repositories[repository]["discovered_by"]:
                repositories[repository]["discovered_by"].append(label)
    for position, row in enumerate(repositories.values()):
        row["status"] = "pending" if position < maximum else "not_selected_repository_cap"
    return list(repositories.values())


def download_public_archive(repository: str, commit: str, destination: Path, maximum_bytes: int) -> int:
    """Download a fixed public ZIP without credentials, redirects, or extraction.

    Args:
        repository: Public GitHub repository whose visibility was just checked.
        commit: Full immutable commit identifier.
        destination: New archive path in the trial directory.
        maximum_bytes: Maximum compressed bytes to retain, allowing one overflow
            detection byte. Partial downloads remain in a .partial file.

    Returns:
        Size of the successfully downloaded ZIP in bytes.

    Raises:
        OSError: If HTTP, TLS, file access, or the read timeout fails.
        ValueError: If identifiers are invalid or a size/time limit is reached.
    """
    repository_name = validate_repository_name(repository)
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or maximum_bytes < 1:
        raise ValueError("An immutable commit and positive download limit are required.")
    request = Request(f"https://codeload.github.com/{repository_name}/zip/{commit}",
                      headers={"User-Agent": "nasa-eo-discovery-trial/0.1"})
    opener = build_opener(RejectEvidenceRedirects(), HTTPSHandler(context=build_tls_context(None)))
    started_at = time.monotonic()
    downloaded_bytes = 0
    partial_path = destination.with_suffix(".partial")
    with opener.open(request, timeout=20) as response:
        declared_length = response.headers.get("Content-Length")
        if declared_length and int(declared_length) > maximum_bytes:
            raise ValueError("Archive exceeds the compressed download budget.")
        with partial_path.open("xb") as output:
            while True:
                if time.monotonic() - started_at > 90:
                    raise ValueError("Archive download exceeded the 90-second elapsed budget.")
                chunk = response.read(min(65536, maximum_bytes - downloaded_bytes + 1))
                if not chunk:
                    break
                output.write(chunk)
                downloaded_bytes += len(chunk)
                if downloaded_bytes > maximum_bytes:
                    raise ValueError("Archive exceeds the compressed download budget; partial bytes retained.")
    partial_path.replace(destination)
    return downloaded_bytes


def build_snapshot_match_rows(repository: str, commit: str, scan: dict, products_by_term: dict) -> list[dict]:
    """Associate local literal matches with every catalog product sharing a term.

    Args:
        repository: Repository containing the scanned snapshot.
        commit: Exact downloaded revision.
        scan: Local scan results and exclusion records.
        products_by_term: Original candidate terms mapped to catalog products.

    Returns:
        One row per product/file/revision, combining matching terms and lines.
        Global search completeness stays false; local scan status is separate.
    """
    grouped_files: dict[tuple, dict] = {}
    for match in scan["matches"]:
        for product in products_by_term[match["term"]]:
            identity = (product["provider"], product["short_name"], match["file"])
            record = grouped_files.setdefault(identity, {"product": product, "file": match["file"],
                                                        "matching_lines": [], "sources": []})
            record["matching_lines"].extend(match["matching_lines"])
            record["sources"].append({"term": match["term"]})
    result = []
    for _, record in sorted(grouped_files.items()):
        record["commit"] = commit
        record["file_url"] = f"https://{repository}/blob/{commit}/{quote(record['file'])}"
        warnings = ["Candidate reference, not verified data use. Bounded discovery is not complete GitHub coverage."]
        if scan["skipped"]:
            warnings.append(f"Snapshot has {len(scan['skipped'])} exclusions; see skipped-files.csv.")
        row = build_file_match_row({"product": record["product"], "repository": repository,
                                   "search_complete": False, "warnings": warnings}, record)
        row["snapshot_scan_status"] = scan["status"]
        result.append(row)
    return result


def write_trial_reports(output_directory: Path, receipts: list[dict], repositories: list[dict],
                        match_rows: list[dict], skipped_rows: list[dict], summary: dict) -> None:
    """Publish readable tables after each stage while preserving raw receipts.

    Args:
        output_directory: Trial-owned directory.
        receipts: Completed provider requests, including failures and zero hits.
        repositories: Discovered/control repository metadata and scan outcomes.
        match_rows: Product-associated local file matches collected so far.
        skipped_rows: Snapshot files excluded from scanning with explicit reasons.
        summary: Current operational status, limits, and elapsed time.

    Raises:
        OSError: If a report cannot be published.
    """
    comparison_rows = []
    discovery_rows = []
    for receipt in receipts:
        found = {item["repository"].lower() for item in receipt["results"]}
        other_found = {item["repository"].lower() for other in receipts
                       if other["provider"] != receipt["provider"] and other["term"] == receipt["term"]
                       for item in other["results"]}
        controls = {repository.lower() for repository in KNOWN_CONTROLS.get(receipt["term"], [])}
        comparison_rows.append({
            "provider": receipt["provider"], "term": receipt["term"], "status": receipt["status"],
            "candidate_files": len({(item['repository'].lower(), item['file']) for item in receipt["results"]}),
            "repositories": len(found), "repositories_exclusive_to_provider": len(found - other_found),
            "known_controls": "; ".join(sorted(controls)), "controls_discovered": "; ".join(sorted(controls & found)),
            "controls_missing": "; ".join(sorted(controls - found)),
            "elapsed_seconds": receipt["elapsed_seconds"], "warnings": "\n".join(receipt["warnings"]),
        })
        discovery_rows.extend({"provider": receipt["provider"], "term": receipt["term"],
                               "status": receipt["status"], **item} for item in receipt["results"])
    repository_rows = [{**row, "discovered_by": "; ".join(row["discovered_by"]),
                        "control_for": "; ".join(row["control_for"])} for row in repositories]
    write_protected_csv_rows(output_directory / "comparison.csv", COMPARISON_COLUMNS, comparison_rows)
    write_protected_csv_rows(output_directory / "discovery.csv", DISCOVERY_COLUMNS, discovery_rows)
    write_protected_csv_rows(output_directory / "repositories.csv", REPOSITORY_COLUMNS, repository_rows)
    write_protected_csv_rows(output_directory / "matches.csv", (*FILE_MATCH_COLUMNS, "snapshot_scan_status"), match_rows)
    write_protected_csv_rows(output_directory / "skipped-files.csv", ("repository", "commit", "file", "reason", "detail"), skipped_rows)
    save_trial_json(output_directory / "summary.json", {
        **summary, "queries_attempted": len(receipts), "candidate_repositories": len(repositories),
        "repositories_scanned": sum(row["status"] in {"complete", "incomplete"} for row in repositories),
        "match_rows": len(match_rows), "skipped_files": len(skipped_rows),
        "stored_bytes_before_summary": sum(path.stat().st_size for path in output_directory.rglob("*") if path.is_file()),
    })


def run_discovery_trial(arguments: argparse.Namespace) -> int:
    """Discover a bounded repository sample, scan fixed snapshots, and compare.

    Args:
        arguments: Validated command options from the argument parser.

    Returns:
        Zero for a finished bounded trial, one for service/download failures,
        or 130 for interruption. Scope exclusions do not mean execution failed.

    Raises:
        OSError: If catalog or report files cannot be accessed.
        ValueError: If catalog terms or selection are invalid.
    """
    catalog_summary, catalog_products = read_pipeline_catalog(arguments.catalog)
    short_names = list(dict.fromkeys(arguments.short_name))
    if len(short_names) > 10:
        raise ValueError("Choose at most ten discovery short names per comparison trial.")
    missing = set(short_names) - {product["short_name"] for product in catalog_products}
    if missing:
        raise ValueError("Unknown catalog short names: " + ", ".join(sorted(missing)))
    for term in short_names:
        validate_search_term(term)
    products = [product for product in catalog_products
                if not arguments.selected_only or product["short_name"] in short_names]
    products_by_term: dict[str, list[dict]] = {}
    for product in products:
        for candidate in product["candidate_signatures"]:
            term = candidate["term"]
            if not isinstance(term, str) or not term.strip() or any(character in term for character in "\r\n\x00"):
                raise ValueError("Catalog contains an invalid local search term.")
            products_by_term.setdefault(term, []).append(product)
    if not products_by_term:
        raise ValueError("No local search terms were selected.")
    providers = list(dict.fromkeys(arguments.providers))
    print(f"Discovery: {len(short_names)} terms x {len(providers)} providers. "
          f"Local scan: {len(products)} datasets / {len(products_by_term)} terms; "
          f"at most {arguments.max_repositories} snapshots.", flush=True)
    if arguments.dry_run:
        return 0
    output_directory = arguments.output.resolve()
    output_directory.mkdir(parents=True, exist_ok=False)
    (output_directory / "receipts").mkdir()
    (output_directory / "snapshots").mkdir()
    save_trial_json(output_directory / "catalog.json", {"summary": catalog_summary, "products": products})
    started_at = time.monotonic()
    summary = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
               "short_names": short_names, "providers": providers, "local_product_count": len(products),
               "local_term_count": len(products_by_term), "matching_rule": MATCHING_RULE,
               "scope": "Bounded public-code discovery and current default-branch snapshots; no history, all-GitHub coverage, or verified use claim.",
               "limits": {"max_repositories": arguments.max_repositories,
                          "max_archive_mb": arguments.max_archive_mb, "max_total_mb": arguments.max_total_mb,
                          "github_pages_per_term": 1, "grep_pagination": "unavailable",
                          "sourcegraph_count": 100, "sourcegraph_timeout_seconds": 10}}
    receipts: list[dict] = []
    repositories: list[dict] = []
    match_rows: list[dict] = []
    skipped_rows: list[dict] = []
    exit_status = 0
    try:
        write_trial_reports(output_directory, receipts, repositories, match_rows, skipped_rows, summary)
        for term in short_names:
            for provider in providers:
                print(f"Discovering {term!r} with {provider}...", flush=True)
                receipt_path = output_directory / "receipts" / f"query-{len(receipts) + 1:03d}"
                if provider == "github":
                    receipt = search_github_code(term)
                elif provider == "grep.app":
                    receipt = search_grep_app(term)
                else:
                    receipt = search_sourcegraph_trial(term, receipt_path)
                receipts.append(receipt)
                save_trial_json(receipt_path.with_suffix(".json"), receipt)
                print(f"  {receipt['status']}: {len(receipt['results'])} candidate files.", flush=True)
                if receipt["status"] in {"error", "failed"}:
                    exit_status = 1
                write_trial_reports(output_directory, receipts, repositories, match_rows, skipped_rows, summary)
        repositories = choose_trial_repositories(receipts, short_names, arguments.max_repositories)
        for position, repository in enumerate(repositories):
            if repository["status"] != "pending":
                continue
            archive_path = output_directory / "snapshots" / f"repository-{position + 1:03d}.zip"
            downloaded_bytes = sum(path.stat().st_size for path in archive_path.parent.iterdir()
                                   if path.is_file() and path.suffix in {".zip", ".partial"})
            remaining_bytes = arguments.max_total_mb * 1_000_000 - downloaded_bytes
            if remaining_bytes <= 0:
                repository["status"] = "not_downloaded_total_byte_cap"
                continue
            print(f"Pinning, downloading, and scanning {repository['repository']}...", flush=True)
            try:
                repository["commit"] = resolve_public_repository_commit(repository["repository"])
                save_trial_json(archive_path.with_suffix(".json"), repository)
                repository["archive_bytes"] = download_public_archive(
                    repository["repository"], repository["commit"], archive_path,
                    min(arguments.max_archive_mb * 1_000_000, remaining_bytes))
                archive_digest = hashlib.sha256()
                with archive_path.open("rb") as archive_input:
                    for chunk in iter(lambda: archive_input.read(65536), b""):
                        archive_digest.update(chunk)
                repository["archive_sha256"] = archive_digest.hexdigest()
                scan_started = time.monotonic()
                scan = scan_repository_archive(archive_path, list(products_by_term))
                found_terms = {match["term"] for match in scan["matches"]}
                repository.update({"status": scan["status"], "files_scanned": scan["files_scanned"],
                                   "bytes_scanned": scan["bytes_scanned"], "skipped_files": len(scan["skipped"]),
                                   "control_terms_found": "; ".join(sorted(set(repository["control_for"]) & found_terms)),
                                   "control_terms_missing": "; ".join(sorted(set(repository["control_for"]) - found_terms)),
                                   "scan_seconds": round(time.monotonic() - scan_started, 3)})
                save_trial_json(archive_path.with_suffix(".scan.json"), scan)
                match_rows.extend(build_snapshot_match_rows(repository["repository"], repository["commit"], scan, products_by_term))
                skipped_rows.extend({"repository": repository["repository"], "commit": repository["commit"], **item}
                                    for item in scan["skipped"])
                if any(item["reason"] in {"invalid_archive", "archive_member_read_error"} for item in scan["skipped"]):
                    repository.update({"status": "error", "error": "Invalid or damaged archive; inspect skipped-files.csv."})
                    exit_status = 1
                print(f"  Scanned {scan['files_scanned']} files; {len(scan['skipped'])} exclusions.", flush=True)
            except (OSError, ValueError, RuntimeError) as error:
                repository.update({"status": "error", "error": str(error)})
                retained_archive = archive_path if archive_path.exists() else archive_path.with_suffix(".partial")
                repository["archive_bytes"] = retained_archive.stat().st_size if retained_archive.exists() else 0
                exit_status = 1
                print(f"  Snapshot unavailable: {error}", file=sys.stderr, flush=True)
            save_trial_json(archive_path.with_suffix(".json"), repository)
            write_trial_reports(output_directory, receipts, repositories, match_rows, skipped_rows, summary)
        summary["status"] = "finished_with_errors" if exit_status else "finished_bounded_trial"
    except KeyboardInterrupt:
        summary["status"] = "interrupted"
        exit_status = 130
    except (OSError, ValueError, RuntimeError) as error:
        summary.update({"status": "failed", "error": str(error)})
        exit_status = 1
    finally:
        summary["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
        write_trial_reports(output_directory, receipts, repositories, match_rows, skipped_rows, summary)
    print(f"{summary['status']}: {len(match_rows)} candidate file rows. Read {output_directory / 'comparison.csv'}", flush=True)
    return exit_status


def main(argument_values: list[str] | None = None) -> int:
    """Run a small provider comparison and local scan from command-line options.

    Args:
        argument_values: Optional arguments; defaults to the process command line.

    Returns:
        Zero for a completed bounded trial or dry run, one for errors, or 130
        for interruption. Argument syntax errors exit through argparse with two.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path, help="existing saved NASA catalog directory")
    parser.add_argument("--short-name", action="append", required=True, help="discovery product; repeat up to ten times")
    parser.add_argument("--output", type=Path, required=True, help="new trial directory; existing directories are never replaced")
    parser.add_argument("--providers", nargs="+", choices=("github", "grep.app", "sourcegraph"),
                        default=["github", "grep.app", "sourcegraph"])
    parser.add_argument("--max-repositories", type=int, default=6, help="snapshots including controls (default: 6)")
    parser.add_argument("--max-archive-mb", type=int, default=25, help="compressed bytes per snapshot in decimal MB")
    parser.add_argument("--max-total-mb", type=int, default=150, help="total retained archive download budget in decimal MB")
    parser.add_argument("--selected-only", action="store_true", help="locally scan only selected datasets instead of the full saved catalog")
    parser.add_argument("--dry-run", action="store_true", help="validate inputs and show bounds without requests or writes")
    arguments = parser.parse_args(argument_values)
    if min(arguments.max_repositories, arguments.max_archive_mb, arguments.max_total_mb) < 1:
        parser.error("Repository and download limits must be positive.")
    try:
        return run_discovery_trial(arguments)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"discovery-trial: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

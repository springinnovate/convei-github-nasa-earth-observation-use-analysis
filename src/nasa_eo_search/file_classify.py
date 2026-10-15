"""Classify saved dataset/repository/file matches without a separate prepare step."""

import argparse
import copy
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

from nasa_eo_search.batch_files import COMPLETED_SEARCH, lock_batch_directory
from nasa_eo_search.batch_plan import validate_saved_batch_plan
from nasa_eo_search.evidence_classify import run_prepared_classifications, select_classification_settings
from nasa_eo_search.evidence_context import build_evidence_packets, enrich_evidence_group
from nasa_eo_search.evidence_inputs import identify_evidence, read_evidence_json, read_product_evidence_groups, serialize_evidence
from nasa_eo_search.evidence_model import CLASSIFICATION_PROMPT, PROMPT_VERSION, build_classification_request
from nasa_eo_search.evidence_reports import load_evidence_plan, save_evidence_json, write_evidence_reports
from nasa_eo_search.pipeline_tables import build_file_match_row
from nasa_eo_search.sourcegraph import build_tls_context


def resolve_file_classification_catalog(batch_directory: Path, catalog_directory: Path | None) -> Path:
    """Find the original saved catalog unless the caller supplies a replacement path.

    Args:
        batch_directory: Saved repository-search output directory.
        catalog_directory: Explicit catalog path, or None to use the saved path.

    Returns:
        Absolute catalog directory for source validation and product descriptions.

    Raises:
        OSError: If the batch plan cannot be read.
        ValueError: If the plan or saved catalog path is malformed.
    """
    batch_plan = read_evidence_json(batch_directory / "plan.json")
    validate_saved_batch_plan(batch_plan)
    if catalog_directory is not None:
        return catalog_directory.resolve()
    saved_catalog = batch_plan.get("catalog_directory")
    if not isinstance(saved_catalog, str) or not saved_catalog.strip():
        raise ValueError("The search plan has no catalog path; supply --catalog with the saved catalog directory.")
    return Path(saved_catalog).resolve()


def split_repository_groups_into_files(source_plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep every dataset/file/revision match independent during classification.

    Preserve the original search table row before adding context. File-specific
    provenance warnings are recalculated so another matching file cannot affect
    this assessment. Product-search and source-catalog coverage still apply.

    Args:
        source_plan: Unenriched repository/product groups from saved search data.

    Returns:
        Independent groups, each containing exactly one matched file and revision.
    """
    file_groups = []
    file_provenance_warnings = {
        "Some evidence appears only in historical search attempts.",
        "Some evidence came from partial or damaged search attempts; inspect provenance.",
        "Evidence spans multiple commits; it is not a single repository snapshot.",
    }
    for repository_group in source_plan["groups"]:
        for file_record in repository_group["files"]:
            match_row = build_file_match_row(repository_group, file_record)
            file_group = copy.deepcopy({field: value for field, value in repository_group.items() if field != "files"})
            file_group["files"] = [copy.deepcopy(file_record)]
            file_group["group_id"] = match_row["match_id"]
            file_group["match_row"] = match_row
            file_group["warnings"] = [warning for warning in repository_group["warnings"] if warning not in file_provenance_warnings]
            if not any(source["latest_attempt"] for source in file_record["sources"]):
                file_group["warnings"].append("Some evidence appears only in historical search attempts.")
            if any(source["status"] != COMPLETED_SEARCH for source in file_record["sources"]):
                file_group["warnings"].append("Some evidence came from partial or damaged search attempts; inspect provenance.")
            file_groups.append(file_group)
    return file_groups


def identify_file_classification_source(source_plan: dict[str, Any]) -> str:
    """Fingerprint evidence and coverage independently of report regeneration or location.

    Match text, file revisions, product metadata, source-query status, and relevant
    catalog coverage remain part of the identity. Generated batch summaries,
    elapsed times, timestamps, and local directory paths do not. Rewriting reports
    or relocating an unchanged catalog therefore does not invalidate paid receipts.

    Args:
        source_plan: Unenriched saved-search groups and source metadata.

    Returns:
        Stable semantic source hash used to validate file-classification resumes.
    """
    catalog_coverage_fields = (
        "schema_version", "status", "source_url", "scope", "short_name_filter", "grouping",
        "products_saved", "unique_collections", "reached_end", "warnings", "error",
    )
    catalog_summary = source_plan["catalog_summary"]
    return identify_evidence({
        "source_identity_version": 1, "groups": source_plan["groups"],
        "catalog_status": source_plan["catalog_status"], "search_scope": source_plan["search_scope"],
        "selected_products": source_plan["selected_products"],
        "source_product_queries": source_plan["source_product_queries"],
        "catalog_coverage": {field: catalog_summary[field] for field in catalog_coverage_fields if field in catalog_summary},
    })


def build_file_classification_plan(
    source_plan: dict[str, Any], offline: bool, context_lines: int,
    packet_bytes: int, ca_bundle: str | None,
) -> dict[str, Any]:
    """Prepare one independently scoped evidence assessment per saved file match.

    Reuse the existing context collection and bounded packet packaging. The
    download cache avoids fetching shared README backgrounds repeatedly, but no
    matching code from another file is supplied as classification evidence.

    Args:
        source_plan: Original saved-search groups and catalog metadata.
        offline: Use saved matching lines without fetching public context.
        context_lines: Number of surrounding lines to collect for each match.
        packet_bytes: Maximum serialized bytes of each complete model packet.
        ca_bundle: Optional certificate authority bundle for public downloads.

    Returns:
        New file-level plan with stable source identity and preserved match rows.
        Groups that cannot be packaged remain explicit blocked assessments.

    Raises:
        SourcegraphError: If verified transport configuration is invalid.
    """
    if not offline:
        build_tls_context(ca_bundle)
    plan = {
        **copy.deepcopy({field: value for field, value in source_plan.items() if field != "groups"}),
        "groups": split_repository_groups_into_files(source_plan),
        "assessment_unit": "file", "source_evidence_id": identify_file_classification_source(source_plan),
        "prompt_version": PROMPT_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
        "offline": offline, "context_lines": context_lines, "packet_bytes": packet_bytes,
    }
    download_cache: dict[str, Any] = {}
    for number, group in enumerate(plan["groups"], start=1):
        file_record = group["files"][0]
        print(f"[{number}/{len(plan['groups'])}] Gather {group['repository']} / {group['product']['short_name']} / "
              f"{file_record['file']} at {file_record['commit'] or 'unknown revision'}.", file=sys.stderr, flush=True)
        enrich_evidence_group(group, download_cache, offline, context_lines, ca_bundle)
        file_scope = {
            "assessment_unit": "file", "assessment_file": file_record["file"],
            "assessment_commit": file_record["commit"],
        }
        # Extra members add one separating comma but no extra object braces.
        scope_bytes = len(serialize_evidence(file_scope).encode("utf-8")) - 1
        try:
            packets = build_evidence_packets(group, packet_bytes - scope_bytes)
            group["packets"] = [{**packet, **file_scope} for packet in packets]
        except ValueError as error:
            group["packets"] = []
            group["preparation_error"] = str(error)
    return plan


def preview_file_classification_plan(plan: dict[str, Any], arguments: argparse.Namespace) -> int:
    """Describe an unsaved file plan without credentials, output writes, or API calls.

    Args:
        plan: In-memory plan prepared from saved matching lines only.
        arguments: Model settings and optional request limit.

    Returns:
        Zero after printing the offline estimate and its context-size limitation.

    Raises:
        ValueError: If the requested model or generated-token limit is invalid.
    """
    settings = select_classification_settings(arguments.output.resolve(), arguments.model, arguments.max_output_tokens)
    packets = [packet for group in plan["groups"] for packet in group["packets"]]
    selected_packets = packets[:arguments.max_requests]
    request_bytes = sum(len(serialize_evidence(build_classification_request(
        packet, settings["model"], settings["max_output_tokens"],
    )).encode("utf-8")) for packet in selected_packets)
    blocked_files = sum(not group["packets"] for group in plan["groups"])
    print(f"Dry run: {len(plan['groups'])} file matches; {len(packets)} estimated requests; "
          f"at most {len(selected_packets)} new calls; {blocked_files} blocked files. "
          f"Estimated request JSON: {request_bytes} bytes. "
          "This estimate uses saved matching lines only; fetched context can change packet counts and size. "
          "No files written, keys read, public context fetched, or model calls made.", file=sys.stderr)
    return 0


def classify_file_matches(arguments: argparse.Namespace) -> int:
    """Classify or resume saved file matches and write an annotated match table.

    A new output is prepared automatically. Existing file-level outputs reuse
    their frozen context and successful receipts only when the saved search and
    catalog still match. Repository-level runs are never reinterpreted as file
    judgments. With no request cap, all eligible requests are selected; failed
    or uncertain requests still require explicit retry authorization.

    Args:
        arguments: Saved batch/catalog paths, output directory, model/credential
            options, context bounds, optional request cap, and dry-run/retry flags.

    Returns:
        Zero for completed processing or a dry run, four for unfinished/blocked
        matches, one for saved API failures, or 130 for interruption.

    Raises:
        OSError: If source files or output cannot be accessed.
        ValueError: If bounds, sources, or resumed output are incompatible.
        SourcegraphError: If verified transport configuration is invalid.
    """
    if arguments.packet_bytes < 4096 or not 0 <= arguments.context_lines <= 100:
        raise ValueError("--packet-bytes must be at least 4096; --context-lines must be 0 through 100.")
    if arguments.max_requests is not None and arguments.max_requests < 1:
        raise ValueError("--max-requests must be positive when supplied.")
    batch_directory = arguments.batch_directory.resolve()
    output_directory = arguments.output.resolve()
    catalog_directory = resolve_file_classification_catalog(batch_directory, arguments.catalog)
    source_plan = read_product_evidence_groups(batch_directory, catalog_directory)
    if read_evidence_json(batch_directory / "summary.json") != source_plan["source_batch_summary"]:
        raise ValueError("The saved search changed while reading it; finish or stop the search before classification.")
    if output_directory.exists():
        plan = load_evidence_plan(output_directory)
        if plan.get("assessment_unit") != "file":
            raise ValueError("This output contains repository-level assessments. Choose a new output directory for file classification.")
        if plan.get("source_evidence_id") != identify_file_classification_source(source_plan):
            raise ValueError("Saved search or catalog differs from this file-classification run. Choose a new output directory.")
        print("Reusing frozen file evidence; context preparation options apply only to a new output directory.", file=sys.stderr)
    else:
        select_classification_settings(output_directory, arguments.model, arguments.max_output_tokens)
        plan = build_file_classification_plan(
            source_plan, arguments.offline or arguments.dry_run, arguments.context_lines,
            arguments.packet_bytes, arguments.ca_bundle,
        )
        if arguments.dry_run:
            return preview_file_classification_plan(plan, arguments)
        output_directory.mkdir(parents=True, exist_ok=False)
        with lock_batch_directory(output_directory):
            save_evidence_json(output_directory / "plan.json", {
                "schema_version": 3, "plan_id": identify_evidence(plan), "data": plan,
            })
            save_evidence_json(output_directory / "prompt.json", {"version": PROMPT_VERSION, "instructions": CLASSIFICATION_PROMPT})
            write_evidence_reports(output_directory, plan, None)
    return run_prepared_classifications(arguments)

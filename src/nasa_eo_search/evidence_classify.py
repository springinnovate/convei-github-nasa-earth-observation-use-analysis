"""Prepare and classify saved NASA repository/product evidence."""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

from nasa_eo_search.batch_files import lock_batch_directory
from nasa_eo_search.evidence_context import build_evidence_packets, enrich_evidence_group
from nasa_eo_search.evidence_credentials import load_openai_api_key
from nasa_eo_search.evidence_inputs import identify_evidence, read_evidence_json, read_product_evidence_groups, serialize_evidence
from nasa_eo_search.evidence_model import (
    CLASSIFICATION_PROMPT, PROMPT_VERSION, build_classification_request,
    parse_classification_response, request_evidence_classification,
)
from nasa_eo_search.evidence_reports import (
    inspect_classification_receipts, load_evidence_plan, save_evidence_json, write_evidence_reports,
)
from nasa_eo_search.sourcegraph import SourcegraphError, build_tls_context


def prepare_classification_evidence(arguments: argparse.Namespace) -> int:
    """Freeze source evidence and preview repository/product assessments without OpenAI.

    Args:
        arguments: Parsed prepare options including source and output directories.

    Returns:
        Zero after preparation, or four if any group cannot fit or has no evidence.

    Raises:
        OSError: If inputs or output files cannot be accessed.
        ValueError: If inputs or bounds are invalid, or the output already exists.
        SourcegraphError: If the shared TLS configuration is invalid.
    """
    if arguments.packet_bytes < 4096 or not 0 <= arguments.context_lines <= 100:
        raise ValueError("--packet-bytes must be at least 4096; --context-lines must be 0 through 100.")
    output_directory = arguments.output.resolve()
    if output_directory.exists():
        raise ValueError("Preparation requires a new output directory.")
    if not arguments.offline:
        build_tls_context(arguments.ca_bundle)
    with lock_batch_directory(arguments.batch_directory):
        plan = read_product_evidence_groups(arguments.batch_directory, arguments.catalog)
    if any(search["status"] != "finished_no_reported_limits" for search in plan["source_product_queries"]):
        print("Warning: source searches include unfinished or damaged evidence; inspect source_product_queries in plan.json.", file=sys.stderr)
    plan.update({"prompt_version": PROMPT_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                 "offline": arguments.offline, "context_lines": arguments.context_lines,
                 "packet_bytes": arguments.packet_bytes})
    download_cache: dict[str, Any] = {}
    for number, group in enumerate(plan["groups"], start=1):
        print(f"[{number}/{len(plan['groups'])}] Prepare {group['repository']} / {group['product']['short_name']}.",
              file=sys.stderr, flush=True)
        enrich_evidence_group(group, download_cache, arguments.offline, arguments.context_lines, arguments.ca_bundle)
        try:
            group["packets"] = build_evidence_packets(group, arguments.packet_bytes)
        except ValueError as error:
            group["packets"] = []
            group["preparation_error"] = str(error)
    return save_prepared_classification_plan(output_directory, plan)


def save_prepared_classification_plan(output_directory: Path, plan: dict[str, Any]) -> int:
    """Save a new immutable v2 preparation and its credential-free review preview.

    Args:
        output_directory: New destination that must not already exist.
        plan: Product groups with raw evidence, compact blocks, and packets.

    Returns:
        Zero for usable groups, or four when at least one group is blocked.

    Raises:
        OSError: If creating or writing the destination fails.
        ValueError: If the prepared report cannot be generated.
    """
    output_directory.mkdir(parents=True, exist_ok=False)
    with lock_batch_directory(output_directory):
        save_evidence_json(output_directory / "plan.json", {
            "schema_version": 2, "plan_id": identify_evidence(plan), "data": plan,
        })
        save_evidence_json(output_directory / "prompt.json", {"version": PROMPT_VERSION, "instructions": CLASSIFICATION_PROMPT})
        summary = write_evidence_reports(output_directory, plan, None)
    print(f"Prepared {summary['repository_product_groups']} repository/product groups, "
          f"{summary['planned_requests']} planned requests. No OpenAI calls made. "
          f"Inspect {output_directory / 'review.csv'} and plan.json.", file=sys.stderr)
    return 4 if summary["groups_by_status"]["blocked"] else 0


def repack_saved_classification_evidence(arguments: argparse.Namespace) -> int:
    """Repackage saved excerpts into a new v2 directory without network access.

    Use this to compare packaging on identical v1 or v2 source evidence. Original
    plans and paid responses remain untouched; changed requests cannot reuse old
    classifications. Missing notebook context stays a recorded limitation.

    Args:
        arguments: Source directory, new output directory, and packet byte cap.

    Returns:
        Zero after preparation, or four if at least one group is blocked.

    Raises:
        OSError: If reading the source or writing the new destination fails.
        ValueError: If the source hash/version is invalid or bounds are invalid.
    """
    if arguments.packet_bytes < 4096:
        raise ValueError("--packet-bytes must be at least 4096.")
    output_directory = arguments.output.resolve()
    if output_directory.exists():
        raise ValueError("Repacking requires a new output directory; the source stays unchanged.")
    source_directory = arguments.source.resolve()
    envelope = read_evidence_json(source_directory / "plan.json")
    plan = envelope.get("data")
    schema_version = envelope.get("schema_version")
    supported_versions = {1: "repository-product-v1", 2: PROMPT_VERSION}
    if (type(schema_version) is not int or schema_version not in supported_versions
            or not isinstance(plan, dict)
            or plan.get("prompt_version") != supported_versions[schema_version]
            or envelope.get("plan_id") != identify_evidence(plan)
            or not isinstance(plan.get("groups"), list)):
        raise ValueError("Saved preparation is invalid, changed, or uses an unsupported version.")
    original_plan_id = envelope["plan_id"]
    source_packets = []
    seen_group_ids = set()
    for group in plan["groups"]:
        if (not isinstance(group, dict) or not isinstance(group.get("product"), dict)
                or not isinstance(group.get("evidence"), list)
                or not isinstance(group.get("files"), list)
                or not isinstance(group.get("packets"), list)
                or not isinstance(group.get("warnings"), list)):
            raise ValueError("Saved preparation contains a malformed evidence group.")
        expected_group_id = identify_evidence([
            group.get("repository"), group["product"].get("provider"),
            group["product"].get("short_name"),
        ])
        if group.get("group_id") != expected_group_id or expected_group_id in seen_group_ids:
            raise ValueError("Saved preparation contains an invalid evidence group identity.")
        seen_group_ids.add(expected_group_id)
        source_packets.extend(group["packets"])
        group.pop("preparation_error", None)
        try:
            group["packets"] = build_evidence_packets(group, arguments.packet_bytes)
        except ValueError as error:
            group["packets"] = []
            group["preparation_error"] = str(error)
    source_input_bytes = sum(len(serialize_evidence(packet).encode("utf-8")) for packet in source_packets)
    plan.update({
        "prompt_version": PROMPT_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
        "packet_bytes": arguments.packet_bytes,
        "repacked_from": {
            "plan_id": original_plan_id, "directory": str(source_directory),
            "planned_requests": len(source_packets), "input_packet_bytes": source_input_bytes,
        },
    })
    result = save_prepared_classification_plan(output_directory, plan)
    repacked_packets = [packet for group in plan["groups"] for packet in group["packets"]]
    repacked_input_bytes = sum(len(serialize_evidence(packet).encode("utf-8")) for packet in repacked_packets)
    print(f"Repacked inputs: {len(source_packets)} -> {len(repacked_packets)} planned requests; "
          f"{source_input_bytes} -> {repacked_input_bytes} JSON bytes. "
          "These are input bytes, not measured tokens or API costs.", file=sys.stderr)
    return result


def select_classification_settings(output_directory: Path, model: str, max_output_tokens: int) -> dict[str, Any]:
    """Validate explicit model settings and preserve them across resumed runs.

    Args:
        output_directory: Prepared evidence directory.
        model: User-selected model identifier, never inferred from a default.
        max_output_tokens: Generated-token cap for each request.

    Returns:
        Settings to persist before the first paid call.

    Raises:
        ValueError: If settings are invalid or differ from an earlier run.
        OSError: If existing settings cannot be read.
    """
    if not model.strip() or any(character.isspace() or ord(character) < 32 for character in model):
        raise ValueError("--model must be a nonempty model identifier without whitespace.")
    if not 256 <= max_output_tokens <= 32768:
        raise ValueError("--max-output-tokens must be between 256 and 32768.")
    settings = {"model": model, "max_output_tokens": max_output_tokens, "prompt_version": PROMPT_VERSION}
    settings_path = output_directory / "settings.json"
    if settings_path.exists() and read_evidence_json(settings_path) != settings:
        raise ValueError("Model/output settings differ from this run; reuse the original settings or prepare a new directory.")
    return settings


def run_prepared_classifications(arguments: argparse.Namespace) -> int:
    """Run bounded requests, preserving successful responses for free local resume.

    Dry runs inspect requests without reading credentials or writing results. Real
    runs hold a process lock, freeze model settings, and write a running receipt
    before each request. Failures and uncertain abandoned requests require explicit
    --retry-failed; successful raw responses are revalidated and never resubmitted.

    Args:
        arguments: Parsed run options including model, request limit, and dry-run flag.

    Returns:
        Zero for a dry run or completed processing, four for pending/blocked groups,
        one for failed requests, or 130 for interruption.

    Raises:
        OSError: If saved evidence or receipts cannot be accessed.
        ValueError: If inputs, credentials, or settings are invalid.
        SourcegraphError: If the shared TLS configuration is invalid.
    """
    output_directory = arguments.output.resolve()
    plan = load_evidence_plan(output_directory)
    is_file_plan = plan.get("assessment_unit") == "file"
    if arguments.max_requests is not None and arguments.max_requests < 1:
        raise ValueError("--max-requests must be positive.")
    settings = select_classification_settings(output_directory, arguments.model, arguments.max_output_tokens)
    scheduled_requests = {}
    skipped_requests = 0
    for group in plan["groups"]:
        for packet in group["packets"]:
            request_body = build_classification_request(packet, settings["model"], settings["max_output_tokens"])
            _, receipt = inspect_classification_receipts(output_directory, request_body, packet)
            if receipt["status"] == "complete":
                skipped_requests += 1
            elif receipt["status"] == "pending" or arguments.retry_failed:
                scheduled_requests.setdefault(identify_evidence(request_body), (request_body, packet))
    selected_requests = list(scheduled_requests.items())[:arguments.max_requests]
    request_bytes = sum(len(serialize_evidence(body).encode("utf-8")) for _, (body, _) in selected_requests)
    assessment_label = "file matches" if is_file_plan else "groups"
    print(f"{len(plan['groups'])} {assessment_label}; {skipped_requests} cached requests; "
          f"{len(scheduled_requests)} eligible; at most {len(selected_requests)} new calls this invocation. "
          f"Request JSON: {request_bytes} bytes; generated-token ceiling: "
          f"{len(selected_requests) * settings['max_output_tokens']}. Bytes are not a token or price estimate.",
          file=sys.stderr, flush=True)
    if arguments.dry_run:
        return 0
    api_key = load_openai_api_key(arguments.api_key_file) if selected_requests else ""
    if selected_requests:
        build_tls_context(arguments.ca_bundle)
    interrupted = False
    with lock_batch_directory(output_directory):
        # Recheck settings and receipts after locking: another runner may have finished.
        settings = select_classification_settings(output_directory, arguments.model, arguments.max_output_tokens)
        save_evidence_json(output_directory / "settings.json", settings)
        write_evidence_reports(output_directory, plan, settings)
        try:
            for number, (request_id, (request_body, packet)) in enumerate(selected_requests, start=1):
                attempts, latest = inspect_classification_receipts(output_directory, request_body, packet)
                if latest["status"] == "complete" or (latest["status"] != "pending" and not arguments.retry_failed):
                    continue
                request_directory = output_directory / "requests" / request_id
                request_directory.mkdir(parents=True, exist_ok=True)
                receipt_path = request_directory / f"attempt-{len(attempts) + 1:06d}.json"
                receipt = {
                    "request_id": request_id, "prompt_version": PROMPT_VERSION,
                    "request": request_body, "status": "running",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
                save_evidence_json(receipt_path, receipt)
                file_label = f" / {packet['assessment_file']} at {packet.get('assessment_commit') or 'unknown revision'}" if is_file_plan else ""
                print(f"[{number}/{len(selected_requests)}] Classify {packet['repository']} / "
                      f"{packet['product']['short_name']}{file_label} (request {request_id[:12]}).", file=sys.stderr, flush=True)
                try:
                    receipt["response"] = request_evidence_classification(request_body, api_key, arguments.ca_bundle)
                    # Persist first: validation or process failure must not lose a paid response.
                    save_evidence_json(receipt_path, receipt)
                    receipt["classification"] = parse_classification_response(receipt["response"], packet)
                    receipt["status"] = "complete"
                except (ValueError, SourcegraphError) as error:
                    receipt["status"] = "failed"
                    receipt["error"] = str(error)
                    print(f"Request failed: {error}", file=sys.stderr)
                receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
                save_evidence_json(receipt_path, receipt)
                if not is_file_plan:
                    write_evidence_reports(output_directory, plan, settings)
                if receipt["status"] == "failed":
                    break
        except KeyboardInterrupt:
            interrupted = True
        summary = write_evidence_reports(output_directory, plan, settings)
    report_name = "classified-matches.csv" if is_file_plan else "review.csv"
    assessment_count = summary["file_matches"] if is_file_plan else summary["repository_product_groups"]
    print(f"Classification {summary['status']}. "
          f"{summary['groups_by_status']['complete']}/{assessment_count} assessments complete; "
          f"{summary['pending_requests']} unstarted requests; "
          f"{summary['failed_or_uncertain_requests']} failed/uncertain requests. "
          f"Results: {output_directory / report_name}. "
          "Completed processing does not establish exhaustive GitHub coverage or validated data use.", file=sys.stderr)
    return 130 if interrupted else {"complete": 0, "incomplete": 4, "failed": 1}[summary["status"]]


def main(argument_values: list[str] | None = None) -> int:
    """Run nasa-evidence-classify preparation, preview, or resumable classification.

    Prepare reads a saved batch and catalog, optionally fetches public GitHub
    context, and writes a new evidence directory. Repack transforms saved excerpts
    without network access into another new directory. Run reads that directory and
    writes model receipts and review.csv. Diagnostics go to standard error.

    Args:
        argument_values: Arguments without the executable name; defaults to sys.argv.

    Returns:
        Zero for successful preparation, preview, or completed classification;
        four for unfinished/blocked work; one for input/API/file errors; or 130
        for interruption. Argparse exits with two for invalid command syntax.
    """
    parser = argparse.ArgumentParser(prog="nasa-evidence-classify", description="Review saved repository/product evidence with OpenAI.")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="gather evidence and preview work without OpenAI calls")
    prepare.add_argument("batch_directory", type=Path)
    prepare.add_argument("--catalog", required=True, type=Path)
    prepare.add_argument("--output", required=True, type=Path, help="new evidence directory")
    prepare.add_argument("--offline", action="store_true", help="use saved matching lines without fetching code context")
    prepare.add_argument("--context-lines", type=int, default=10, help="lines before/after each match (default: 10)")
    prepare.add_argument("--packet-bytes", type=int, default=24000, help="maximum UTF-8 JSON bytes per evidence packet (default: 24000)")
    repack = commands.add_parser("repack", help="repackage saved v1/v2 evidence without network or model calls")
    repack.add_argument("source", type=Path, help="existing prepared evidence directory")
    repack.add_argument("--output", required=True, type=Path, help="new v2 evidence directory")
    repack.add_argument("--packet-bytes", type=int, default=24000, help="maximum UTF-8 JSON bytes per evidence packet (default: 24000)")
    run = commands.add_parser("run", help="classify or resume a prepared evidence directory")
    run.add_argument("output", type=Path)
    run.add_argument("--model", required=True, help="OpenAI model supporting Responses structured outputs")
    run.add_argument(
        "--api-key-file", type=Path,
        help="read the raw key from this file; otherwise use OPENAI_API_KEY, then ./secrets/openai.key",
    )
    run.add_argument("--max-requests", type=int, default=20, help="new API-call limit for this invocation (default: 20)")
    run.add_argument("--max-output-tokens", type=int, default=2048, help="generated-token cap per call (default: 2048)")
    run.add_argument("--dry-run", action="store_true", help="preview work without credentials, API calls, or output writes")
    run.add_argument("--retry-failed", action="store_true", help="explicitly retry failed/uncertain requests, which may incur another charge")
    for command_parser in (prepare, run):
        command_parser.add_argument("--ca-bundle", help="optional PEM certificate authority bundle")
    arguments = parser.parse_args(argument_values)
    try:
        if arguments.command == "prepare":
            return prepare_classification_evidence(arguments)
        if arguments.command == "repack":
            return repack_saved_classification_evidence(arguments)
        return run_prepared_classifications(arguments)
    except (OSError, ValueError, SourcegraphError) as error:
        print(f"nasa-evidence-classify: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Inspect the output before resuming; --retry-failed may repeat a charged request.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

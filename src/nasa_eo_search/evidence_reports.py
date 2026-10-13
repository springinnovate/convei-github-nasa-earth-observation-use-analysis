"""Persist classification evidence and export repository/product review rows."""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from nasa_eo_search.batch_files import atomic_batch_output
from nasa_eo_search.batch_reports import write_protected_csv_rows
from nasa_eo_search.evidence_inputs import identify_evidence, read_evidence_json, serialize_evidence
from nasa_eo_search.evidence_model import PROMPT_VERSION, build_classification_request, parse_classification_response


REVIEW_COLUMNS = (
    "repository", "provider", "short_name", "status", "category", "explanation", "needs_review",
    "evidence_citations", "file_count", "packet_count", "completed_packets", "search_complete",
    "warnings", "error", "model", "prompt_version", "group_id", "human_category", "human_notes",
)


def save_evidence_json(path: Path, value: Any) -> None:
    """Atomically save a generated evidence plan, receipt, or summary.

    Args:
        path: Destination inside the command-owned output directory.
        value: JSON-compatible contents.

    Raises:
        OSError: If writing or replacing the destination fails.
    """
    with atomic_batch_output(path) as output:
        json.dump(value, output, indent=2, ensure_ascii=False)


def load_evidence_plan(output_directory: Path) -> dict[str, Any]:
    """Verify a frozen preparation plan before permitting paid requests.

    Args:
        output_directory: Directory created by the prepare subcommand.

    Returns:
        Validated plan data with intact content hash and current prompt version.

    Raises:
        OSError: If the plan is unavailable.
        ValueError: If the plan was changed, has an unsupported version, or is malformed.
    """
    envelope = read_evidence_json(output_directory / "plan.json")
    plan = envelope.get("data")
    if (envelope.get("schema_version") != 1 or not isinstance(plan, dict)
            or envelope.get("plan_id") != identify_evidence(plan)
            or plan.get("prompt_version") != PROMPT_VERSION or not isinstance(plan.get("groups"), list)):
        raise ValueError("Prepared plan is invalid, changed, or uses another prompt version; prepare a new directory.")
    seen_groups = set()
    for group in plan["groups"]:
        if (not isinstance(group, dict) or not isinstance(group.get("product"), dict)
                or not isinstance(group.get("packets"), list) or not isinstance(group.get("evidence"), list)):
            raise ValueError("Malformed prepared evidence group.")
        expected_id = identify_evidence([group.get("repository"), group["product"].get("provider"), group["product"].get("short_name")])
        if group.get("group_id") != expected_id or expected_id in seen_groups:
            raise ValueError("Invalid or duplicate evidence group identity.")
        seen_groups.add(expected_id)
    return plan


def inspect_classification_receipts(
    output_directory: Path, request_body: dict[str, Any], packet: dict[str, Any],
) -> tuple[list[Path], dict[str, Any]]:
    """Read the latest request receipt, revalidating successful raw responses.

    Args:
        output_directory: Prepared evidence directory.
        request_body: Exact credential-free request used for cache identity.
        packet: Supplied evidence for citation validation.

    Returns:
        Ordered attempt paths and latest state. Abandoned running receipts become
        uncertain; they require explicit retry because the provider may have charged.

    Raises:
        OSError: If a receipt cannot be read.
        ValueError: If receipt paths or request identities are inconsistent.
    """
    request_id = identify_evidence(request_body)
    request_directory = output_directory / "requests" / request_id
    if not request_directory.resolve().is_relative_to(output_directory.resolve()):
        raise ValueError("Request path resolves outside the evidence directory.")
    if not request_directory.exists():
        return [], {"status": "pending"}
    attempts = sorted(request_directory.iterdir())
    for number, path in enumerate(attempts, start=1):
        if path.name != f"attempt-{number:06d}.json" or not path.is_file() or path.is_symlink():
            raise ValueError(f"Unexpected request receipt: {path}")
    if not attempts:
        return [], {"status": "pending"}
    receipt = read_evidence_json(attempts[-1])
    if receipt.get("request_id") != request_id or receipt.get("request") != request_body:
        raise ValueError("Saved classification request differs from its cache identity.")
    if receipt.get("status") not in ("running", "failed", "complete"):
        raise ValueError("Unknown classification receipt status.")
    if "response" in receipt:
        try:
            receipt["classification"] = parse_classification_response(receipt["response"], packet)
            receipt["status"] = "complete"
        except (ValueError, TypeError, AttributeError):
            receipt["status"] = "failed"
            receipt["error"] = "Saved response failed validation; inspect the raw response before retrying."
    elif receipt["status"] in ("running", "complete"):
        receipt["status"] = "uncertain"
        receipt["error"] = "No durable response: request may have been charged. Use --retry-failed only after inspection."
    return attempts, receipt


def combine_packet_classifications(classifications: list[dict[str, Any]]) -> str:
    """Conservatively combine completed packet labels into one review category.

    Args:
        classifications: Validated judgments for every packet in one group, with
            context_only set when a packet contains no saved matching lines.

    Returns:
        Strongest supported relevant category, unrelated if unanimous, or unclear
        for ambiguous matching evidence or conflicting interpretations. An unclear
        context-only packet is neutral, not evidence against the matched code.
    """
    categories = {item["category"] for item in classifications
                  if not (item["category"] == "unclear" and item.get("context_only"))}
    if not categories or "unclear" in categories or ("unrelated" in categories and len(categories) > 1):
        return "unclear"
    for category in ("direct_use", "supporting_software", "reference_only", "unrelated"):
        if category in categories:
            return category
    return "unclear"


def write_evidence_reports(output_directory: Path, plan: dict[str, Any], settings: dict[str, Any] | None) -> dict[str, Any]:
    """Refresh review.csv and summary.json from frozen evidence and saved receipts.

    Human columns start blank; copy the CSV before adding review notes because
    subsequent runs regenerate it. Report model processing independently of search
    completeness. Include usage from all attempts, and count requests without usage.

    Args:
        output_directory: Command-owned output directory.
        plan: Validated frozen preparation plan.
        settings: Selected model/output limit, or None before the first run.

    Returns:
        Summary counts, saved token usage, coverage, and overall execution status.

    Raises:
        OSError: If receipts cannot be read or reports cannot be written.
        ValueError: If saved receipts have inconsistent identities.
    """
    rows = []
    request_ids = set()
    usage_totals = {"input_tokens": 0, "output_tokens": 0}
    attempts_without_usage = 0
    for group in plan["groups"]:
        classifications = []
        states = []
        errors = []
        for packet_number, packet in enumerate(group["packets"], start=1):
            if settings is None:
                states.append("pending")
                continue
            request_body = build_classification_request(packet, settings["model"], settings["max_output_tokens"])
            attempts, receipt = inspect_classification_receipts(output_directory, request_body, packet)
            states.append(receipt["status"])
            if receipt["status"] == "complete":
                classifications.append({
                    **receipt["classification"], "packet_number": packet_number,
                    "context_only": not any(item["kind"] == "matching_line" for item in packet["evidence"]),
                })
            elif receipt.get("error"):
                errors.append(receipt["error"])
            request_id = identify_evidence(request_body)
            if request_id not in request_ids:
                request_ids.add(request_id)
                for attempt in attempts:
                    raw_receipt = read_evidence_json(attempt)
                    response = raw_receipt.get("response")
                    usage = response.get("usage") if isinstance(response, dict) else None
                    if not isinstance(usage, dict) or not all(type(usage.get(field)) is int and usage[field] >= 0 for field in usage_totals):
                        attempts_without_usage += 1
                    else:
                        for field in usage_totals:
                            usage_totals[field] += usage[field]
        status = "complete" if states and all(state == "complete" for state in states) else "pending"
        if not group["packets"]:
            status = "blocked"
            errors.append(group.get("preparation_error", "No usable matching lines."))
        elif any(state in ("failed", "uncertain") for state in states):
            status = "failed"
        category = combine_packet_classifications(classifications) if status == "complete" else "unclear"
        evidence_by_id = {item["evidence_id"]: item for item in group["evidence"]}
        citations = []
        for citation in sorted({citation for result in classifications for citation in result["evidence_ids"]}):
            item = evidence_by_id[citation]
            location = f"{item['file']}:{item['line_number']}"
            if item["commit"] and item["repository"].startswith("github.com/"):
                location = (f"https://{item['repository']}/blob/{quote(item['commit'], safe='')}/"
                            f"{quote(item['file'], safe='/')}#L{item['line_number']}")
            citations.append(f"{citation}: {location}")
        explanations = [f"Packet {result['packet_number']}: {result['explanation']}" for result in classifications]
        if status != "complete":
            explanations.insert(0, "Assessment unfinished; no final category assigned.")
        rows.append({
            "repository": group["repository"], "provider": group["product"]["provider"],
            "short_name": group["product"]["short_name"], "status": status, "category": category,
            "explanation": "\n".join(explanations),
            "needs_review": str(status != "complete" or category == "unclear" or bool(group["warnings"])
                                or len(group["packets"]) > 1 or any(result["needs_review"] for result in classifications)).lower(),
            "evidence_citations": "\n".join(citations), "file_count": len(group["files"]),
            "packet_count": len(group["packets"]), "completed_packets": len(classifications),
            "search_complete": str(group["search_complete"]).lower(), "warnings": "\n".join(group["warnings"]),
            "error": "\n".join(sorted(set(errors))), "model": settings["model"] if settings else "",
            "prompt_version": PROMPT_VERSION, "group_id": group["group_id"],
        })
    counts = {status: sum(row["status"] == status for row in rows) for status in ("complete", "pending", "failed", "blocked")}
    overall_status = "failed" if counts["failed"] else "incomplete" if counts["pending"] or counts["blocked"] else "complete"
    summary = {
        "status": overall_status, "updated_at": datetime.now(timezone.utc).isoformat(),
        "repository_product_groups": len(rows), "groups_by_status": counts,
        "planned_requests": sum(len(group["packets"]) for group in plan["groups"]),
        "input_packet_bytes": sum(len(serialize_evidence(packet).encode("utf-8")) for group in plan["groups"] for packet in group["packets"]),
        "recorded_usage_all_attempts": usage_totals, "attempts_without_recorded_usage": attempts_without_usage,
        "search_scope": plan["search_scope"], "catalog_status": plan["catalog_status"],
        "selected_products": plan["selected_products"],
        "source_product_queries_by_status": {
            status: sum(search["status"] == status for search in plan.get("source_product_queries", []))
            for status in sorted({search["status"] for search in plan.get("source_product_queries", [])})
        },
    }
    write_protected_csv_rows(output_directory / "review.csv", REVIEW_COLUMNS, rows)
    save_evidence_json(output_directory / "summary.json", summary)
    return summary

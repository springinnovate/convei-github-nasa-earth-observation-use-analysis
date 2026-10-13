"""Persist classification evidence and export repository/product review rows."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

from nasa_eo_search.batch_files import atomic_batch_output
from nasa_eo_search.batch_reports import write_protected_csv_rows
from nasa_eo_search.evidence_inputs import identify_evidence, read_evidence_json, serialize_evidence
from nasa_eo_search.evidence_model import PROMPT_VERSION, build_classification_request, parse_classification_response


REVIEW_COLUMNS = (
    "repository", "provider", "short_name", "status", "category", "explanation", "needs_review",
    "review_reasons", "evidence_types", "observed_versions", "evidence_citations", "evidence_limitations",
    "file_count", "packet_count", "completed_packets", "search_complete",
    "warnings", "error", "model", "prompt_version", "group_id", "human_category", "human_notes",
)
MAX_SUMMARY_CHARACTERS = 800
MAX_SUMMARY_CITATIONS = 3
CATEGORY_PRIORITY = {category: number for number, category in enumerate(
    ("direct_use", "supporting_software", "reference_only", "unrelated", "unclear"),
)}


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
    if envelope.get("schema_version") != 2 or (isinstance(plan, dict) and plan.get("prompt_version") != PROMPT_VERSION):
        raise ValueError("This run needs evidence format 2 and repository-product-v2. Existing results are unchanged; "
                         "use nasa-evidence-classify repack SOURCE --output NEW, or prepare a new directory.")
    if (not isinstance(plan, dict) or envelope.get("plan_id") != identify_evidence(plan)
            or not isinstance(plan.get("groups"), list)):
        raise ValueError("Prepared plan is invalid or changed; prepare a new directory.")
    seen_groups = set()
    for group in plan["groups"]:
        if (not isinstance(group, dict) or not isinstance(group.get("product"), dict)
                or not isinstance(group.get("packets"), list) or not isinstance(group.get("evidence"), list)
                or not isinstance(group.get("evidence_blocks"), list)):
            raise ValueError("Malformed prepared evidence group.")
        expected_id = identify_evidence([group.get("repository"), group["product"].get("provider"), group["product"].get("short_name")])
        if group.get("group_id") != expected_id or expected_id in seen_groups:
            raise ValueError("Invalid or duplicate evidence group identity.")
        seen_groups.add(expected_id)
        validate_prepared_blocks(group)
    return plan


def validate_prepared_blocks(group: dict[str, Any]) -> None:
    """Check that every model citation maps to preserved source evidence.

    Args:
        group: Frozen repository/product group with raw lines and compact blocks.

    Raises:
        ValueError: If identifiers, source mappings, or packet contents disagree.
    """
    raw_ids = set()
    for evidence in group["evidence"]:
        if (not isinstance(evidence, dict) or not isinstance(evidence.get("evidence_id"), str)
                or evidence["evidence_id"] in raw_ids):
            raise ValueError("Invalid or duplicate raw evidence identifier.")
        raw_ids.add(evidence["evidence_id"])
    blocks_by_id = {}
    fields = ("file", "commit", "line_number", "end_line", "text", "kind")
    for block in group["evidence_blocks"]:
        if (not isinstance(block, dict) or not isinstance(block.get("evidence_id"), str)
                or not re.fullmatch(r"E[1-9][0-9]*(?:\.[1-9][0-9]*)?", block["evidence_id"])
                or block["evidence_id"] in blocks_by_id
                or any(field not in block for field in fields)
                or block.get("repository") != group["repository"]
                or not isinstance(block["text"], str) or not isinstance(block["file"], str)
                or type(block["line_number"]) is not int or type(block["end_line"]) is not int
                or not 1 <= block["line_number"] <= block["end_line"]
                or not isinstance(block.get("source_ids"), list) or not block["source_ids"]
                or any(not isinstance(source_id, str) or source_id not in raw_ids for source_id in block["source_ids"])):
            raise ValueError("Invalid compact evidence block or source mapping.")
        blocks_by_id[block["evidence_id"]] = block
    included_ids = set()
    for packet in group["packets"]:
        if (not isinstance(packet, dict) or packet.get("format_version") != 2
                or packet.get("repository") != group["repository"]
                or not isinstance(packet.get("product"), dict)
                or any(packet["product"].get(field) != group["product"].get(field) for field in ("provider", "short_name"))
                or not isinstance(packet.get("evidence"), list) or not packet["evidence"]):
            raise ValueError("Invalid version 2 evidence packet.")
        packet_ids = set()
        for evidence in packet["evidence"]:
            if not isinstance(evidence, dict) or not isinstance(evidence.get("evidence_id"), str):
                raise ValueError("Invalid packet citation identifier.")
            evidence_id = evidence["evidence_id"]
            block = blocks_by_id.get(evidence_id)
            if (block is None or evidence_id in packet_ids
                    or any(evidence.get(field) != block[field] for field in fields)):
                raise ValueError("Packet citation differs from its preserved evidence block.")
            if "locations" in evidence:
                location_fields = ("file", "commit", "line_number", "end_line")
                compact_locations = evidence["locations"]
                full_locations = block.get("locations")
                if (not isinstance(compact_locations, list) or not isinstance(full_locations, list)
                        or len(compact_locations) != len(full_locations)
                        or any(not isinstance(compact, dict) or not isinstance(full, dict)
                               or any(compact.get(field) != full.get(field) for field in location_fields)
                               for compact, full in zip(compact_locations, full_locations))):
                    raise ValueError("Packet duplicate locations differ from their preserved evidence block.")
            packet_ids.add(evidence_id)
            included_ids.add(evidence_id)
    if group["packets"] and included_ids != set(blocks_by_id):
        raise ValueError("Prepared packets omit one or more preserved evidence blocks.")


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


def rank_packet_classifications(classifications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order substantive judgments by supported category and citation count.

    Args:
        classifications: Validated packet judgments for a repository/product.

    Returns:
        Deterministically ranked judgments, excluding neutral context-only results.
    """
    return sorted(
        (result for result in classifications if not (result["category"] == "unclear" and result.get("context_only"))),
        key=lambda result: (CATEGORY_PRIORITY[result["category"]], -len(result["evidence_ids"]), result["packet_number"]),
    )


def summarize_group_judgments(classifications: list[dict[str, Any]], status: str, category: str) -> str:
    """Create one bounded conclusion without another model call.

    Select the strongest-supported packet explanation and explicitly qualify
    unfinished or conflicting assessments. Preserve every original in audit.json.

    Args:
        classifications: Completed validated packet judgments.
        status: Overall processing status, separate from evidence relevance.
        category: Conservative aggregate category.

    Returns:
        At most MAX_SUMMARY_CHARACTERS of readable summary text.
    """
    ranked = rank_packet_classifications(classifications)
    prefix = ""
    if status != "complete":
        prefix = "Assessment unfinished; no final category assigned. "
        if ranked:
            prefix += "Completed evidence so far: "
    elif category == "unclear":
        categories = {result["category"] for result in ranked}
        prefix = ("Evidence has conflicting relevant and unrelated interpretations; review required. "
                  if "unrelated" in categories and len(categories) > 1
                  else "Matching evidence remains insufficient or ambiguous; review required. ")
        if ranked:
            prefix += "Strongest completed finding: "
    explanation = " ".join(ranked[0]["explanation"].split()) if ranked else ""
    if not prefix and not explanation:
        explanation = "No substantive product evidence was established."
    summary = (prefix + explanation).strip()
    if len(summary) > MAX_SUMMARY_CHARACTERS:
        summary = summary[:MAX_SUMMARY_CHARACTERS - 1].rstrip() + "…"
    return summary


def collect_group_review_reasons(
    group: dict[str, Any], classifications: list[dict[str, Any]], status: str,
    category: str, catalog_status: str,
) -> list[str]:
    """Separate actionable evidence concerns from harmless packaging notices.

    Args:
        group: Prepared group including coverage warnings and packaging gaps.
        classifications: Completed model judgments.
        status: Overall processing status.
        category: Aggregated category or the unfinished placeholder.
        catalog_status: Source catalog completeness status.

    Returns:
        Explicit review reasons. Multiple packets and bounded README excerpts
        alone do not require review; missing matched-file context does.
    """
    reasons = set(group.get("evidence_gaps", []))
    if status != "complete":
        reasons.add("Classification is unfinished; inspect processing status before reviewing the category.")
    elif category == "unclear":
        reasons.add("Matching evidence is ambiguous or has conflicting interpretations.")
    for result in rank_packet_classifications(classifications):
        if result["needs_review"]:
            reasons.update(result["review_reasons"])
    if not group["search_complete"]:
        reasons.add("One or more selected product searches are incomplete.")
    if catalog_status != "complete":
        reasons.add("The source catalog is partial.")
    for warning in group["warnings"]:
        if warning.startswith("README context unavailable") or (
            warning.startswith("README context at ") and "limited to its first 80 lines" in warning
        ):
            continue
        reasons.add(warning)
    return sorted(reasons)


def format_evidence_location(evidence: dict[str, Any], repository: str) -> str:
    """Render a preserved excerpt location as a pinned link when available.

    Args:
        evidence: Block or duplicate location with file, commit, and line fields.
        repository: Repository identifier used when a location omits it.

    Returns:
        Commit-pinned GitHub line-range URL, or a local source location string.
    """
    repository = evidence.get("repository", repository)
    line_number = evidence["line_number"]
    end_line = evidence.get("end_line", line_number)
    if evidence.get("commit") and repository.startswith("github.com/"):
        location = (f"https://{repository}/blob/{quote(evidence['commit'], safe='')}/"
                    f"{quote(evidence['file'], safe='/')}#L{line_number}")
        return f"{location}-L{end_line}" if end_line > line_number else location
    return f"{evidence['file']}:{line_number}" + (f"-{end_line}" if end_line > line_number else "")


def collect_group_citations(
    group: dict[str, Any], classifications: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Select three strong summary citations and preserve every cited occurrence.

    Args:
        group: Prepared group with complete source block mappings.
        classifications: Validated packet judgments, including version citations.

    Returns:
        Short CSV citation strings and full audit records for all cited blocks.
    """
    evidence_by_id = {item["evidence_id"]: item for item in group["evidence_blocks"]}
    ordered_ids = []
    ranked = rank_packet_classifications(classifications)
    for result in [*ranked, *(result for result in classifications if result not in ranked)]:
        citation_ids = [*result["evidence_ids"], *(citation for version in result["observed_versions"] for citation in version["evidence_ids"])]
        for citation in sorted(set(citation_ids), key=lambda value: (
            evidence_by_id[value]["kind"] != "matching_line", evidence_by_id[value]["file"],
            evidence_by_id[value]["line_number"], value,
        )):
            if citation not in ordered_ids:
                ordered_ids.append(citation)
    audit_citations = []
    for citation in ordered_ids:
        block = evidence_by_id[citation]
        locations = block.get("locations") or [block]
        audit_citations.append({
            "evidence_id": citation, "primary_url": format_evidence_location(block, group["repository"]),
            "all_locations": [{**location, "url": format_evidence_location(location, group["repository"])} for location in locations],
            "source_ids": block["source_ids"],
        })
    return ([f"{citation['evidence_id']}: {citation['primary_url']}" for citation in audit_citations[:MAX_SUMMARY_CITATIONS]],
            audit_citations)


def collect_observed_versions(classifications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge repeated observed versions while retaining their source citations.

    Args:
        classifications: Validated completed model judgments.

    Returns:
        Deterministic version observations with all corresponding evidence IDs.
    """
    citations_by_version: dict[str, set[str]] = {}
    for result in classifications:
        for observation in result["observed_versions"]:
            citations_by_version.setdefault(observation["version"], set()).update(observation["evidence_ids"])
    return [{"version": version, "evidence_ids": sorted(citations)}
            for version, citations in sorted(citations_by_version.items())]


def write_evidence_reports(output_directory: Path, plan: dict[str, Any], settings: dict[str, Any] | None) -> dict[str, Any]:
    """Refresh concise review.csv, full audit.json, and processing summary.json.

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
    audit_groups = []
    request_states = []
    request_ids = set()
    usage_totals = {"input_tokens": 0, "output_tokens": 0}
    attempts_without_usage = 0
    for group in plan["groups"]:
        classifications = []
        states = []
        errors = []
        packet_audits = []
        for packet_number, packet in enumerate(group["packets"], start=1):
            if settings is None:
                states.append("pending")
                packet_audits.append({"packet_number": packet_number, "status": "pending"})
                continue
            request_body = build_classification_request(packet, settings["model"], settings["max_output_tokens"])
            attempts, receipt = inspect_classification_receipts(output_directory, request_body, packet)
            states.append(receipt["status"])
            packet_audit = {
                "packet_number": packet_number, "request_id": identify_evidence(request_body),
                "status": receipt["status"], "receipts": [str(attempt.relative_to(output_directory)) for attempt in attempts],
            }
            if receipt["status"] == "complete":
                classification = {
                    **receipt["classification"], "packet_number": packet_number,
                    "context_only": not any(item["kind"] == "matching_line" for item in packet["evidence"]),
                }
                classifications.append(classification)
                packet_audit["classification"] = classification
            elif receipt.get("error"):
                errors.append(receipt["error"])
                packet_audit["error"] = receipt["error"]
            packet_audits.append(packet_audit)
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
        request_states.extend(states)
        status = "complete" if states and all(state == "complete" for state in states) else "pending"
        if not group["packets"]:
            status = "blocked"
            errors.append(group.get("preparation_error", "No usable matching lines."))
        elif any(state in ("failed", "uncertain") for state in states):
            status = "failed"
        category = combine_packet_classifications(classifications) if status == "complete" else "unclear"
        citations, audit_citations = collect_group_citations(group, classifications)
        review_reasons = collect_group_review_reasons(group, classifications, status, category, plan["catalog_status"])
        observed_versions = collect_observed_versions(classifications)
        evidence_types = sorted({kind for result in classifications for kind in result["evidence_types"]})
        limitations = sorted(set([*group["warnings"], *group.get("packaging_notes", []), *group.get("evidence_gaps", [])]))
        row = {
            "repository": group["repository"], "provider": group["product"]["provider"],
            "short_name": group["product"]["short_name"], "status": status, "category": category,
            "explanation": summarize_group_judgments(classifications, status, category),
            "needs_review": str(bool(review_reasons)).lower(), "review_reasons": "\n".join(review_reasons),
            "evidence_types": "; ".join(evidence_types),
            "observed_versions": "; ".join(f"{item['version']} ({', '.join(item['evidence_ids'])})" for item in observed_versions),
            "evidence_limitations": "\n".join(limitations),
            "evidence_citations": "\n".join(citations), "file_count": len(group["files"]),
            "packet_count": len(group["packets"]), "completed_packets": len(classifications),
            "search_complete": str(group["search_complete"]).lower(), "warnings": "\n".join(group["warnings"]),
            "error": "\n".join(sorted(set(errors))), "model": settings["model"] if settings else "",
            "prompt_version": PROMPT_VERSION, "group_id": group["group_id"],
        }
        rows.append(row)
        audit_groups.append({
            "group_id": group["group_id"], "repository": group["repository"],
            "provider": group["product"]["provider"], "short_name": group["product"]["short_name"],
            "status": status, "category": category, "summary": row["explanation"],
            "needs_review": bool(review_reasons), "review_reasons": review_reasons,
            "evidence_types": evidence_types, "observed_versions": observed_versions,
            "evidence_limitations": limitations, "search_complete": group["search_complete"],
            "citations": audit_citations, "evidence_blocks": group["evidence_blocks"], "packets": packet_audits,
        })
    counts = {status: sum(row["status"] == status for row in rows) for status in ("complete", "pending", "failed", "blocked")}
    overall_status = "failed" if counts["failed"] else "incomplete" if counts["pending"] or counts["blocked"] else "complete"
    summary = {
        "status": overall_status, "updated_at": datetime.now(timezone.utc).isoformat(),
        "repository_product_groups": len(rows), "groups_by_status": counts,
        "planned_requests": sum(len(group["packets"]) for group in plan["groups"]),
        "completed_requests": request_states.count("complete"), "pending_requests": request_states.count("pending"),
        "failed_or_uncertain_requests": sum(state in ("failed", "uncertain") for state in request_states),
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
    save_evidence_json(output_directory / "audit.json", {
        "schema_version": 2, "prompt_version": PROMPT_VERSION, "model": settings["model"] if settings else None,
        "summary_method": "Strongest-supported packet conclusion; all packet judgments and citations retained below.",
        "search_scope": plan["search_scope"], "groups": audit_groups,
    })
    save_evidence_json(output_directory / "summary.json", summary)
    return summary

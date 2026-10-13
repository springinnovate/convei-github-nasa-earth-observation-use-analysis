"""Compare frozen sample selections with current evidence and independent labels."""

from html import escape
import math
from typing import Any


REVIEW_CATEGORIES = ("direct_use", "supporting_software", "reference_only", "unrelated", "unclear")
SUPPORTED_CATEGORIES = {"direct_use", "supporting_software"}
NEGATIVE_CATEGORIES = {"reference_only", "unrelated"}
REVIEW_SPLITS = ("development", "test", "regression")
SAMPLE_KINDS = ("ordinary", "challenge")
USAGE_FIELDS = (
    "request_count", "completed_requests", "attempt_count", "input_tokens", "output_tokens", "attempts_without_usage",
)
COMPARISON_EXCLUSIONS = (
    "missing_snapshot", "stale_snapshot", "stale_label", "unreviewed", "machine_incomplete",
    "human_unclear", "model_abstention",
)


def make_review_rate(numerator: int, denominator: int, definition: str) -> dict[str, Any]:
    """Report a sample proportion with explicit counts and an absent zero-denominator value.

    Args:
        numerator: Number of selected examples satisfying the measured condition.
        denominator: Number of selected examples eligible for this measure.
        definition: Plain-language description of those two counts.

    Returns:
        Counts, a fractional value or None, and the denominator definition.
    """
    return {
        "numerator": numerator, "denominator": denominator,
        "value": numerator / denominator if denominator else None, "definition": definition,
    }


def read_review_minutes(label: dict[str, Any]) -> float | None:
    """Read optional elapsed human-review time without treating blank time as zero.

    Args:
        label: Independently maintained human label record.

    Returns:
        Finite nonnegative minutes, or None when no duration was recorded.

    Raises:
        ValueError: If a supplied duration is not a finite nonnegative number.
    """
    recorded_minutes = label.get("review_minutes", "")
    if recorded_minutes is None or recorded_minutes == "":
        return None
    try:
        if isinstance(recorded_minutes, bool):
            raise ValueError
        minutes = float(recorded_minutes)
    except (TypeError, ValueError):
        raise ValueError("Review minutes must be blank or a finite nonnegative number.") from None
    if not math.isfinite(minutes) or minutes < 0:
        raise ValueError("Review minutes must be blank or a finite nonnegative number.")
    return minutes


def build_review_comparison_row(
    selection: dict[str, Any], current_group: dict[str, Any] | None,
    matching_label: dict[str, Any] | None, other_label_revisions: list[str],
) -> dict[str, Any]:
    """Describe one selected example without applying an obsolete human judgment.

    Args:
        selection: Manifest entry identifying the exact evidence revision and split.
        current_group: Current classification snapshot, or None for a missing group.
        matching_label: Label matching the manifest group and evidence revision.
        other_label_revisions: Other saved revisions for this group, never applied.

    Returns:
        Readable comparison fields, independent processing/revision status, and
        metric eligibility indicators. Stale human labels remain unapplied.

    Raises:
        ValueError: If snapshot categories, usage counts, or human time are invalid.
    """
    current_evidence = current_group is not None and current_group.get("evidence_revision") == selection["evidence_revision"]
    usable_label = matching_label if current_evidence else None
    human_category = usable_label.get("human_category", "") if usable_label else ""
    machine_category = current_group.get("category", "") if current_group else ""
    machine_status = current_group.get("status", "missing") if current_group else "missing"
    if machine_status == "complete" and machine_category not in REVIEW_CATEGORIES:
        raise ValueError("A completed snapshot has an invalid classification category.")
    if human_category and human_category not in REVIEW_CATEGORIES:
        raise ValueError("A human label has an invalid classification category.")
    if current_group is None:
        review_status = "missing_snapshot"
    elif not current_evidence:
        review_status = "stale_snapshot"
    elif matching_label is None and other_label_revisions:
        review_status = "stale_label"
    elif not human_category:
        review_status = "unreviewed"
    else:
        review_status = "reviewed"
    exclusion_reason = review_status if review_status != "reviewed" else ""
    if not exclusion_reason and machine_status != "complete":
        exclusion_reason = "machine_incomplete"
    if not exclusion_reason and human_category == "unclear":
        exclusion_reason = "human_unclear"
    if not exclusion_reason and machine_category == "unclear":
        exclusion_reason = "model_abstention"
    paired_complete = review_status == "reviewed" and machine_status == "complete"
    definitive_human_pair = paired_complete and human_category != "unclear"
    category_comparable = not exclusion_reason
    row = {
        "group_id": selection["group_id"], "evidence_revision": selection["evidence_revision"],
        "snapshot_evidence_revision": current_group.get("evidence_revision", "") if current_group else "",
        "split": selection["split"], "sample_kind": selection["sample_kind"], "family": selection.get("family", ""),
        "repository": selection.get("repository", current_group.get("repository", "") if current_group else ""),
        "provider": selection.get("provider", current_group.get("provider", "") if current_group else ""),
        "short_name": selection.get("short_name", current_group.get("short_name", "") if current_group else ""),
        "current_evidence": current_evidence, "review_status": review_status,
        "other_label_revisions": sorted(set(other_label_revisions)),
        "machine_status": machine_status, "machine_category": machine_category,
        "human_category": human_category, "label_present": usable_label is not None,
        "supporting_citation": usable_label.get("supporting_citation", "") if usable_label else "",
        "evidence_sufficiency": usable_label.get("evidence_sufficiency", "") if usable_label else "",
        "missing_context_reason": usable_label.get("missing_context_reason", "") if usable_label else "",
        "notes": usable_label.get("notes", "") if usable_label else "",
        "review_minutes": read_review_minutes(usable_label) if usable_label else None,
        "needs_review": current_group.get("needs_review", False) if current_evidence else None,
        "eligible_for_comparison": category_comparable, "exclusion_reason": exclusion_reason,
        "category_disagreement": machine_category != human_category if category_comparable else None,
        "false_use_or_support": (machine_category in SUPPORTED_CATEGORIES and human_category in NEGATIVE_CATEGORIES)
        if definitive_human_pair else None,
        "missed_supported_finding": (human_category in SUPPORTED_CATEGORIES and machine_category in NEGATIVE_CATEGORIES)
        if definitive_human_pair else None,
        "model_abstention": machine_category == "unclear" if current_evidence and machine_status == "complete" else None,
        "supported_finding_abstention": (human_category in SUPPORTED_CATEGORIES and machine_category == "unclear")
        if definitive_human_pair else None,
    }
    for field in USAGE_FIELDS:
        recorded_count = current_group.get(field, 0) if current_evidence else None
        if current_evidence and (type(recorded_count) is not int or recorded_count < 0):
            raise ValueError(f"Snapshot {field} must be a nonnegative integer.")
        row[field] = recorded_count
    if current_evidence and type(row["needs_review"]) is not bool:
        raise ValueError("Snapshot needs_review must be boolean.")
    return row


def summarize_review_comparisons(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize one selected sample stratum with explicit metric denominators.

    Args:
        rows: Revision-checked comparison rows for one split/sample-kind stratum.

    Returns:
        Sample counts, exclusions, rates, recorded usage, and human review time.
        Machine abstentions and uncertain human labels are not counted as wrong
        definitive categories. Missing evidence is measured separately.
    """
    current_rows = [row for row in rows if row["current_evidence"]]
    completed_rows = [row for row in current_rows if row["machine_status"] == "complete"]
    reviewed_rows = [row for row in current_rows if row["review_status"] == "reviewed"]
    paired_rows = [row for row in reviewed_rows if row["machine_status"] == "complete"]
    definitive_human_pairs = [row for row in paired_rows if row["human_category"] != "unclear"]
    categorical_rows = [row for row in rows if row["eligible_for_comparison"]]
    machine_supported_pairs = [row for row in definitive_human_pairs if row["machine_category"] in SUPPORTED_CATEGORIES]
    human_supported_pairs = [row for row in definitive_human_pairs if row["human_category"] in SUPPORTED_CATEGORIES]
    evidence_review_rows = [row for row in current_rows if row["evidence_sufficiency"] in ("sufficient", "insufficient", "uncertain")]
    annotated_rows = [row for row in current_rows if row["human_category"] or row["evidence_sufficiency"] or row["missing_context_reason"]]
    recorded_minutes = [row["review_minutes"] for row in current_rows if row["review_minutes"] is not None]
    usage = {field: sum(row[field] for row in current_rows) for field in USAGE_FIELDS}
    rates = {
        "category_agreement": make_review_rate(
            sum(not row["category_disagreement"] for row in categorical_rows), len(categorical_rows),
            "Identical categories / current complete machine-human pairs with neither category unclear.",
        ),
        "category_disagreement": make_review_rate(
            sum(row["category_disagreement"] for row in categorical_rows), len(categorical_rows),
            "Different definitive categories / current complete machine-human pairs with neither category unclear.",
        ),
        "false_use_or_support": make_review_rate(
            sum(row["false_use_or_support"] for row in machine_supported_pairs), len(machine_supported_pairs),
            "Machine direct_use/supporting_software with human reference_only/unrelated / reviewed complete machine use/support claims with a definitive human label.",
        ),
        "missed_supported_findings": make_review_rate(
            sum(row["missed_supported_finding"] for row in human_supported_pairs), len(human_supported_pairs),
            "Human direct_use/supporting_software with machine reference_only/unrelated / current complete human-supported pairs; machine unclear is reported separately.",
        ),
        "supported_finding_abstentions": make_review_rate(
            sum(row["supported_finding_abstention"] for row in human_supported_pairs), len(human_supported_pairs),
            "Machine unclear / current complete human direct_use/supporting_software pairs.",
        ),
        "model_abstentions": make_review_rate(
            sum(row["model_abstention"] for row in completed_rows), len(completed_rows),
            "Machine unclear / all current complete selected machine assessments, including unreviewed examples.",
        ),
        "human_unclear": make_review_rate(
            sum(row["human_category"] == "unclear" for row in paired_rows), len(paired_rows),
            "Human unclear / current complete machine assessments with a nonblank human category.",
        ),
        "needs_review_workload": make_review_rate(
            sum(row["needs_review"] for row in completed_rows), len(completed_rows),
            "Machine needs_review=true / all current complete selected machine assessments.",
        ),
        "evidence_insufficiency": make_review_rate(
            sum(row["evidence_sufficiency"] == "insufficient" for row in evidence_review_rows), len(evidence_review_rows),
            "Human evidence_sufficiency=insufficient / current examples with an explicit sufficiency assessment.",
        ),
        "evidence_uncertainty": make_review_rate(
            sum(row["evidence_sufficiency"] == "uncertain" for row in evidence_review_rows), len(evidence_review_rows),
            "Human evidence_sufficiency=uncertain / current examples with an explicit sufficiency assessment.",
        ),
        "missing_context": make_review_rate(
            sum(bool(row["missing_context_reason"].strip()) for row in annotated_rows), len(annotated_rows),
            "Nonblank missing_context_reason / current examples with a category, sufficiency assessment, or missing-context reason.",
        ),
    }
    return {
        "selected_groups": len(rows), "current_evidence_groups": len(current_rows),
        "reviewed_groups": len(reviewed_rows), "paired_complete_groups": len(paired_rows),
        "category_comparison_groups": len(categorical_rows),
        "groups_by_review_status": {
            status: sum(row["review_status"] == status for row in rows)
            for status in ("reviewed", "unreviewed", "stale_label", "missing_snapshot", "stale_snapshot")
        },
        "classification_exclusions": {reason: sum(row["exclusion_reason"] == reason for row in rows) for reason in COMPARISON_EXCLUSIONS},
        "metrics": rates,
        "usage": {
            **usage, "groups_included": len(current_rows), "groups_excluded_stale_or_missing": len(rows) - len(current_rows),
            "token_usage_complete_for_recorded_attempts": usage["attempts_without_usage"] == 0,
            "definition": "Planned requests, completed requests, recorded attempts and recorded token totals for current selected evidence only; not a price estimate.",
        },
        "human_review_time": {
            "recorded_minutes": sum(recorded_minutes), "groups_with_recorded_minutes": len(recorded_minutes),
            "reviewed_groups_without_recorded_minutes": sum(row["review_minutes"] is None for row in reviewed_rows),
            "mean_recorded_minutes": sum(recorded_minutes) / len(recorded_minutes) if recorded_minutes else None,
        },
    }


def compare_review_labels(
    snapshot: dict[str, Any], manifest_entries: list[dict[str, Any]], labels: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare selected current evidence with exact-revision human labels offline.

    This pure function neither reads files nor contacts a model. It does not apply
    labels across changed evidence revisions or infer population-wide accuracy or
    GitHub coverage from a selected sample. Keep development, test, and regression
    samples separate, with ordinary and challenge examples broken out within each.

    Args:
        snapshot: Current groups including evidence revisions, machine judgments,
            processing state, request counts, and recorded token totals.
        manifest_entries: Selected group/revision identities, split, sample_kind,
            and repository-family grouping metadata validated by the caller.
        labels: Human CSV records already validated by the caller. Blank categories
            remain unreviewed; labels for other evidence revisions remain unused.

    Returns:
        Per-selection comparison rows and overall, split, sample-kind, and joint
        summaries. Rate values are None when their denominator is zero.

    Raises:
        ValueError: For duplicate current groups/selections/matching labels, invalid
            split values, or inconsistent category, time, or count fields.
    """
    groups_by_id = {}
    for group in snapshot.get("groups", []):
        group_id = group["group_id"]
        if group_id in groups_by_id:
            raise ValueError("Snapshot contains duplicate group IDs.")
        groups_by_id[group_id] = group
    selected_identities = set()
    for selection in manifest_entries:
        identity = (selection["group_id"], selection["evidence_revision"])
        if identity in selected_identities:
            raise ValueError("Manifest contains a duplicate group/revision selection.")
        if selection["split"] not in REVIEW_SPLITS or selection["sample_kind"] not in SAMPLE_KINDS:
            raise ValueError("Manifest split or sample_kind is invalid.")
        selected_identities.add(identity)
    labels_by_identity = {}
    revisions_by_group: dict[str, set[str]] = {}
    for label in labels:
        identity = (label["group_id"], label["evidence_revision"])
        revisions_by_group.setdefault(label["group_id"], set()).add(label["evidence_revision"])
        if identity in selected_identities:
            if identity in labels_by_identity:
                raise ValueError("Human labels contain duplicate matching group/revision entries.")
            labels_by_identity[identity] = label
    rows = []
    for selection in manifest_entries:
        identity = (selection["group_id"], selection["evidence_revision"])
        other_revisions = sorted(revisions_by_group.get(selection["group_id"], set()) - {selection["evidence_revision"]})
        rows.append(build_review_comparison_row(selection, groups_by_id.get(selection["group_id"]),
                                               labels_by_identity.get(identity), other_revisions))
    applied_label_identities = {(row["group_id"], row["evidence_revision"]) for row in rows if row["label_present"]}
    summary = summarize_review_comparisons(rows)
    summary.update({
        "scope": "Selected repository/product evidence examples only; not population accuracy or GitHub recall.",
        "limitations": [
            "Small convenience samples and deliberately selected challenge examples do not establish population-wide classification accuracy.",
            "Agreement with human labels depends on their quality; evidence insufficiency is reported separately from model disagreements.",
            "These comparisons cannot measure repositories missed by search; search coverage needs independent known examples.",
            "Development, held-out test, regression, ordinary, and challenge strata are reported separately and must not be treated as random samples.",
        ],
        "by_split": {split: summarize_review_comparisons([row for row in rows if row["split"] == split]) for split in REVIEW_SPLITS},
        "by_sample_kind": {kind: summarize_review_comparisons([row for row in rows if row["sample_kind"] == kind]) for kind in SAMPLE_KINDS},
        "by_split_and_sample_kind": {
            split: {kind: summarize_review_comparisons([row for row in rows if row["split"] == split and row["sample_kind"] == kind])
                    for kind in SAMPLE_KINDS}
            for split in REVIEW_SPLITS
        },
        "unused_label_rows": sum((label["group_id"], label["evidence_revision"]) not in applied_label_identities for label in labels),
    })
    return {"rows": rows, "summary": summary}


def format_review_metric_rate(metric: dict[str, Any] | None) -> str:
    """Format one sample rate without presenting an empty denominator as zero percent.

    Args:
        metric: Numerator/denominator record from a comparison summary, or None.

    Returns:
        Plain text counts and a percentage, or an explicit unavailable message.
    """
    if not isinstance(metric, dict):
        return "Not available"
    numerator = metric.get("numerator")
    denominator = metric.get("denominator")
    if type(numerator) is not int or type(denominator) is not int or not 0 <= numerator <= denominator:
        return "Not available"
    if denominator == 0:
        return "Not available (0 eligible)"
    return f"{numerator:,} / {denominator:,} ({100 * numerator / denominator:.1f}%)"


def format_recorded_review_number(value: Any) -> str:
    """Format recorded counts or durations while keeping missing values visible.

    Args:
        value: A recorded numeric value or None for unavailable information.

    Returns:
        Plain text number or an unavailable marker, never HTML markup.
    """
    if value is None:
        return "Not recorded"
    if type(value) is int:
        return f"{value:,}"
    if type(value) is float and math.isfinite(value):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def render_review_metrics_table(headers: list[str], rows: list[list[str]], caption: str) -> str:
    """Render an accessible static table with escaped headings and cell text.

    Args:
        headers: Human-readable column headings.
        rows: Plain text cells, not pre-rendered HTML.
        caption: Plain text table caption.

    Returns:
        HTML table markup with no scripts, external links, or unescaped data.
    """
    heading_markup = "".join(f'<th scope="col">{escape(str(header))}</th>' for header in headers)
    row_markup = "".join(
        "<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return (f'<div class="table-scroll"><table><caption>{escape(caption)}</caption>'
            f"<thead><tr>{heading_markup}</tr></thead><tbody>{row_markup}</tbody></table></div>")


def render_review_metrics(summary: dict[str, Any]) -> str:
    """Render an understandable local HTML report without file access or network calls.

    Present numerator/denominator counts, separate evaluation splits, exclusions,
    recorded resource usage, and provenance. All supplied text is escaped; trusted
    styling is inline and the document contains no scripts or external assets.

    Args:
        summary: Comparison summary, optionally including a provenance dictionary
            with model, prompt/rubric versions, and input identity hashes.

    Returns:
        Complete static HTML document suitable for an atomic local file write.
    """
    count_cards = "".join(
        f'<div class="count"><strong>{escape(format_recorded_review_number(summary.get(field)))}</strong>'
        f"<span>{escape(label)}</span></div>"
        for field, label in (
            ("selected_groups", "Selected examples"), ("reviewed_groups", "Human categories recorded"),
            ("category_comparison_groups", "Eligible category comparisons"),
        )
    )
    metric_labels = (
        ("category_agreement", "Matching categories",
         "Same category from the machine and human, among completed assessments with clear categories from both."),
        ("category_disagreement", "Different categories",
         "Different categories from the machine and human, among completed assessments with clear categories from both."),
        ("false_use_or_support", "Use/support claims contradicted by review",
         "Human review finds only a reference or an unrelated match, among machine use/support claims with a clear human judgment."),
        ("missed_supported_findings", "Supported findings missed",
         "Machine finds only a reference or an unrelated match, among human-confirmed use/support examples. Uncertain machine results are separate."),
        ("supported_finding_abstentions", "Supported findings left uncertain",
         "Machine cannot decide, among completed examples where human review confirms application use or software support."),
        ("model_abstentions", "Machine judgments left uncertain",
         "Machine cannot decide, among all completed assessments of current selected evidence, including examples not yet human-reviewed."),
        ("needs_review_workload", "Machine flags requiring human review",
         "Assessments flagged for human attention, among all completed assessments of current selected evidence."),
        ("evidence_insufficiency", "Collected evidence judged insufficient",
         "Human review says the collected evidence is insufficient, among examples with an explicit evidence-quality assessment."),
        ("evidence_uncertainty", "Evidence sufficiency still uncertain",
         "Human review cannot decide whether enough evidence was collected, among examples with an explicit evidence-quality assessment."),
        ("missing_context", "Examples with missing context recorded",
         "A missing-context reason is recorded, among examples with a human category, evidence-quality assessment, or missing-context reason."),
    )
    metrics = summary.get("metrics", {})
    metric_rows = []
    for metric_name, label, explanation in metric_labels:
        metric = metrics.get(metric_name, {})
        metric_rows.append([label, format_review_metric_rate(metric), explanation])
    headline_table = render_review_metrics_table(
        ["Measure", "Count / eligible examples", "What counts"], metric_rows, "Results for this selected sample",
    )
    split_descriptions = (
        ("development", "Development", "Examples used to refine the rules."),
        ("test", "Held-out test", "Examples reserved for checking the settled rules."),
        ("regression", "Regression", "Known examples checked again after changes."),
    )
    split_tables = []
    joint_summaries = summary.get("by_split_and_sample_kind", {})
    for split, title, description in split_descriptions:
        split_rows = []
        for sample_kind, sample_label in (("ordinary", "Ordinary search results"), ("challenge", "Deliberately difficult examples")):
            stratum = joint_summaries.get(split, {}).get(sample_kind, {})
            stratum_metrics = stratum.get("metrics", {})
            split_rows.append([
                sample_label, format_recorded_review_number(stratum.get("selected_groups")),
                format_recorded_review_number(stratum.get("reviewed_groups")),
                format_review_metric_rate(stratum_metrics.get("category_agreement")),
                format_review_metric_rate(stratum_metrics.get("model_abstentions")),
            ])
        split_tables.append(f"<h3>{escape(title)}</h3><p>{escape(description)}</p>" + render_review_metrics_table(
            ["Example type", "Selected", "Reviewed", "Matching categories", "Machine uncertain"], split_rows, title + " by example type",
        ))
    exclusion_labels = (
        ("missing_snapshot", "Group missing from current classification results"),
        ("stale_snapshot", "Current evidence differs from the selected evidence"),
        ("stale_label", "Human labels belong to older or different evidence"),
        ("unreviewed", "No current human category recorded"),
        ("machine_incomplete", "Machine processing is unfinished or failed"),
        ("human_unclear", "Human category is unclear"),
        ("model_abstention", "Machine category is unclear"),
    )
    exclusions = summary.get("classification_exclusions", {})
    exclusion_table = render_review_metrics_table(
        ["Reason", "Examples"],
        [[label, format_recorded_review_number(exclusions.get(reason))] for reason, label in exclusion_labels],
        "Why examples are excluded from category agreement and disagreement",
    )
    usage = summary.get("usage", {})
    review_time = summary.get("human_review_time", {})
    usage_rows = [[label, format_recorded_review_number(usage.get(field))] for field, label in (
        ("request_count", "Planned classification requests"), ("completed_requests", "Completed requests"),
        ("attempt_count", "Recorded attempts, including retries"), ("input_tokens", "Recorded input tokens"),
        ("output_tokens", "Recorded output tokens"), ("attempts_without_usage", "Attempts without token-usage records"),
        ("groups_excluded_stale_or_missing", "Selected examples excluded from usage because evidence is stale or missing"),
    )]
    has_review_durations = type(review_time.get("groups_with_recorded_minutes")) is int and review_time["groups_with_recorded_minutes"] > 0
    usage_rows.extend([
        ["Recorded human review time (minutes)", format_recorded_review_number(review_time.get("recorded_minutes"))
         if has_review_durations else "Not recorded"],
        ["Examples with a recorded review duration", format_recorded_review_number(review_time.get("groups_with_recorded_minutes"))],
        ["Reviewed examples without a recorded duration", format_recorded_review_number(review_time.get("reviewed_groups_without_recorded_minutes"))],
        ["Mean recorded review time per example (minutes)", format_recorded_review_number(review_time.get("mean_recorded_minutes"))
         if has_review_durations else "Not recorded"],
    ])
    usage_table = render_review_metrics_table(["Recorded work", "Amount"], usage_rows, "Processing and human-review work")
    unknown_usage = usage.get("attempts_without_usage")
    if type(unknown_usage) is int and unknown_usage > 0:
        usage_caveat = (f"{unknown_usage:,} recorded attempt(s) have no token-usage record. "
                        "The displayed token totals are partial; missing usage is unknown, not zero or free.")
    elif unknown_usage is None:
        usage_caveat = "Token-usage completeness is not recorded. Missing usage must not be treated as zero or free."
    else:
        usage_caveat = "Token totals cover recorded attempts only. Planned requests are not completed work, and tokens are not a price estimate."
    provenance = summary.get("provenance", {})
    provenance_table = render_review_metrics_table(
        ["Record", "Value"],
        [[label, str(provenance.get(field) or "Not recorded")] for field, label in (
            ("model", "Model"), ("prompt_version", "Classification prompt"), ("rubric_version", "Human review rubric"),
            ("manifest_id", "Sample selection ID"), ("machine_snapshot_id", "Machine snapshot ID"),
            ("human_labels_id", "Human labels ID"),
        )], "Inputs used for this comparison",
    )
    limitations = summary.get("limitations", [])
    limitation_markup = "".join(f"<li>{escape(str(limitation))}</li>" for limitation in limitations)
    scope = str(summary.get("scope", "This report covers selected examples, not all GitHub repositories."))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>NASA evidence review results</title>
<style>
body {{ margin: 0; background: #f4f6f8; color: #182b3a; font: 16px/1.5 system-ui, sans-serif; }}
main {{ max-width: 1100px; margin: auto; padding: 32px 24px 64px; }}
h1 {{ font-size: 2rem; margin-bottom: 8px; }} h2 {{ margin-top: 36px; }} h3 {{ margin-bottom: 4px; }}
p {{ max-width: 90ch; }} .notice {{ border-left: 4px solid #216780; background: #e8f2f6; padding: 14px 18px; }}
.counts {{ display: flex; flex-wrap: wrap; gap: 16px; margin: 24px 0; }}
.count {{ background: white; border: 1px solid #d4dde3; border-radius: 6px; padding: 16px 24px; min-width: 180px; }}
.count strong {{ display: block; font-size: 1.8rem; }} .count span {{ color: #425867; }}
.table-scroll {{ overflow-x: auto; }} table {{ width: 100%; border-collapse: collapse; background: white; margin: 12px 0 24px; }}
caption {{ text-align: left; font-weight: 600; padding: 10px 0; }}
th, td {{ border: 1px solid #d4dde3; text-align: left; vertical-align: top; padding: 10px 12px; overflow-wrap: anywhere; }}
th {{ background: #e7eef3; }} td:first-child {{ min-width: 180px; }}
li {{ margin: 8px 0; }} details {{ margin-top: 24px; }} summary {{ cursor: pointer; font-weight: 600; }}
@media print {{ body {{ background: white; }} main {{ max-width: none; padding: 0; }} .table-scroll {{ overflow: visible; }} }}
</style></head><body><main>
<h1>NASA evidence review results</h1>
<p>Model: <strong>{escape(str(provenance.get('model') or 'Not recorded'))}</strong> · Classification prompt: {escape(str(provenance.get('prompt_version') or 'Not recorded'))}</p>
<p class="notice">{escape(scope)} This is a selected sample, not a measure of all GitHub usage or search coverage.</p>
<div class="counts">{count_cards}</div>
<p>One example is one repository paired with one NASA product. A human category counts only when it matches the selected, current evidence revision. Category comparisons also require completed machine processing and a clear category from both reviewers.</p>
<h2>What the comparisons show</h2>
<p>Each measure has its own eligible examples. An uncertain judgment is reported separately, not treated as proof that NASA data is absent.</p>
{headline_table}
<h2>Keep the sample groups separate</h2>
<p>Ordinary search results and deliberately difficult examples answer different questions. Do not treat either as a random sample of GitHub.</p>
{''.join(split_tables)}
<h2>What is not yet comparable</h2>
{exclusion_table}
<p>These exclusion reasons are mutually exclusive for each example. An example can still contribute to other measures, such as recorded processing work or evidence sufficiency.</p>
<h2>How much work was recorded</h2>
<p class="notice">{escape(usage_caveat)}</p>
{usage_table}
<p>Usage includes only selected examples whose current evidence matches the selection. Blank review durations are unknown, not zero minutes.</p>
<h2>Limits of this result</h2><ul>{limitation_markup}</ul>
<details><summary>Model and comparison provenance</summary>{provenance_table}</details>
</main></body></html>"""

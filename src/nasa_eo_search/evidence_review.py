"""Create independent human reviews and compare saved evidence entirely offline."""

import argparse
from pathlib import Path
import sys
from typing import Any

from nasa_eo_search.batch_files import atomic_batch_output
from nasa_eo_search.batch_reports import write_protected_csv_rows
from nasa_eo_search.evidence_inputs import identify_evidence
from nasa_eo_search.evidence_reports import load_evidence_plan, save_evidence_json
from nasa_eo_search.review_files import (
    KNOWN_EXAMPLE_COLUMNS, LABEL_COLUMNS, REVIEW_RUBRIC_VERSION, SELECTION_COLUMNS,
    read_classification_snapshot, read_human_labels, read_review_csv,
    read_review_snapshot, save_review_snapshot, write_blind_review_page,
)
from nasa_eo_search.review_metrics import compare_review_labels, render_review_metrics
from nasa_eo_search.review_selection import build_review_selection, compare_known_examples


def prepare_human_review(source: Path, output: Path, selection: Path | None = None) -> None:
    """Freeze saved evidence and responses, then create a separate blank label sheet.

    Args:
        source: Current-format prepared classification directory.
        output: New directory; existing paths are never overwritten.
        selection: Optional explicit repository/product splits and sample kinds.

    Raises:
        OSError: If reading sources or creating artifacts fails.
        ValueError: If source data or selection is invalid.
    """
    if output.exists():
        raise ValueError("Review output already exists. Choose a new directory; human labels will not be overwritten.")
    plan, snapshot = read_classification_snapshot(source)
    selection_rows = read_review_csv(selection, SELECTION_COLUMNS) if selection else None
    entries = build_review_selection(snapshot["groups"], selection_rows)
    output.mkdir(parents=True, exist_ok=False)
    evidence_id = save_review_snapshot(output / "evidence.json", plan)
    machine_id = save_review_snapshot(output / "machine-snapshot.json", snapshot)
    save_review_snapshot(output / "manifest.json", {
        "rubric_version": REVIEW_RUBRIC_VERSION,
        "evidence_snapshot_id": evidence_id, "machine_snapshot_id": machine_id, "entries": entries,
    })
    write_protected_csv_rows(output / "human-labels.csv", LABEL_COLUMNS, entries)
    write_protected_csv_rows(output / "selection.csv", SELECTION_COLUMNS, entries)
    # Deliberately empty: examples must come from independent knowledge, not these hits.
    write_protected_csv_rows(output / "known-examples.csv", KNOWN_EXAMPLE_COLUMNS, [])
    write_blind_review_page(output / "evidence.html", plan, entries)
    print(f"Created {len(entries)} independent review rows in {output / 'human-labels.csv'}. "
          f"Read {output / 'evidence.html'} before looking at model conclusions. "
          "No API calls made. Default selections are regression examples, not held-out tests.", file=sys.stderr)


def load_human_review(directory: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Verify that selection, evidence, and baseline machine artifacts belong together.

    Args:
        directory: Frozen review directory created by prepare.

    Returns:
        Manifest, evidence plan, and saved baseline machine snapshot.

    Raises:
        OSError: If any snapshot is missing.
        ValueError: If snapshots were edited, mixed, or use another rubric.
    """
    manifest = read_review_snapshot(directory / "manifest.json")
    plan = read_review_snapshot(directory / "evidence.json")
    snapshot = read_review_snapshot(directory / "machine-snapshot.json")
    if (manifest.get("rubric_version") != REVIEW_RUBRIC_VERSION
            or manifest.get("evidence_snapshot_id") != identify_evidence(plan)
            or manifest.get("machine_snapshot_id") != identify_evidence(snapshot)
            or snapshot.get("source_plan_id") != identify_evidence(plan)):
        raise ValueError("Review snapshots do not belong together or use an unsupported rubric.")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or build_review_selection(
        snapshot["groups"], [{column: entry[column] for column in SELECTION_COLUMNS} for entry in entries],
    ) != entries:
        raise ValueError("Frozen selection changed. Create another review directory for a different selection.")
    return manifest, plan, snapshot


def compare_human_review(directory: Path, output: Path, labels: Path | None = None,
                         classifications: Path | None = None) -> None:
    """Compare human labels against frozen or new saved model responses, without calls.

    Args:
        directory: Review directory supplying immutable selection and evidence.
        output: New report directory; existing reports/labels are never replaced.
        labels: Separate label CSV; defaults to human-labels.csv in the review.
        classifications: Optional different classification run for comparisons.
            Changed evidence is reported as stale and excluded, not relabeled.

    Raises:
        OSError: If input or output files are unavailable.
        ValueError: If labels, snapshots, or model receipts are invalid.
    """
    if output.exists():
        raise ValueError("Comparison output already exists; choose a new report directory.")
    manifest, plan, snapshot = load_human_review(directory)
    human_labels = read_human_labels(labels or directory / "human-labels.csv", manifest["entries"], plan)
    if classifications is not None:
        _, snapshot = read_classification_snapshot(classifications)
    comparison = compare_review_labels(snapshot, manifest["entries"], human_labels)
    comparison["summary"]["provenance"] = {
        "manifest_id": identify_evidence(manifest), "machine_snapshot_id": identify_evidence(snapshot),
        "human_labels_id": identify_evidence(human_labels), "model": snapshot["model"],
        "prompt_version": snapshot["prompt_version"], "rubric_version": REVIEW_RUBRIC_VERSION,
    }
    output.mkdir(parents=True, exist_ok=False)
    rows = comparison["rows"]
    columns = tuple(dict.fromkeys(key for row in rows for key in row)) or ("group_id", "review_status")
    write_protected_csv_rows(output / "comparison.csv", columns, rows)
    save_evidence_json(output / "metrics.json", comparison["summary"])
    with atomic_batch_output(output / "metrics.html") as report:
        report.write(render_review_metrics(comparison["summary"]))
    # Pin the exact inputs to this comparison, even when evaluating a newer model run.
    save_review_snapshot(output / "comparison-inputs.json", {
        "manifest": manifest, "human_labels": human_labels, "machine_snapshot": snapshot,
    })
    print(f"Compared {len(rows)} selected pairs. Read {output / 'comparison.csv'} and "
          f"{output / 'metrics.html'} for exclusions and denominators. No API calls made; "
          "human labels and source results are unchanged.", file=sys.stderr)


def check_known_example_retrieval(source: Path, known_examples: Path, output: Path) -> None:
    """Check an independent known-example list against saved search evidence.

    Args:
        source: Prepared classification directory containing source query coverage.
        known_examples: Human-supplied independent repository/product example CSV.
        output: New directory for retrieval rows, summary, and frozen inputs.

    Raises:
        OSError: If reading or writing fails.
        ValueError: If evidence or known examples are malformed.
    """
    if output.exists():
        raise ValueError("Retrieval output already exists; choose a new directory.")
    plan = load_evidence_plan(source)
    examples = read_review_csv(known_examples, KNOWN_EXAMPLE_COLUMNS)
    result = compare_known_examples(plan, examples)
    output.mkdir(parents=True, exist_ok=False)
    columns = tuple(dict.fromkeys(key for row in result["rows"] for key in row)) or KNOWN_EXAMPLE_COLUMNS
    write_protected_csv_rows(output / "retrieval.csv", columns, result["rows"])
    save_evidence_json(output / "retrieval-summary.json", result["summary"])
    save_review_snapshot(output / "retrieval-inputs.json", {"plan": plan, "known_examples": examples})
    print(f"Checked {len(examples)} independent examples. Read {output / 'retrieval.csv'}. "
          "This measures known-example retrieval, never GitHub-wide recall. No API calls made.", file=sys.stderr)


def main(argument_values: list[str] | None = None) -> int:
    """Run offline review preparation, label comparison, or known-example checking.

    Inputs are saved classification evidence and optional CSV selections/labels.
    Outputs go to new directories only; existing human judgments remain untouched.
    No command reads credentials, fetches code, or submits model requests.

    Args:
        argument_values: Command arguments without the executable; defaults to sys.argv.

    Returns:
        Zero for successful exports (even when labels are incomplete); one for
        input/file errors; 130 for interruption. Argparse exits two for bad syntax.
    """
    parser = argparse.ArgumentParser(prog="nasa-evidence-review", description="Review and measure saved NASA evidence offline.")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="freeze evidence and create blind human-review files")
    prepare.add_argument("source", type=Path, help="format-2 nasa-evidence-classify output")
    prepare.add_argument("--selection", type=Path, help="optional repository/product split and family CSV")
    compare = commands.add_parser("compare", help="compare human labels with saved machine judgments")
    compare.add_argument("review", type=Path, help="directory created by review prepare")
    compare.add_argument("--labels", type=Path, help="human CSV; defaults to REVIEW/human-labels.csv")
    compare.add_argument("--classifications", type=Path, help="different saved classifier run; evidence must still match")
    retrieval = commands.add_parser("retrieval", help="check independent known examples against saved search results")
    retrieval.add_argument("source", type=Path, help="format-2 nasa-evidence-classify output")
    retrieval.add_argument("--known-examples", type=Path, required=True)
    for command in (prepare, compare, retrieval):
        command.add_argument("--output", required=True, type=Path, help="new output directory (never overwritten)")
    arguments = parser.parse_args(argument_values)
    try:
        if arguments.command == "prepare":
            prepare_human_review(arguments.source, arguments.output, arguments.selection)
        elif arguments.command == "compare":
            compare_human_review(arguments.review, arguments.output, arguments.labels, arguments.classifications)
        else:
            check_known_example_retrieval(arguments.source, arguments.known_examples, arguments.output)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"nasa-evidence-review: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Existing inputs are unchanged; choose a new output directory to retry.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

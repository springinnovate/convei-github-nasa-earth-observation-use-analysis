"""Run the catalog, public-code search, and file-classification workflow."""

import argparse
import csv
from pathlib import Path
import sys

from nasa_eo_search.batch_files import lock_batch_directory
from nasa_eo_search.batch_plan import validate_saved_batch_plan
from nasa_eo_search.catalog import main as collect_catalog
from nasa_eo_search.evidence_inputs import read_evidence_json
from nasa_eo_search.evidence_reports import save_evidence_json
from nasa_eo_search.file_classify import classify_file_matches
from nasa_eo_search.pipeline_catalog import build_catalog_search_plan, export_dataset_table
from nasa_eo_search.repo_batch import execute_batch_searches
from nasa_eo_search.sourcegraph import SourcegraphError


def collect_dataset_table(arguments: argparse.Namespace) -> int:
    """Collect the NASA catalog and export its table, or export an existing catalog.

    Args:
        arguments: Catalog options. With source, output is a new CSV path;
            otherwise output is a new directory containing datasets.csv and raw data.

    Returns:
        Collector exit status, or zero after exporting an existing saved catalog.

    Raises:
        OSError: If catalog files or output cannot be accessed.
        ValueError: If saved catalog data are malformed or output already exists.
    """
    if arguments.source is not None:
        export_dataset_table(arguments.source, arguments.output)
        print(f"Dataset table: {arguments.output}. No catalog requests made.", file=sys.stderr)
        return 0
    values = ["--output", str(arguments.output)]
    if arguments.short_name is not None:
        values.extend(["--short-name", arguments.short_name])
    if arguments.ca_bundle:
        values.extend(["--ca-bundle", arguments.ca_bundle])
    result = collect_catalog(values)
    if result in (0, 4, 130) and (arguments.output / "products.jsonl").is_file():
        export_dataset_table(arguments.output, arguments.output / "datasets.csv")
        print(f"Dataset table: {arguments.output / 'datasets.csv'}.", file=sys.stderr)
    return result


def search_dataset_catalog(arguments: argparse.Namespace) -> int:
    """Search all selected catalog terms or resume their saved search plan.

    Args:
        arguments: Source catalog, output directory, optional exact product filters,
            host scope, dry-run flag, and certificate bundle.

    Returns:
        Zero for a preview or completed searches, four for limits/incomplete work,
        one for failed searches, or 130 for interruption.

    Raises:
        OSError: If plans, attempts, or reports cannot be read or written.
        ValueError: If the catalog or saved plan is invalid or the selection changed.
    """
    plan = build_catalog_search_plan(arguments.catalog, arguments.short_name, arguments.all_hosts)
    output = arguments.output.resolve()
    if output.exists():
        saved = read_evidence_json(output / "plan.json")
        validate_saved_batch_plan(saved)
        if (saved.get("report_unit") != "file" or saved["jobs"] != plan["jobs"]
                or saved["all_hosts"] != plan["all_hosts"]):
            raise ValueError("Output contains another search selection or legacy run; choose a new output directory.")
        plan = saved
    products = {(product["provider"], product["short_name"])
                for job in plan["jobs"] for product in job["products"]}
    print(f"{len(products)} datasets; {len(plan['jobs'])} distinct search queries. "
          f"Scope: {plan['scope']}. Limits will be recorded in searches.csv.", file=sys.stderr, flush=True)
    if arguments.dry_run:
        print("Preview only; no searches or output writes.", file=sys.stderr)
        return 0
    if not output.exists():
        output.mkdir(parents=True, exist_ok=False)
        save_evidence_json(output / "plan.json", plan)
    with lock_batch_directory(output):
        # Re-read after locking in case another process changed the saved plan.
        current = read_evidence_json(output / "plan.json")
        validate_saved_batch_plan(current)
        if current != plan:
            raise ValueError("Search plan changed while starting; inspect it before resuming.")
        return execute_batch_searches(output, plan, arguments.ca_bundle)


def main(argument_values: list[str] | None = None) -> int:
    """Run nasa-eo catalog, search, or classify and report the resulting table.

    Catalog collects metadata and writes datasets.csv. Search reads a saved catalog
    and writes matches.csv with one dataset/file/revision row. Classify gathers
    nearby code automatically and writes classified-matches.csv with GPT judgments.
    Repeating search/classify with the same inputs and output resumes saved work.

    Args:
        argument_values: Arguments without the executable name, or None for sys.argv.

    Returns:
        Zero for success/preview, four for unfinished work, one for an input/service
        error, or 130 for interruption. Argparse exits two for invalid syntax.
    """
    parser = argparse.ArgumentParser(prog="nasa-eo", description="Collect NASA datasets, search code, and classify matching files.")
    commands = parser.add_subparsers(dest="command", required=True)
    catalog = commands.add_parser("catalog", help="collect datasets and export their names and sources")
    catalog.add_argument("--from", dest="source", type=Path, help="export an existing catalog; --output is a new CSV path")
    catalog.add_argument("--short-name", help="collect one exact dataset name for a trial")
    search = commands.add_parser("search", help="search catalog terms and save one row per dataset/file")
    search.add_argument("catalog", type=Path, help="directory from the catalog command")
    search.add_argument("--short-name", action="append", help="search only this dataset; repeat for several")
    search.add_argument("--all-hosts", action="store_true", help="include all indexed public code hosts, not just GitHub")
    search.add_argument("--dry-run", action="store_true", help="show the selected query count without searching")
    classify = commands.add_parser("classify", help="classify saved matching files; automatically gather context and resume")
    classify.add_argument("batch_directory", type=Path, help="saved search output directory")
    classify.add_argument("--catalog", type=Path, help="source catalog if it has moved; otherwise use its saved location")
    classify.add_argument("--model", required=True, help="GPT model supporting Responses structured outputs")
    classify.add_argument("--api-key-file", type=Path, help="key file; default lookup includes ./secrets/openai.key")
    classify.add_argument("--offline", action="store_true", help="use saved snippets without downloading surrounding code; GPT calls still occur")
    classify.add_argument("--dry-run", action="store_true", help="preview without downloads, API calls, key reads, or writes")
    classify.add_argument("--max-requests", type=int, help="optional new-call limit; by default process all planned files")
    classify.add_argument("--retry-failed", action="store_true", help="explicitly retry failed/uncertain requests; may incur another charge")
    classify.add_argument("--context-lines", type=int, default=10, help="lines around a match (default: 10)")
    classify.add_argument("--packet-bytes", type=int, default=24000, help="maximum evidence JSON bytes per request (default: 24000)")
    classify.add_argument("--max-output-tokens", type=int, default=2048, help="generated-token cap per call (default: 2048)")
    for command in (catalog, search, classify):
        command.add_argument("--output", required=True, type=Path)
        command.add_argument("--ca-bundle", help="optional PEM certificate authority bundle")
    arguments = parser.parse_args(argument_values)
    try:
        if arguments.command == "catalog":
            if arguments.source is not None and arguments.short_name is not None:
                raise ValueError("--short-name filters a new collection; omit it when exporting --from an existing catalog.")
            return collect_dataset_table(arguments)
        if arguments.command == "search":
            return search_dataset_catalog(arguments)
        return classify_file_matches(arguments)
    except (OSError, ValueError, csv.Error, SourcegraphError) as error:
        print(f"nasa-eo: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Saved results remain available; repeat the same search/classify command to resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

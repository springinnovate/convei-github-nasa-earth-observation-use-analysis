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
from nasa_eo_search.partition_search import execute_partitioned_searches
from nasa_eo_search.repo_batch import execute_batch_searches
from nasa_eo_search.search_partitions import partition_catalog_plan
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
    """Search the catalog with GitHub or Sourcegraph, retaining the saved backend.

    New output directories default to GitHub unless ``--backend`` is supplied.
    For an existing directory, read ``plan.json`` to choose the same backend
    and reject incompatible options before handing work to that search command.

    Args:
        arguments: Catalog/output paths, backend selection, dataset filters,
            request and download limits, and resume/dry-run options.

    Returns:
        Backend exit status: zero for success/preview, four for incomplete work,
        one for failures, or 130 for interruption.

    Raises:
        OSError: If an existing plan cannot be read.
        ValueError: If the backend or its options conflict with saved work.
    """
    saved_backend = None
    if arguments.output.exists():
        saved_plan = read_evidence_json(arguments.output / "plan.json")
        saved_backend = saved_plan.get("backend", "sourcegraph")
        if saved_backend not in {"github", "sourcegraph"}:
            raise ValueError("Unknown saved search backend; existing outputs were not changed.")
    backend = arguments.backend or saved_backend or "github"
    if saved_backend is not None and backend != saved_backend:
        raise ValueError("A saved search cannot change backend; choose a new output directory.")
    if backend == "github":
        if arguments.all_hosts or arguments.search_timeout is not None:
            raise ValueError("--all-hosts and --search-timeout apply only to --backend sourcegraph.")
        from nasa_eo_search.github_search import search_github_catalog
        return search_github_catalog(arguments)
    if any(getattr(arguments, option) is not None for option in (
        "github_pages", "max_repositories", "max_archive_mb", "max_total_mb", "include_grep_app",
        "github_discovery", "discovery_aliases",
        "web_search_model", "api_key_file",
    )):
        raise ValueError("GitHub discovery/download options cannot be used with a Sourcegraph search.")
    return search_sourcegraph_catalog(arguments)


def search_sourcegraph_catalog(arguments: argparse.Namespace) -> int:
    """Run or resume Sourcegraph queries for the selected catalog datasets.

    New runs split queries into repository groups and save progress in the
    output directory. Existing runs reuse their saved query plan. A dry run
    prints query counts without making requests or writing files.

    Args:
        arguments: Source catalog, output directory, optional exact product filters,
            host scope, timeout, request budget, retry flag, and certificate bundle.

    Returns:
        Zero for a preview or completed searches, four for limits/incomplete work,
        one for failed searches, or 130 for interruption.

    Raises:
        OSError: If plans, attempts, or reports cannot be read or written.
        ValueError: If the catalog or saved plan is invalid or the selection changed.
    """
    if arguments.max_requests is not None and arguments.max_requests < 1:
        raise ValueError("--max-requests must be positive when supplied.")
    if arguments.search_timeout is not None and not 1 <= arguments.search_timeout <= 60:
        raise ValueError("--search-timeout must be 1 through 60 seconds.")
    catalog_plan = build_catalog_search_plan(arguments.catalog, arguments.short_name, arguments.all_hosts)
    output = arguments.output.resolve()
    if output.exists():
        saved = read_evidence_json(output / "plan.json")
        validate_saved_batch_plan(saved)
        if saved["schema_version"] == 1:
            if arguments.max_requests is not None or arguments.retry_incomplete or arguments.search_timeout is not None:
                raise ValueError("These search controls require a new partitioned output directory; this directory uses the original global-query strategy.")
            plan = catalog_plan
            print("Resuming the original global-query strategy. Choose a new output directory for partitioned searches.", file=sys.stderr)
        else:
            search_timeout = arguments.search_timeout or saved["search_timeout_seconds"]
            plan = partition_catalog_plan(catalog_plan, search_timeout)
        if (saved.get("report_unit") != "file" or saved["jobs"] != plan["jobs"]
                or saved["all_hosts"] != plan["all_hosts"]):
            raise ValueError("Output contains another search selection or legacy run; choose a new output directory.")
        plan = saved
    else:
        plan = partition_catalog_plan(catalog_plan, arguments.search_timeout or 10)
    products = {(product["provider"], product["short_name"])
                for job in plan["jobs"] for product in job["products"]}
    terms = {job["term"] for job in plan["jobs"]}
    print(f"{len(products)} datasets; {len(terms)} distinct terms; {len(plan['jobs'])} search queries. "
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
        if plan["schema_version"] == 2:
            return execute_partitioned_searches(output, plan, arguments.ca_bundle,
                                                arguments.max_requests, arguments.retry_incomplete)
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
    search.add_argument("--backend", choices=("github", "sourcegraph"), help="new runs default to GitHub; resume uses the saved backend")
    search.add_argument("--short-name", action="append", help="search only this dataset; repeat for several")
    search.add_argument("--all-hosts", action="store_true", help="include all indexed public code hosts, not just GitHub")
    search.add_argument("--dry-run", action="store_true", help="show the selected query count without searching")
    search.add_argument("--max-requests", type=int, help="discovery-call cap per invocation (GitHub default: 10; Sourcegraph counts retries)")
    search.add_argument("--github-pages", type=int, help="maximum pages per term and GitHub search source, 1-10 (new-run default: 10)")
    search.add_argument("--github-discovery", choices=("code", "code-and-readme"), help="new GitHub runs default to code plus repository/README search; resume retains its saved mode")
    search.add_argument("--discovery-aliases", type=Path, help="optional CSV with short_name,term,source_url to broaden GitHub discovery; aliases do not change local catalog matches")
    search.add_argument("--web-search-model", help="opt in to paid OpenAI web discovery with this model; saved runs retain their setting")
    search.add_argument("--api-key-file", type=Path, help="web discovery key file; default lookup includes ./secrets/openai.key")
    search.add_argument("--max-repositories", type=int, help="maximum pinned snapshots (new GitHub run default: 6)")
    search.add_argument("--max-archive-mb", type=int, help="compressed download cap per snapshot (default: 100 decimal MB)")
    search.add_argument("--max-total-mb", type=int, help="total retained snapshot download budget (default: 300 decimal MB)")
    search.add_argument("--include-grep-app", action="store_true", default=None, help="supplement GitHub with bounded grep.app discovery")
    search.add_argument("--search-timeout", type=int, help="seconds per partition: 1-60, default 10 for new runs; retained on resume")
    search.add_argument("--retry-incomplete", action="store_true", help="explicitly retry saved failures/unfinished work; backend-specific limits remain")
    classify = commands.add_parser("classify", help="classify saved matching files; automatically gather context and resume")
    classify.add_argument("batch_directory", type=Path, help="saved search output directory")
    classify.add_argument("--catalog", type=Path, help="source catalog if it has moved; otherwise use its saved location")
    classify.add_argument("--short-name", action="append", help="classify only this exact dataset; repeat for several; omit selectors on resume to reuse the saved selection")
    classify.add_argument("--selection", type=Path, help="CSV of match_id values copied from matches.csv; combines with --short-name; other columns are ignored")
    classify.add_argument("--model", required=True, help="GPT model supporting Responses structured outputs")
    classify.add_argument("--api-key-file", type=Path, help="key file; default lookup includes ./secrets/openai.key")
    classify.add_argument("--offline", action="store_true", help="use saved evidence without public-code downloads; GPT calls still occur")
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

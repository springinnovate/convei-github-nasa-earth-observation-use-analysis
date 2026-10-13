"""Run and resume sequential public-code searches for selected NASA products."""

import argparse
import csv
import json
from pathlib import Path
import sys
import time
from typing import Any

from nasa_eo_search.batch_files import (
    COMPLETED_SEARCH, atomic_batch_output, inspect_search_attempt,
    list_search_attempts, lock_batch_directory,
)
from nasa_eo_search.batch_plan import build_batch_search_plan, validate_saved_batch_plan
from nasa_eo_search.batch_reports import write_batch_reports
from nasa_eo_search.repo_search import main as run_repository_search


def execute_batch_searches(
    output_directory: Path, plan: dict[str, Any], ca_bundle_path: str | None,
) -> int:
    """Run each unfinished query once and refresh product-linked reports.

    Skip verified completed attempts. Retry unfinished queries in newly numbered
    directories, keep historical evidence, and pause one second between requests.
    Stop on Ctrl+C and save a resumable status. The caller must hold the batch lock.

    Args:
        output_directory: Batch directory with an immutable, validated plan.json.
        plan: Jobs and associations to execute, independent of source catalog files.
        ca_bundle_path: Optional certificate authority file for HTTPS requests.

    Returns:
        Zero if all queries completed, four for incomplete searches, one for failed
        searches, or 130 for interruption. Completion covers Sourcegraph's index.

    Raises:
        OSError: If attempt directories or reports cannot be written.
        ValueError: If existing attempt directories have invalid paths.
    """

    requests_started = 0
    interrupted = False
    write_batch_reports(output_directory, plan, "running")
    try:
        for job_number, job in enumerate(plan["jobs"], start=1):
            attempts = list_search_attempts(output_directory, job["search_id"])
            if attempts and inspect_search_attempt(attempts[-1], job["query"])[0]["status"] == COMPLETED_SEARCH:
                print(f"[{job_number}/{len(plan['jobs'])}] Skip completed {job['term']!r}.", file=sys.stderr)
                continue
            if requests_started:
                time.sleep(1)
            next_number = int(attempts[-1].name.split("-")[1]) + 1 if attempts else 1
            attempt_directory = output_directory / "searches" / job["search_id"] / f"attempt-{next_number:06d}"
            print(f"[{job_number}/{len(plan['jobs'])}] Search {job['term']!r}, attempt {next_number}.",
                  file=sys.stderr, flush=True)
            arguments = ["--output", str(attempt_directory)]
            if plan["all_hosts"]:
                arguments.append("--all-hosts")
            if ca_bundle_path:
                arguments.extend(["--ca-bundle", ca_bundle_path])
            arguments.extend(["--", job["term"]])
            requests_started += 1
            exit_code = run_repository_search(arguments)
            if not attempt_directory.is_dir():
                raise OSError(f"Search could not create its output directory: {attempt_directory}")
            if exit_code == 130:
                interrupted = True
                break
            write_batch_reports(output_directory, plan, "running")
    except KeyboardInterrupt:
        interrupted = True
    summary = write_batch_reports(output_directory, plan, "interrupted" if interrupted else None)
    print(f"Batch {summary['status']}: {summary['searches_by_status']}. "
          f"Review {output_directory / 'searches.csv'} and {output_directory / 'matches.csv'}.", file=sys.stderr)
    return {"complete": 0, "incomplete": 4, "failed": 1, "interrupted": 130}[summary["status"]]


def main(argument_values: list[str] | None = None) -> int:
    """Start or resume nasa-repo-batch and save searchable review evidence.

    Start validates a catalog and reviewed CSV selection, then creates a new batch
    directory and saves its plan. Resume reads that plan from an existing directory.
    Both commands use an exclusive process lock and print progress to standard error.

    Args:
        argument_values: Arguments without the executable name; defaults to sys.argv.

    Returns:
        Zero for completion, four for incomplete queries, one for input/file/search
        failure, or 130 for interruption. Argparse exits with two for syntax errors.
    """

    parser = argparse.ArgumentParser(prog="nasa-repo-batch", description="Search a reviewed selection of NASA products.")
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser("start", help="create and run a new batch")
    start_parser.add_argument("catalog_directory", type=Path)
    start_parser.add_argument("--selection", required=True, type=Path, help="reviewed product-selection CSV")
    start_parser.add_argument("--output", required=True, type=Path, help="new batch directory")
    start_parser.add_argument("--all-hosts", action="store_true", help="include all indexed public code hosts")
    start_parser.add_argument("--max-searches", type=int, default=20, help="maximum distinct selected queries (default: 20)")
    resume_parser = commands.add_parser("resume", help="retry unfinished queries in a saved batch")
    resume_parser.add_argument("output", type=Path, help="existing batch directory")
    for command_parser in (start_parser, resume_parser):
        command_parser.add_argument("--ca-bundle", help="optional PEM certificate authority bundle")
    arguments = parser.parse_args(argument_values)
    output_directory = arguments.output.resolve()
    try:
        if arguments.command == "start":
            plan = build_batch_search_plan(arguments.catalog_directory, arguments.selection,
                                           arguments.all_hosts, arguments.max_searches)
            validate_saved_batch_plan(plan)
            output_directory.mkdir(parents=True, exist_ok=False)
        elif not (output_directory / "plan.json").is_file():
            raise ValueError("Resume requires an existing batch directory containing plan.json.")
        with lock_batch_directory(output_directory):
            if arguments.command == "start":
                with atomic_batch_output(output_directory / "plan.json") as plan_output:
                    json.dump(plan, plan_output, indent=2)
            else:
                with (output_directory / "plan.json").open(encoding="utf-8") as plan_input:
                    plan = json.load(plan_input)
                validate_saved_batch_plan(plan)
            if plan["catalog_summary"]["status"] != "complete":
                print(f"Warning: source catalog status is {plan['catalog_summary']['status']}.", file=sys.stderr)
            return execute_batch_searches(output_directory, plan, arguments.ca_bundle)
    except (OSError, ValueError, csv.Error) as batch_error:
        print(f"nasa-repo-batch: {batch_error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Batch interrupted; resume using the saved output directory.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

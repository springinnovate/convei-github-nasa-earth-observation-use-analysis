"""Collect all returned public code matches and preserve search completion evidence."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterable, TextIO

from nasa_eo_search.repo_preview import build_sourcegraph_product_query
from nasa_eo_search.sourcegraph import (
    DEFAULT_SOURCEGRAPH_ENDPOINT,
    ServerSentEvent,
    SourcegraphError,
    SourcegraphProtocolError,
    decode_sourcegraph_event_payload,
    stream_sourcegraph_search_events,
)


def save_search_stream_and_summarize(
    search_events: Iterable[ServerSentEvent],
    search_query: str,
    matches_output: TextIO,
    events_output: TextIO,
    diagnostics_output: TextIO,
) -> dict[str, Any]:
    """Write Sourcegraph matches and events to files and return a run summary.

    Consume the event stream through its completion event. Write matches with
    their query to matches_output, write other events to events_output, and
    flush each batch so collected results are available during the search.
    Count saved records and distinct repositories, print progress, and retain
    each distinct server warning.

    Use the returned dictionary to write summary.json and choose the command's
    exit status. A finished status requires final progress, a completion event,
    and an empty warning list. Connection errors, file errors, and keyboard
    interruption produce a summary of the partial run. The caller closes the
    input stream and output files.

    Args:
        search_events: Events from the caller-owned Sourcegraph response stream.
        search_query: Query to preserve alongside each raw match record.
        matches_output: Destination for JSON Lines match records, flushed per event.
        events_output: Destination for non-match events, including warnings.
        diagnostics_output: Destination for concise live counts and warnings.

    Returns:
        Counts and completion evidence. Status is finished_no_reported_limits,
        incomplete, failed, or interrupted. Transport and protocol failures are
        recorded here so a partial run retains its counts and saved matches.
    """

    started_at = time.monotonic()
    repository_names: set[str] = set()
    reported_warning_keys: set[str] = set()
    run_summary: dict[str, Any] = {
        "status": "incomplete",
        "match_records_saved": 0,
        "repositories_with_matches": 0,
        "received_done_event": False,
        "received_final_progress": False,
        "warnings": [],
        "final_progress": None,
    }
    try:
        for search_event in search_events:
            event_payload = decode_sourcegraph_event_payload(search_event)
            if search_event.event_type == "matches":
                if not isinstance(event_payload, list):
                    raise SourcegraphProtocolError("Sourcegraph matches must be a list.")
                for match_record in event_payload:
                    if not isinstance(match_record, dict):
                        raise SourcegraphProtocolError("Sourcegraph match must be an object.")
                    print(json.dumps({
                        "query": search_query, "match": match_record,
                    }, ensure_ascii=True), file=matches_output)
                    run_summary["match_records_saved"] += 1
                    if isinstance(match_record.get("repository"), str):
                        repository_names.add(match_record["repository"])
                matches_output.flush()
                print(
                    f"Saved {run_summary['match_records_saved']} match records from "
                    f"{len(repository_names)} repositories "
                    f"({time.monotonic() - started_at:.0f}s elapsed).",
                    file=diagnostics_output, flush=True,
                )
                continue

            print(json.dumps({
                "event": search_event.event_type, "data": event_payload,
            }, ensure_ascii=True), file=events_output, flush=True)
            event_warnings = []
            if search_event.event_type == "progress":
                if not isinstance(event_payload, dict):
                    raise SourcegraphProtocolError("Sourcegraph progress must be an object.")
                event_warnings = event_payload.get("skipped", [])
                if not isinstance(event_warnings, list):
                    raise SourcegraphProtocolError("Sourcegraph skipped items must be a list.")
                if event_payload.get("done") is True:
                    run_summary["received_final_progress"] = True
                    run_summary["final_progress"] = event_payload
            elif search_event.event_type == "alert":
                event_warnings = [event_payload]
            elif search_event.event_type == "done":
                run_summary["received_done_event"] = True
                break

            for search_warning in event_warnings:
                warning_key = json.dumps(search_warning, sort_keys=True)
                if warning_key not in reported_warning_keys:
                    reported_warning_keys.add(warning_key)
                    run_summary["warnings"].append(search_warning)
                    warning_description = (
                        search_warning.get("message") or search_warning.get("title")
                        or search_warning.get("reason") or warning_key
                        if isinstance(search_warning, dict) else warning_key
                    )
                    print(f"Search warning: {warning_description}", file=diagnostics_output)

        if (
            run_summary["received_done_event"]
            and run_summary["received_final_progress"]
            and not run_summary["warnings"]
        ):
            run_summary["status"] = "finished_no_reported_limits"
    except (SourcegraphError, OSError) as search_error:
        run_summary["status"] = "failed"
        run_summary["error"] = str(search_error)
    except KeyboardInterrupt:
        run_summary["status"] = "interrupted"
    run_summary["repositories_with_matches"] = len(repository_names)
    run_summary["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
    run_summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return run_summary


def main(argument_values: list[str] | None = None) -> int:
    """Run nasa-repo-search to save a product search in a new directory.

    Parse the phrase and output path, create matches.jsonl, events.jsonl, and
    summary.json, and stream public code matches from Sourcegraph into those
    files. Print saved-result counts and the final status to standard error.

    Args:
        argument_values: Arguments without the executable name; defaults to sys.argv.

    Returns:
        Zero when the service finishes with no reported limits, four for an
        incomplete run, one for failure, or 130 for interruption. Argparse exits
        with two for invalid arguments. Completion describes the queried index.
    """

    argument_parser = argparse.ArgumentParser(
        prog="nasa-repo-search",
        description="Save all returned public code matches, including forks and archives.",
    )
    argument_parser.add_argument("phrase", help="product term or phrase, e.g. ATL03")
    argument_parser.add_argument(
        "--output", required=True, type=Path,
        help="new directory for matches.jsonl, events.jsonl, and summary.json",
    )
    argument_parser.add_argument(
        "--all-hosts", action="store_true",
        help="include every indexed public host (default: GitHub)",
    )
    argument_parser.add_argument("--ca-bundle", help="optional PEM CA bundle")
    parsed_arguments = argument_parser.parse_args(argument_values)
    search_phrase = parsed_arguments.phrase.strip()
    if not search_phrase or any(ord(character) < 32 for character in search_phrase):
        argument_parser.error("phrase must be nonempty and contain no control characters")
    search_query = build_sourcegraph_product_query(
        search_phrase, parsed_arguments.all_hosts, collect_all=True,
    )
    output_directory = parsed_arguments.output.resolve()
    run_metadata = {
        "status": "running",
        "query": search_query,
        "endpoint": DEFAULT_SOURCEGRAPH_ENDPOINT,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "public indexed default-branch content; includes forks and archives",
        "coverage_note": "Results cover public repository content in Sourcegraph's index.",
    }
    try:
        output_directory.mkdir(parents=True, exist_ok=False)
        summary_path = output_directory / "summary.json"
        with summary_path.open("x", encoding="utf-8") as summary_output:
            json.dump(run_metadata, summary_output, indent=2)
        print(
            f"Collecting all returned matches for {search_phrase!r}. "
            f"Saving to {output_directory}", file=sys.stderr, flush=True,
        )
        with (
            (output_directory / "matches.jsonl").open("x", encoding="utf-8") as matches_output,
            (output_directory / "events.jsonl").open("x", encoding="utf-8") as events_output,
            closing(stream_sourcegraph_search_events(
                DEFAULT_SOURCEGRAPH_ENDPOINT, search_query,
                os.environ.get("SOURCEGRAPH_TOKEN"), 65.0,
                ca_bundle_path=parsed_arguments.ca_bundle,
            )) as search_events,
        ):
            run_metadata.update(save_search_stream_and_summarize(
                search_events, search_query, matches_output, events_output, sys.stderr,
            ))
        with summary_path.open("w", encoding="utf-8") as summary_output:
            json.dump(run_metadata, summary_output, indent=2, ensure_ascii=True)
    except OSError as output_error:
        print(f"nasa-repo-search: {output_error}", file=sys.stderr)
        return 1

    print(
        f"{run_metadata['status']}: {run_metadata['match_records_saved']} match records, "
        f"{run_metadata['repositories_with_matches']} repositories. Summary: {summary_path}",
        file=sys.stderr,
    )
    if run_metadata["status"] == "finished_no_reported_limits":
        return 0
    if run_metadata["status"] == "interrupted":
        return 130
    if run_metadata["status"] == "failed":
        print(run_metadata.get("error", "Search failed."), file=sys.stderr)
        return 1
    print(
        "Search incomplete. Inspect summary.json and events.jsonl "
        "for completion status and service warnings.",
        file=sys.stderr,
    )
    return 4


if __name__ == "__main__":
    raise SystemExit(main())

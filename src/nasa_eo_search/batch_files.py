"""Protect batch output files and verify saved search-attempt evidence."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterator, TextIO

from nasa_eo_search.repo_preview import format_repository_match
from nasa_eo_search.search_partitions import repository_matches_partition
from nasa_eo_search.sourcegraph import DEFAULT_SOURCEGRAPH_ENDPOINT


COMPLETED_SEARCH = "finished_no_reported_limits"
EMPTY_REPOSITORY_SCOPE_NOTICE = {
    "title": "No repositories found",
    "description": "Try using a different `repo:<regexp>` filter to see results",
    "proposedQueries": None,
}


def list_search_attempts(output_directory: Path, search_id: str) -> list[Path]:
    """Find a job's numbered attempt directories in creation order.

    Args:
        output_directory: Batch root containing the searches directory.
        search_id: Validated hexadecimal query identifier from the saved plan.

    Returns:
        Existing attempt directories sorted by numeric attempt number.

    Raises:
        ValueError: For malformed names or directories outside the batch root.
    """

    search_directory = output_directory / "searches" / search_id
    if not search_directory.resolve().is_relative_to(output_directory.resolve()):
        raise ValueError("Search directory resolves outside the batch output.")
    attempts_by_number = {}
    if search_directory.exists():
        for attempt in search_directory.iterdir():
            name_match = re.fullmatch(r"attempt-([0-9]{6})", attempt.name)
            if (not name_match or not attempt.is_dir() or attempt.is_symlink()
                    or int(name_match[1]) < 1):
                raise ValueError(f"Unexpected attempt path: {attempt}")
            attempts_by_number[int(name_match[1])] = attempt
    return [attempts_by_number[number] for number in sorted(attempts_by_number)]


def inspect_search_attempt(
    attempt: Path, query: str, *, partition_id: str | None = None, all_hosts: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Verify a saved attempt before skipping it on resume or reporting matches.

    A completed attempt needs final completion flags, no warnings, matching query
    metadata, both evidence files, and the expected number of valid match records.
    Preserve readable matches before a corrupt record for inspection. A saved
    running status means the previous process stopped before finalizing it.
    When a partition is supplied, keep out-of-scope matches as evidence but mark
    an otherwise completed attempt incomplete. Never rewrite saved source files.
    A completed zero-hit partition with the exact empty-repository alert is a
    verified empty indexed scope, not a search limit. Its derived status is
    completed with the alert retained in notices and the original incomplete
    status retained in raw_status. The raw collector summary remains unchanged.

    Args:
        attempt: Existing attempt directory containing single-search outputs.
        query: Expected query from the batch plan.
        partition_id: Repository partition to validate, or None for legacy searches.
        all_hosts: Whether the partitioned plan includes non-GitHub public hosts.

    Returns:
        Attempt status/details and readable raw matches. Broken evidence is marked
        failed, so the next resume retries it in a new attempt directory.
    """

    summary: dict[str, Any] = {"status": "failed"}
    matches = []
    errors = []
    try:
        with (attempt / "summary.json").open(encoding="utf-8") as summary_input:
            saved_summary = json.load(summary_input)
        if (not isinstance(saved_summary, dict) or saved_summary.get("query") != query
                or saved_summary.get("endpoint") != DEFAULT_SOURCEGRAPH_ENDPOINT):
            raise ValueError("Saved summary has an invalid or different query.")
        summary = saved_summary
        if summary.get("status") == "running":
            summary["status"] = "interrupted"
        if summary.get("status") not in (COMPLETED_SEARCH, "incomplete", "failed", "interrupted"):
            raise ValueError("Invalid saved search status.")
    except (OSError, ValueError) as evidence_error:
        errors.append(str(evidence_error))
    try:
        with (attempt / "matches.jsonl").open(encoding="utf-8") as matches_input:
            for line_number, match_line in enumerate(matches_input, start=1):
                try:
                    record = json.loads(match_line)
                    if (not isinstance(record, dict) or record.get("query") != query
                            or not isinstance(record.get("match"), dict)):
                        raise ValueError("Invalid match record or query.")
                    content_match = record["match"]
                    matching_lines = content_match.get("lineMatches", [])
                    if not isinstance(matching_lines, list) or any(
                        not isinstance(line, dict) or type(line.get("lineNumber")) is not int
                        or line["lineNumber"] < 0 or not isinstance(line.get("line", ""), str)
                        for line in matching_lines
                    ):
                        raise ValueError("Invalid matching line data.")
                    format_repository_match(content_match)
                    matches.append(content_match)
                except ValueError as record_error:
                    raise ValueError(f"{attempt.name}/matches.jsonl line {line_number}: {record_error}") from record_error
    except (OSError, ValueError) as evidence_error:
        errors.append(str(evidence_error))
    if summary.get("status") == COMPLETED_SEARCH and (
        summary.get("received_done_event") is not True
        or summary.get("received_final_progress") is not True
        or summary.get("warnings") != []
        or type(summary.get("match_records_saved")) is not int
        or summary["match_records_saved"] != len(matches)
        or not (attempt / "events.jsonl").is_file()
    ):
        errors.append("Saved completion evidence or match count is inconsistent.")
    if summary.get("status") == COMPLETED_SEARCH:
        try:
            received_done = False
            received_final_progress = False
            with (attempt / "events.jsonl").open(encoding="utf-8") as events_input:
                for event_line in events_input:
                    event = json.loads(event_line)
                    if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                        raise ValueError("Invalid saved event.")
                    if event["event"] == "done":
                        received_done = True
                    elif event["event"] == "alert":
                        raise ValueError("Completed attempt contains a server alert.")
                    elif event["event"] == "progress":
                        progress = event.get("data")
                        if not isinstance(progress, dict) or progress.get("skipped"):
                            raise ValueError("Invalid progress or saved search limits.")
                        received_final_progress |= progress.get("done") is True
            if not received_done or not received_final_progress:
                raise ValueError("Saved events lack final progress or completion.")
        except (OSError, ValueError) as evidence_error:
            errors.append(str(evidence_error))
    if errors:
        summary = {**summary, "status": "failed", "error": "; ".join(errors)}
    if partition_id is not None:
        if not errors and not matches and verify_empty_partition_completion(attempt, summary):
            summary = {
                **summary, "raw_status": summary["status"], "status": COMPLETED_SEARCH,
                "warnings": [], "notices": list(summary["warnings"]),
                "completion_basis": "empty_indexed_repository_scope",
            }
        outside_partition = sum(not repository_matches_partition(
            match.get("repository"), all_hosts, partition_id,
        ) for match in matches)
        if outside_partition:
            scope_warning = {
                "reason": "repository-partition-mismatch",
                "message": f"{outside_partition} returned match records are outside partition {partition_id}.",
            }
            warnings = list(summary.get("warnings", []))
            if scope_warning not in warnings:
                warnings.append(scope_warning)
            summary = {**summary, "warnings": warnings}
            if summary["status"] == COMPLETED_SEARCH:
                summary["status"] = "incomplete"
    return summary, matches


def verify_empty_partition_completion(attempt: Path, summary: dict[str, Any]) -> bool:
    """Verify that an empty partition ended normally despite its informational alert.

    Call only after a validated partition's match file has been checked and found
    empty. Require the exact observed empty-scope alert, zero final match counts,
    no skipped work or other alerts, and consistent completion in both the raw
    summary and saved events. Legacy/global searches do not use this exception.
    Malformed or missing evidence remains unfinished rather than being promoted.

    Args:
        attempt: Existing directory containing the original events.jsonl file.
        summary: Raw, validated attempt summary before any status interpretation.

    Returns:
        Whether the saved evidence proves a completed, empty indexed scope.
        No source files are changed.
    """
    final_progress = summary.get("final_progress")
    if (summary.get("status") != "incomplete" or summary.get("error")
            or summary.get("received_done_event") is not True
            or summary.get("received_final_progress") is not True
            or type(summary.get("match_records_saved")) is not int
            or summary["match_records_saved"] != 0
            or summary.get("warnings") != [EMPTY_REPOSITORY_SCOPE_NOTICE]
            or not isinstance(final_progress, dict) or final_progress.get("error")
            or final_progress.get("done") is not True
            or type(final_progress.get("matchCount")) is not int
            or final_progress["matchCount"] != 0 or final_progress.get("skipped") != []):
        return False
    received_notice = False
    received_done = False
    saved_final_progress = None
    try:
        with (attempt / "events.jsonl").open(encoding="utf-8") as events_input:
            for event_line in events_input:
                event = json.loads(event_line)
                if not isinstance(event, dict) or received_done:
                    return False
                event_type = event.get("event")
                if event_type == "alert":
                    if event.get("data") != EMPTY_REPOSITORY_SCOPE_NOTICE:
                        return False
                    received_notice = True
                elif event_type == "progress":
                    progress = event.get("data")
                    if (not isinstance(progress, dict) or progress.get("error") or progress.get("skipped") != []
                            or ("matchCount" in progress and (
                                type(progress["matchCount"]) is not int or progress["matchCount"] != 0))):
                        return False
                    if progress.get("done") is True:
                        saved_final_progress = progress
                elif event_type == "done":
                    if saved_final_progress is None or event.get("data") != {}:
                        return False
                    received_done = True
                elif event_type != "filters":
                    return False
    except (OSError, ValueError):
        return False
    return received_notice and received_done and saved_final_progress == final_progress


@contextmanager
def atomic_batch_output(destination: Path, encoding: str = "utf-8") -> Iterator[TextIO]:
    """Replace a generated report only after its new contents are fully written.

    Args:
        destination: Batch-owned plan, summary, or report file to publish.
        encoding: Text encoding, including utf-8-sig for spreadsheet reports.

    Yields:
        Temporary text stream in the destination directory.

    Raises:
        OSError: If writing, replacing, or removing the temporary file fails.
    """

    with tempfile.NamedTemporaryFile(mode="w", encoding=encoding, newline="",
                                     dir=destination.parent, prefix=destination.name + ".",
                                     suffix=".tmp", delete=False) as temporary_output:
        temporary_path = Path(temporary_output.name)
        try:
            yield temporary_output
            temporary_output.flush()
            os.fsync(temporary_output.fileno())
        except BaseException:
            temporary_output.close()
            temporary_path.unlink(missing_ok=True)
            raise
    try:
        temporary_path.replace(destination)
    finally:
        temporary_path.unlink(missing_ok=True)


@contextmanager
def lock_batch_directory(output_directory: Path) -> Iterator[None]:
    """Hold a process lock so two runners cannot write to the same batch.

    The operating system releases the lock if the process exits or crashes. The
    small lock file remains in the directory and can be reused on the next run.

    Args:
        output_directory: Existing batch directory to lock.

    Yields:
        Control while the exclusive lock is held.

    Raises:
        ValueError: If another process already holds the batch lock.
        OSError: If the lock file cannot be opened.
    """

    with (output_directory / "batch.lock").open("a+b") as lock_file:
        if lock_file.seek(0, os.SEEK_END) == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt
        else:
            import fcntl
        try:
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as lock_error:
            raise ValueError("Another process is using this batch directory.") from lock_error
        try:
            yield
        finally:
            lock_file.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

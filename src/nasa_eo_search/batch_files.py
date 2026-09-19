"""Protect batch output files and verify saved search-attempt evidence."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterator, TextIO

from nasa_eo_search.repo_preview import format_repository_match
from nasa_eo_search.sourcegraph import DEFAULT_SOURCEGRAPH_ENDPOINT


COMPLETED_SEARCH = "finished_no_reported_limits"


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


def inspect_search_attempt(attempt: Path, query: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Verify a saved attempt before skipping it on resume or reporting matches.

    A completed attempt needs final completion flags, no warnings, matching query
    metadata, both evidence files, and the expected number of valid match records.
    Preserve readable matches before a corrupt record for inspection. A saved
    running status means the previous process stopped before finalizing it.

    Args:
        attempt: Existing attempt directory containing single-search outputs.
        query: Expected query from the batch plan.

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
    return summary, matches


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

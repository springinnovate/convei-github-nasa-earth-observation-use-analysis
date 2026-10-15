"""Execute bounded, checkpointed searches across saved repository partitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time
from typing import Any

from nasa_eo_search.batch_files import (
    COMPLETED_SEARCH, atomic_batch_output, inspect_search_attempt, list_search_attempts,
)
from nasa_eo_search.batch_reports import write_batch_reports
from nasa_eo_search.repo_search import collect_repository_query


SEARCH_STATUSES = (COMPLETED_SEARCH, "incomplete", "failed", "interrupted", "pending")
DEFAULT_ATTEMPT_BUDGET = 2


@dataclass
class PartitionJobState:
    """Reconciled state for one job, kept in memory rather than every checkpoint."""

    status: str = "pending"
    attempt_count: int = 0
    last_attempt_number: int = 0
    attempt_ceiling: int = DEFAULT_ATTEMPT_BUDGET
    latest_summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class PartitionRunState:
    """Small counters and current-request details written at each checkpoint."""

    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SEARCH_STATUSES, 0))
    attempts_recorded: int = 0
    requests_this_invocation: int = 0
    consecutive_unsuccessful_requests: int = 0
    not_before_epoch: float = 0.0
    current_job: dict[str, Any] | None = None
    stop_reason: str | None = None
    status: str = "running"


def write_partition_json(destination: Path, contents: dict[str, Any]) -> None:
    """Publish a partition-run JSON checkpoint atomically.

    Args:
        destination: Run-owned JSON file whose parent already exists.
        contents: Serializable state to publish.

    Raises:
        OSError: If the checkpoint cannot be published.
    """

    with atomic_batch_output(destination) as output_file:
        json.dump(contents, output_file, indent=2, allow_nan=False)


def read_attempt_ceiling(output_directory: Path, search_id: str) -> int:
    """Read a finite retry grant without treating progress caches as evidence.

    Args:
        output_directory: Batch root containing optional retry-budget records.
        search_id: Validated query identifier from the saved plan.

    Returns:
        Absolute allowed attempt count, defaulting to two.

    Raises:
        ValueError: If a saved retry grant is malformed or targets another job.
        OSError: If an existing retry grant cannot be read.
    """

    budget_path = output_directory / "retry-budgets" / f"{search_id}.json"
    if not budget_path.exists():
        return DEFAULT_ATTEMPT_BUDGET
    if budget_path.is_symlink() or not budget_path.resolve().is_relative_to(output_directory.resolve()):
        raise ValueError("Retry budget resolves outside the batch output.")
    with budget_path.open(encoding="utf-8") as budget_input:
        saved_budget = json.load(budget_input)
    if (not isinstance(saved_budget, dict) or saved_budget.get("search_id") != search_id
            or type(saved_budget.get("attempt_ceiling")) is not int
            or saved_budget["attempt_ceiling"] < DEFAULT_ATTEMPT_BUDGET):
        raise ValueError(f"Invalid retry budget for {search_id}.")
    return saved_budget["attempt_ceiling"]


def inspect_partition_attempt(
    attempt_directory: Path, job: dict[str, Any], all_hosts: bool,
) -> dict[str, Any]:
    """Reconcile saved completion evidence and check its repository partition.

    Old attempt files are not rewritten. Use the shared inspector's status so
    progress counts, combined reports, and downstream evidence agree: malformed
    evidence is failed, and a valid abandoned running attempt is interrupted.
    Every attempt counts toward the retry allowance regardless of its status.

    Args:
        attempt_directory: Existing attempt directory.
        job: Validated partition job containing its exact query.
        all_hosts: Whether the plan includes non-GitHub public repositories.

    Returns:
        Verified summary with a conservative status and any scope warning.
    """

    summary, _ = inspect_search_attempt(
        attempt_directory, job["query"], partition_id=job["partition_id"], all_hosts=all_hosts,
    )
    return summary


def retry_not_before(summary: dict[str, Any]) -> float:
    """Reconstruct an HTTP rate-limit or service-unavailable retry deadline.

    Args:
        summary: Saved attempt summary including completion time and retry delay.

    Returns:
        Earliest allowed Unix request time, or zero without valid retry guidance.
    """

    delay_seconds = summary.get("retry_after_seconds")
    if (summary.get("http_status") not in (429, 503) or type(delay_seconds) not in (int, float)
            or not math.isfinite(delay_seconds) or delay_seconds < 0):
        return 0.0
    try:
        finished_at = datetime.fromisoformat(summary["finished_at"])
        if finished_at.tzinfo is None:
            return 0.0
        deadline = finished_at.timestamp() + delay_seconds
        return deadline if math.isfinite(deadline) else 0.0
    except (KeyError, TypeError, ValueError, OverflowError):
        return 0.0


def reconcile_partition_jobs(
    output_directory: Path, plan: dict[str, Any], run_state: PartitionRunState,
) -> dict[str, PartitionJobState]:
    """Build authoritative job counts from attempt files once at startup.

    Args:
        output_directory: Existing batch root.
        plan: Validated partition search plan.
        run_state: Invocation counters to initialize from durable attempts.

    Returns:
        Per-job in-memory status and bounded retry allowance.

    Raises:
        ValueError: If an attempt path or retry grant is invalid.
        OSError: If an existing retry grant cannot be read.
    """

    job_states = {}
    for job in plan["jobs"]:
        attempts = list_search_attempts(output_directory, job["search_id"])
        job_state = PartitionJobState(
            attempt_count=len(attempts),
            last_attempt_number=int(attempts[-1].name.split("-")[1]) if attempts else 0,
            attempt_ceiling=read_attempt_ceiling(output_directory, job["search_id"]),
        )
        if attempts:
            job_state.latest_summary = inspect_partition_attempt(attempts[-1], job, plan["all_hosts"])
            job_state.status = job_state.latest_summary["status"]
            run_state.not_before_epoch = max(run_state.not_before_epoch, retry_not_before(job_state.latest_summary))
        run_state.counts[job_state.status] += 1
        run_state.attempts_recorded += len(attempts)
        job_states[job["search_id"]] = job_state
    return job_states


def checkpoint_partition_run(
    output_directory: Path, run_state: PartitionRunState, report_summary: dict[str, Any],
    *, reports_current: bool = False,
) -> None:
    """Write current counters without scanning jobs or regenerating combined CSVs.

    Args:
        output_directory: Batch root containing the checkpoint and summary.
        run_state: Constant-size live counters and current job.
        report_summary: Most recent combined-report summary, retained for row counts.
        reports_current: Whether combined CSVs were just regenerated.

    Raises:
        OSError: If publishing either checkpoint fails.
    """

    checkpoint = {
        "schema_version": 1, "status": run_state.status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "searches_by_status": run_state.counts,
        "attempts_recorded": run_state.attempts_recorded,
        "requests_this_invocation": run_state.requests_this_invocation,
        "consecutive_unsuccessful_requests": run_state.consecutive_unsuccessful_requests,
        "current_job": run_state.current_job,
        "stop_reason": run_state.stop_reason,
        "not_before_epoch": run_state.not_before_epoch or None,
        "combined_reports_current": reports_current,
        "combined_reports_updated_at": report_summary["updated_at"],
    }
    write_partition_json(output_directory / "progress.json", checkpoint)
    write_partition_json(output_directory / "summary.json", {
        **report_summary, **checkpoint,
        "progress_note": "Counts are current; combined CSVs refresh at startup and when execution stops.",
    })


def reconcile_stopped_run(
    output_directory: Path, plan: dict[str, Any], run_state: PartitionRunState,
) -> None:
    """Refresh counters after an exception interrupts normal attempt accounting.

    Args:
        output_directory: Batch root containing durable attempt evidence.
        plan: Validated partition search plan.
        run_state: Invocation state whose request counts and stop reason are kept.

    Raises:
        ValueError: If a saved attempt path or retry grant is invalid.
        OSError: If retry state cannot be read.
    """

    run_state.counts = dict.fromkeys(SEARCH_STATUSES, 0)
    run_state.attempts_recorded = 0
    run_state.not_before_epoch = 0.0
    job_states = reconcile_partition_jobs(output_directory, plan, run_state)
    if run_state.current_job:
        run_state.current_job["status"] = job_states[run_state.current_job["search_id"]].status


def extend_retry_allowance(
    output_directory: Path, job: dict[str, Any], job_state: PartitionJobState,
) -> None:
    """Persist one explicit additional two-attempt allowance for an existing job.

    Args:
        output_directory: Batch root containing retry-budget records.
        job: Validated job whose retry budget is being extended.
        job_state: Existing attempt count and ceiling to update.

    Raises:
        OSError: If the new finite ceiling cannot be persisted.
        ValueError: If the retry-budget directory escapes the batch root.
    """

    budget_directory = output_directory / "retry-budgets"
    if not budget_directory.resolve().is_relative_to(output_directory.resolve()):
        raise ValueError("Retry budgets resolve outside the batch output.")
    budget_directory.mkdir(exist_ok=True)
    job_state.attempt_ceiling = job_state.attempt_count + DEFAULT_ATTEMPT_BUDGET
    write_partition_json(budget_directory / f"{job['search_id']}.json", {
        "search_id": job["search_id"], "attempt_ceiling": job_state.attempt_ceiling,
    })


def execute_partitioned_searches(
    output_directory: Path, plan: dict[str, Any], ca_bundle_path: str | None,
    max_requests: int | None = None, retry_incomplete: bool = False,
) -> int:
    """Run saved partition queries sequentially with finite retries and checkpoints.

    The caller must validate the schema-2 plan and hold the batch-directory lock.
    Completed attempts are verified before being skipped. Each job has a durable
    two-attempt ceiling; explicit retries grant two more attempts, and ordinary
    resumes only consume the remaining allowance. Raw evidence is never removed.
    Three consecutive unsuccessful requests or HTTP 400/401/403 stop the run.
    Retry-After delays longer than a minute are checkpointed for a later resume.

    Args:
        output_directory: Existing, caller-locked batch output directory.
        plan: Validated schema-2 plan with exact queries and partition identifiers.
        ca_bundle_path: Optional PEM certificate-authority bundle.
        max_requests: Optional nonnegative request cap for this invocation.
        retry_incomplete: Explicitly grant another bounded allowance to old unfinished jobs.

    Returns:
        Zero if all partitions completed, four for a deliberate pause or incomplete
        search, one for failed requests, or 130 for keyboard interruption.

    Raises:
        ValueError: If options, attempt paths, or retry-budget records are invalid.
        OSError: If checkpoints or combined reports cannot be written.
    """

    if plan.get("schema_version") != 2:
        raise ValueError("Partition execution requires a schema-version-2 search plan.")
    if max_requests is not None and (type(max_requests) is not int or max_requests < 0):
        raise ValueError("max_requests must be a nonnegative integer or None.")
    run_state = PartitionRunState()
    job_states = reconcile_partition_jobs(output_directory, plan, run_state)
    report_summary = write_batch_reports(output_directory, plan, "running")
    checkpoint_partition_run(output_directory, run_state, report_summary, reports_current=True)
    next_delay_seconds = 0.0
    exit_status = 4
    try:
        for job in plan["jobs"]:
            job_state = job_states[job["search_id"]]
            if job_state.status == COMPLETED_SEARCH:
                continue
            if retry_incomplete and job_state.attempt_count:
                extend_retry_allowance(output_directory, job, job_state)
            while job_state.attempt_count < job_state.attempt_ceiling:
                if max_requests is not None and run_state.requests_this_invocation >= max_requests:
                    run_state.stop_reason = "request_limit_reached"
                    break
                remaining_retry_delay = max(0.0, run_state.not_before_epoch - time.time())
                if remaining_retry_delay > 60:
                    run_state.stop_reason = "retry_after_not_before"
                    break
                wait_seconds = max(next_delay_seconds, remaining_retry_delay)
                if wait_seconds:
                    print(f"Waiting {wait_seconds:.1f}s before the next request.", file=sys.stderr, flush=True)
                    time.sleep(wait_seconds)
                attempt_number = job_state.last_attempt_number + 1
                attempt_directory = output_directory / "searches" / job["search_id"] / f"attempt-{attempt_number:06d}"
                run_state.current_job = {
                    "search_id": job["search_id"], "term": job["term"],
                    "partition_id": job["partition_id"], "query": job["query"],
                    "attempt": attempt_directory.name, "status": "running",
                }
                run_state.requests_this_invocation += 1
                checkpoint_partition_run(output_directory, run_state, report_summary)
                print(
                    f"Searching {job['term']!r}, partition {job['partition_id']} "
                    f"(attempt {attempt_number}; {run_state.counts[COMPLETED_SEARCH]}/{len(plan['jobs'])} complete).",
                    file=sys.stderr, flush=True,
                )
                request_exit_status = collect_repository_query(
                    job["query"], attempt_directory, ca_bundle_path,
                    timeout_seconds=float(plan.get("search_timeout_seconds", 10)) + 10.0,
                )
                if not attempt_directory.exists():
                    run_state.stop_reason = "attempt_output_failed"
                    exit_status = 1
                    break
                latest_summary = inspect_partition_attempt(attempt_directory, job, plan["all_hosts"])
                if latest_summary.get("completion_basis") == "empty_indexed_repository_scope":
                    print("No indexed repositories in this partition; recorded as a completed zero-hit search.",
                          file=sys.stderr, flush=True)
                # Only this newly created attempt may acquire additional scope warnings.
                if any(warning.get("reason") == "repository-partition-mismatch"
                       for warning in latest_summary.get("warnings", []) if isinstance(warning, dict)):
                    write_partition_json(attempt_directory / "summary.json", latest_summary)
                run_state.counts[job_state.status] -= 1
                job_state.status = latest_summary["status"]
                job_state.latest_summary = latest_summary
                job_state.attempt_count += 1
                job_state.last_attempt_number = attempt_number
                run_state.attempts_recorded += 1
                run_state.counts[job_state.status] += 1
                run_state.current_job["status"] = job_state.status
                run_state.not_before_epoch = max(run_state.not_before_epoch, retry_not_before(latest_summary))
                if job_state.status == COMPLETED_SEARCH:
                    run_state.consecutive_unsuccessful_requests = 0
                else:
                    run_state.consecutive_unsuccessful_requests += 1
                next_delay_seconds = 1.0 if job_state.status == COMPLETED_SEARCH else 2.0
                if request_exit_status == 130 or job_state.status == "interrupted":
                    run_state.stop_reason = "keyboard_interrupted" if request_exit_status == 130 else "attempt_interrupted"
                    exit_status = 130 if request_exit_status == 130 else 1
                elif latest_summary.get("http_status") in (400, 401, 403):
                    run_state.stop_reason = f"http_{latest_summary['http_status']}_requires_attention"
                    exit_status = 1
                elif run_state.not_before_epoch - time.time() > 60:
                    run_state.stop_reason = "retry_after_not_before"
                    exit_status = 4
                elif run_state.consecutive_unsuccessful_requests >= 3:
                    run_state.stop_reason = "three_consecutive_unsuccessful_requests"
                    exit_status = 1 if run_state.counts["failed"] else 4
                checkpoint_partition_run(output_directory, run_state, report_summary)
                if run_state.stop_reason or job_state.status == COMPLETED_SEARCH:
                    break
            if run_state.stop_reason:
                break
        if not run_state.stop_reason:
            if run_state.counts[COMPLETED_SEARCH] == len(plan["jobs"]):
                exit_status = 0
                run_state.stop_reason = "all_partitions_completed"
            else:
                exit_status = 1 if run_state.counts["failed"] else 4
                run_state.stop_reason = "retry_budget_exhausted"
    except KeyboardInterrupt:
        run_state.stop_reason = "keyboard_interrupted"
        exit_status = 130
        # A hard stop can occur after creating a running attempt but before its
        # result is accounted for. Reconcile only on this exceptional path.
        reconcile_stopped_run(output_directory, plan, run_state)
    except (OSError, ValueError):
        run_state.stop_reason = "local_output_or_state_error"
        exit_status = 1
        reconcile_stopped_run(output_directory, plan, run_state)
        raise
    finally:
        run_state.status = (
            "complete" if exit_status == 0 else "interrupted" if exit_status == 130
            else "failed" if exit_status == 1 else "incomplete"
        )
        report_summary = write_batch_reports(output_directory, plan, run_state.status)
        checkpoint_partition_run(output_directory, run_state, report_summary, reports_current=True)
        print(
            f"{run_state.status}: {run_state.counts[COMPLETED_SEARCH]}/{len(plan['jobs'])} partitions complete; "
            f"{run_state.requests_this_invocation} requests this invocation. Stop: {run_state.stop_reason}.",
            file=sys.stderr, flush=True,
        )
    return exit_status

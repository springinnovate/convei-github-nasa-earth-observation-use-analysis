"""Find public GitHub files through GitHub code search and grep.app.

The search command uses these functions for both small samples and larger runs;
page limits are arguments, not a separate trial implementation. GitHub requests
use the user's GitHub CLI login. grep.app requests use its public Model Context
Protocol (MCP) endpoint, which accepts a JSON tool call and returns search text.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from nasa_eo_search.sourcegraph import build_tls_context, parse_server_sent_events


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
GREP_MCP_ENDPOINT = "https://mcp.grep.app"
_last_github_search_started: float | None = None
PUBLIC_SEARCH_FILTER = (
    '{total_count, incomplete_results, '
    'omitted_nonpublic: ([.items[] | select(.repository.private != false)] | length), '
    'items: [.items[] | select(.repository.private == false) | '
    '{path, html_url, sha, repository: {full_name: .repository.full_name, private: false}, '
    'text_matches: [.text_matches[]? | {property, fragment}]}]}'
)


def validate_repository_name(repository: str) -> str:
    """Check a GitHub repository name and remove its optional host prefix.

    Args:
        repository: ``owner/repository`` or ``github.com/owner/repository``.

    Returns:
        The ``owner/repository`` string to include in a GitHub API URL.

    Raises:
        ValueError: If the owner or repository name has invalid characters, or
            the repository is named ``.`` or ``..``.
    """

    repository_name = repository.removeprefix("github.com/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+", repository_name):
        raise ValueError("Expected a GitHub owner/repository name.")
    if repository_name.split("/", 1)[1] in {".", ".."}:
        raise ValueError("Invalid repository name.")
    return repository_name


def validate_search_term(term: str) -> None:
    """Check that a dataset identifier can be sent as one quoted search term.

    Args:
        term: Dataset name or other literal identifier, such as ``ATL03``.

    Raises:
        ValueError: If the term is blank, longer than 256 characters, or contains
            a double quote, newline, carriage return, or NUL character.
    """

    if not isinstance(term, str) or not term.strip() or len(term) > 256:
        raise ValueError("Search terms must contain 1 to 256 characters.")
    if any(character in term for character in ('"', "\n", "\r", "\x00")):
        raise ValueError("Search terms cannot contain quotes or control characters.")


def run_github_json(arguments: list[str]) -> Any:
    """Run ``gh api`` and parse its JSON output.

    GitHub CLI supplies authentication from its own configuration. This function
    waits at most 40 seconds and rejects output larger than 4 MiB. Failure
    messages omit the command's raw error output, which may contain credentials.

    Args:
        arguments: Arguments following ``gh api``; callers supply GET requests.

    Returns:
        The JSON object, array, or value printed by GitHub CLI.

    Raises:
        RuntimeError: If GitHub CLI is missing, fails, times out, or returns an
            oversized or invalid response. Raw error output is never included.
    """

    try:
        response = subprocess.run(
            ["gh", "api", "--hostname", "github.com", *arguments],
            capture_output=True, timeout=40, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("GitHub CLI is unavailable or timed out; check gh auth status.") from error
    if response.returncode:
        raise RuntimeError(
            f"GitHub CLI request failed (exit {response.returncode}); check authentication or rate limits."
        )
    if len(response.stdout) > MAX_RESPONSE_BYTES:
        raise RuntimeError("GitHub response exceeded the 4 MiB response limit.")
    try:
        return json.loads(response.stdout)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("GitHub CLI returned invalid JSON.") from error


def search_github_code(term: str, max_pages: int = 1, *, start_page: int = 1) -> dict[str, Any]:
    """Search GitHub for files containing a dataset identifier.

    Calls are spaced at least 6.1 seconds apart within this Python process. The
    GitHub CLI manages authentication; no token is read by this module. Private
    and unknown-visibility results are discarded by CLI filtering and checked
    again before adding them to the returned result. The caller saves this result
    and decides which repositories to download. A matching name alone does not
    establish that the code uses the dataset.

    Args:
        term: Dataset name or identifier; punctuation follows the legacy
            GitHub code-search API's matching rules.
        max_pages: Maximum 100-result pages to request, between one and ten.
        start_page: First page to request, allowing a saved run to resume without
            fetching earlier pages. The requested range cannot exceed page ten.

    Returns:
        A dictionary with the query, elapsed time, warnings, and ``results``
        containing repository/file/URL records. ``raw`` holds response pages
        after nonpublic matches have been removed. ``status`` is ``returned``
        when the requested query has no more pages, ``capped`` when more pages
        remain, ``incomplete`` when GitHub reports missing results, or ``error``
        when a request fails. Partial results survive a later request failure.
        None of these statuses establishes that all public GitHub code was searched.

    Raises:
        ValueError: If a term or page bound is invalid.
    """

    global _last_github_search_started
    validate_search_term(term)
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 10:
        raise ValueError("max_pages must be an integer between 1 and 10.")
    if type(start_page) is not int or start_page < 1 or start_page + max_pages - 1 > 10:
        raise ValueError("Requested GitHub pages must stay between 1 and 10.")
    query = f'"{term}" in:file fork:true'
    started_at = time.monotonic()
    receipt: dict[str, Any] = {
        "provider": "github", "term": term, "query": query, "status": "returned",
        "results": [], "raw": [], "start_page": start_page, "pages_returned": 0, "has_more": False,
        "warnings": [
            "Legacy GitHub index: default branch, searchable files under 384 KB; archived and some forks excluded.",
            "Query punctuation follows GitHub legacy search; exact boundaries are checked during the local scan.",
            "Raw pages are sanitized to public repositories only; total_count can include discarded private hits.",
        ],
    }
    seen_files: set[tuple[str, str]] = set()
    try:
        for page_number in range(start_page, start_page + max_pages):
            if _last_github_search_started is not None:
                time.sleep(max(0.0, 6.1 - (time.monotonic() - _last_github_search_started)))
            _last_github_search_started = time.monotonic()
            page = run_github_json([
                "--method", "GET", "search/code", "-f", f"q={query}",
                "-f", "per_page=100", "-f", f"page={page_number}",
                "-H", "Accept: application/vnd.github.text-match+json",
                "--jq", PUBLIC_SEARCH_FILTER,
            ])
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise RuntimeError("GitHub returned an unexpected search response.")
            if type(page.get("total_count")) is not int or type(page.get("incomplete_results")) is not bool:
                raise RuntimeError("GitHub omitted search completion metadata.")
            sanitized_items = []
            for item in page["items"]:
                repository = item.get("repository", {}) if isinstance(item, dict) else {}
                if not isinstance(repository, dict) or repository.get("private") is not False:
                    continue
                try:
                    repository_name = validate_repository_name(repository.get("full_name", ""))
                except (ValueError, AttributeError):
                    continue
                file_path = item.get("path")
                file_url = item.get("html_url")
                if not isinstance(file_path, str) or not file_path or not isinstance(file_url, str):
                    continue
                if not file_url.startswith(f"https://github.com/{repository_name}/blob/"):
                    continue
                sanitized_items.append(item)
                identity = (repository_name, file_path)
                if identity not in seen_files:
                    seen_files.add(identity)
                    receipt["results"].append({
                        "repository": f"github.com/{repository_name}", "file": file_path, "url": file_url,
                    })
            receipt["raw"].append({
                "total_count": page["total_count"], "incomplete_results": page["incomplete_results"],
                "omitted_nonpublic": page.get("omitted_nonpublic", 0), "items": sanitized_items,
            })
            receipt["total_count"] = page["total_count"]
            receipt["pages_returned"] += 1
            receipt["has_more"] = page["total_count"] > page_number * 100
            if page["incomplete_results"]:
                receipt["status"] = "incomplete"
                receipt["warnings"].append("GitHub reported incomplete_results=true.")
            if page["total_count"] <= page_number * 100:
                break
            if page_number == start_page + max_pages - 1:
                if receipt["status"] != "incomplete":
                    receipt["status"] = "capped"
                receipt["warnings"].append(
                    f"Stopped after page {page_number}; more hits exist and GitHub exposes at most 1,000 per query."
                )
    except RuntimeError as error:
        receipt["status"] = "error"
        receipt["warnings"].append(str(error))
    receipt["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
    return receipt


def decode_grep_app_response(response_body: bytes, content_type: str) -> dict[str, Any]:
    """Read grep.app's JSON response from a plain body or an event stream.

    grep.app's Model Context Protocol service may return one JSON object or a
    sequence of server-sent events. In either form, select the response whose
    request ID is 1, the ID used by ``search_grep_app``.

    Args:
        response_body: Complete HTTP response bytes; the caller checks the size.
        content_type: HTTP Content-Type value.

    Returns:
        The response dictionary, including its ``result`` or ``error`` field.

    Raises:
        ValueError: If no matching valid response is present.
    """

    if "text/event-stream" in content_type:
        candidates = [json.loads(event.payload_text) for event in
                      parse_server_sent_events(response_body.splitlines(keepends=True))]
    else:
        candidates = [json.loads(response_body)]
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("id") == 1:
            return candidate
    raise ValueError("No matching MCP response was returned.")


def parse_grep_search_results(envelope: dict[str, Any]) -> list[dict[str, str]]:
    """Extract repository names, paths, and URLs from grep.app result text.

    Args:
        envelope: Decoded response from grep.app's ``searchGitHub`` tool, with
            result text blocks under ``result.content``.

    Returns:
        One dictionary per distinct repository/file pair, with ``repository``,
        ``file``, and ``url`` fields. An explicit "no results found" response
        returns an empty list; an unrecognized response raises an error instead.
        This function does not download files or check repository visibility;
        that check occurs before the caller downloads a repository ZIP.

    Raises:
        ValueError: If the service reports an error or its response format changes.
    """

    result = envelope.get("result")
    if "error" in envelope or not isinstance(result, dict) or result.get("isError"):
        raise ValueError("Grep MCP reported a tool or protocol error.")
    content = result.get("content")
    if not isinstance(content, list):
        raise ValueError("Grep MCP omitted content blocks.")
    matches: list[dict[str, str]] = []
    seen_files: set[tuple[str, str]] = set()
    explicit_empty = False
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text", "")
        if not isinstance(text, str):
            raise ValueError("Grep MCP returned an invalid text block.")
        if "\n" not in text and "\\n" in text:
            text = text.replace("\\n", "\n")
        if text.strip().lower().startswith("no results found"):
            explicit_empty = True
            continue
        header = re.match(r"\ARepository: ([^\r\n]+)\r?\nPath: ([^\r\n]+)\r?\nURL: ([^\r\n]+)", text)
        if not header:
            raise ValueError("Grep MCP returned unrecognized result text; inspect the saved receipt.")
        repository_name = validate_repository_name(header.group(1))
        file_path, file_url = header.group(2), header.group(3)
        if not file_url.startswith(f"https://github.com/{repository_name}/blob/"):
            raise ValueError("Grep MCP returned an unexpected repository URL.")
        identity = (repository_name, file_path)
        if identity not in seen_files:
            seen_files.add(identity)
            matches.append({"repository": f"github.com/{repository_name}", "file": file_path, "url": file_url})
    if not matches and not explicit_empty:
        raise ValueError("Grep MCP returned no recognizable search status.")
    return matches


def search_grep_app(term: str, ca_bundle_path: str | None = None) -> dict[str, Any]:
    """Search grep.app once for a dataset identifier and return matching files.

    The request times out after 30 seconds and the response is limited to 4 MiB.
    Matches use ASCII letter/digit boundaries: ``ATL03_007`` can match ``ATL03``
    while ``MATL03`` cannot. The service supplies a ranked sample, not a complete
    list of results or a way to request another page.

    Args:
        term: Dataset name or other literal identifier to find in public code.
        ca_bundle_path: Optional path to trusted certificate-authority
            certificates in PEM format for this HTTPS connection.

    Returns:
        A dictionary with repository/file/URL records in ``results``, the
        decoded service response in ``raw``, elapsed time, and warnings.
        ``status`` is ``limited`` on success because no pagination or total
        count is available, or ``error`` on a request or parsing failure.

    Raises:
        ValueError: If the search term is invalid.
    """

    validate_search_term(term)
    started_at = time.monotonic()
    query = f"(^|[^A-Za-z0-9]){re.escape(term)}($|[^A-Za-z0-9])"
    receipt: dict[str, Any] = {
        "provider": "grep.app", "term": term, "query": query, "status": "limited",
        "results": [], "raw": None, "warnings": [
            "Grep MCP returns a ranked sample with no pagination or total count; this is not an exhaustive search.",
        ],
    }
    request_body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "searchGitHub", "arguments": {"query": query, "useRegexp": True, "matchCase": False},
    }}
    request = Request(GREP_MCP_ENDPOINT, data=json.dumps(request_body).encode("utf-8"), headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18", "User-Agent": "nasa-eo-repository-discovery/0.1",
    })
    try:
        with urlopen(request, timeout=30, context=build_tls_context(ca_bundle_path)) as response:
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                raise ValueError("Grep response exceeded the 4 MiB response limit.")
            receipt["raw"] = decode_grep_app_response(response_body, response.headers.get("Content-Type", ""))
        receipt["results"] = parse_grep_search_results(receipt["raw"])
    except HTTPError as error:
        receipt["status"] = "error"
        receipt["warnings"].append(f"Grep MCP returned HTTP {error.code}; no challenge or access check was bypassed.")
    except (URLError, OSError, ValueError, RuntimeError) as error:
        receipt["status"] = "error"
        receipt["warnings"].append(f"Grep MCP request or response failed ({type(error).__name__}).")
    receipt["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
    return receipt


def resolve_public_repository_commit(repository: str) -> str:
    """Look up a public repository's current default-branch commit on GitHub.

    First request repository metadata and reject private or unknown visibility.
    Then request the default branch's current commit. Callers use that commit
    identifier in ZIP download URLs so later branch changes do not change the
    source files being scanned.

    Args:
        repository: ``owner/repository`` or ``github.com/owner/repository``.

    Returns:
        The lowercase 40-character Git commit hash.

    Raises:
        ValueError: If the repository name is unsafe.
        RuntimeError: If visibility is not explicitly public or resolution fails.
    """

    repository_name = validate_repository_name(repository)
    metadata = run_github_json([
        "--method", "GET", f"repos/{repository_name}", "--jq",
        'if .private == false then {private, default_branch} else {private: true} end',
    ])
    if not isinstance(metadata, dict) or metadata.get("private") is not False:
        raise RuntimeError("Repository is not confirmed public; download refused.")
    branch = metadata.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise RuntimeError("Repository has no default branch to resolve.")
    commit = run_github_json([
        "--method", "GET", f"repos/{repository_name}/commits/{quote(branch, safe='')}",
        "--jq", "{sha}",
    ])
    if not isinstance(commit, dict) or not re.fullmatch(r"[0-9a-fA-F]{40}", str(commit.get("sha", ""))):
        raise RuntimeError("GitHub returned no valid immutable commit SHA.")
    return commit["sha"].lower()

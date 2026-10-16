"""Make bounded, public-only discovery requests for the provider comparison."""

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
    """Validate a GitHub repository name before constructing requests.

    Args:
        repository: Owner/repository or github.com/owner/repository identifier.

    Returns:
        The validated owner/repository identifier without a host prefix.

    Raises:
        ValueError: If the identifier contains another host or unsafe components.
    """

    repository_name = repository.removeprefix("github.com/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+", repository_name):
        raise ValueError("Expected a GitHub owner/repository name.")
    if repository_name.split("/", 1)[1] in {".", ".."}:
        raise ValueError("Invalid repository name.")
    return repository_name


def validate_search_term(term: str) -> None:
    """Reject empty, excessively long, or query-breaking discovery terms.

    Args:
        term: Literal NASA product signature to send to a search provider.

    Raises:
        ValueError: If the term cannot be represented by this bounded trial.
    """

    if not isinstance(term, str) or not term.strip() or len(term) > 256:
        raise ValueError("Search terms must contain 1 to 256 characters.")
    if any(character in term for character in ('"', "\n", "\r", "\x00")):
        raise ValueError("Search terms cannot contain quotes or control characters.")


def run_github_json(arguments: list[str]) -> Any:
    """Run a bounded GitHub CLI read without exposing credentials or stderr.

    Args:
        arguments: Arguments following ``gh api``; callers supply GET requests.

    Returns:
        Parsed JSON returned by GitHub CLI.

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
        raise RuntimeError("GitHub response exceeded the 4 MiB trial limit.")
    try:
        return json.loads(response.stdout)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("GitHub CLI returned invalid JSON.") from error


def search_github_code(term: str, max_pages: int = 1, *, start_page: int = 1) -> dict[str, Any]:
    """Discover public candidate files using GitHub's legacy code-search API.

    Calls are spaced at least 6.1 seconds apart within this Python process. The
    GitHub CLI manages authentication; no token is read by this module. Private
    and unknown-visibility results are discarded by CLI filtering and checked
    again before persistence. Search snippets are candidates, not usage proof.

    Args:
        term: Literal product signature; punctuation follows legacy API rules.
        max_pages: Maximum 100-result pages to request, between one and ten.
        start_page: First page to request, allowing a saved run to resume without
            fetching earlier pages. The requested range cannot exceed page ten.

    Returns:
        A receipt with provider, query, status, normalized results, sanitized
        public-only response pages, timing, and coverage warnings. Status is
        returned, capped, incomplete, or error; none means all GitHub was searched.

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


def decode_mcp_response(response_body: bytes, content_type: str) -> dict[str, Any]:
    """Decode a bounded Model Context Protocol JSON or event-stream response.

    Args:
        response_body: Complete HTTP body within the trial's response-size limit.
        content_type: HTTP Content-Type value.

    Returns:
        The JSON-RPC response envelope matching this module's request ID.

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
    """Extract validated repository/file locations from Grep's public text results.

    Args:
        envelope: JSON-RPC envelope returned by Grep's searchGitHub tool.

    Returns:
        Distinct public GitHub file locations; empty for an explicit no-results
        response. Snippet contents remain untrusted evidence in the raw receipt.

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
    """Run one bounded query through Grep's documented public MCP service.

    Args:
        term: Literal product signature, matched with ASCII alphanumeric
            boundaries so underscores separate identifiers but MATL03 does not.
        ca_bundle_path: Optional PEM certificate bundle for this HTTP request.

    Returns:
        A receipt with candidate files, raw response, timing, and warnings.
        Successful status is always limited because the MCP tool exposes no
        pagination or total count. Error receipts contain no credentials.

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
        "MCP-Protocol-Version": "2025-06-18", "User-Agent": "nasa-eo-provider-trial/0.1",
    })
    try:
        with urlopen(request, timeout=30, context=build_tls_context(ca_bundle_path)) as response:
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                raise ValueError("Grep response exceeded the 4 MiB trial limit.")
            receipt["raw"] = decode_mcp_response(response_body, response.headers.get("Content-Type", ""))
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
    """Resolve the current default-branch commit after verifying public visibility.

    Args:
        repository: GitHub repository identifier with an optional host prefix.

    Returns:
        The full commit SHA for a currently public repository's default branch.

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

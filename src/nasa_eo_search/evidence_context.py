"""Fetch bounded public code context and construct citation-ready evidence."""

import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from nasa_eo_search.evidence_inputs import identify_evidence
from nasa_eo_search.evidence_notebooks import decode_notebook_source_cells
from nasa_eo_search.evidence_packaging import package_evidence_blocks
from nasa_eo_search.sourcegraph import build_tls_context


MAX_DOWNLOAD_BYTES = 2_000_000
README_PATHS = ("README.md", "README.rst", "README", "readme.md", ".github/README.md")


class RejectEvidenceRedirects(HTTPRedirectHandler):
    """Keep public downloads and authenticated requests on their intended URLs."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        """Refuse redirects instead of forwarding requests or credentials.

        Args:
            request: Original urllib request.
            response: Redirect response stream.
            code: HTTP redirect status.
            message: HTTP reason text.
            headers: Response headers.
            new_url: Proposed destination, which is never requested.

        Returns:
            None, causing urllib to raise HTTPError for the redirect.
        """
        return None


def read_bounded_https(request: Request, timeout: float, ca_bundle_path: str | None) -> bytes:
    """Read a size-limited HTTPS response without redirects or automatic retries.

    Args:
        request: Request constructed by a trusted service-specific caller.
        timeout: Socket connection/read timeout in seconds.
        ca_bundle_path: Optional certificate authority bundle for verified TLS.

    Returns:
        At most MAX_DOWNLOAD_BYTES response bytes.

    Raises:
        OSError: For HTTP, connection, or timeout failures.
        ValueError: If the response exceeds the download limit.
        SourcegraphError: If the shared TLS configuration is invalid.
    """
    opener = build_opener(RejectEvidenceRedirects(), HTTPSHandler(context=build_tls_context(ca_bundle_path)))
    with opener.open(request, timeout=timeout) as response:
        response_bytes = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(response_bytes) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"Response exceeds the {MAX_DOWNLOAD_BYTES}-byte limit.")
    return response_bytes


def build_public_file_url(repository: str, commit: str | None, file_path: str) -> str:
    """Construct a public GitHub raw-file URL pinned to a full commit hash.

    Args:
        repository: Sourcegraph repository name, including github.com.
        commit: Full SHA-1 or SHA-256 commit identifier, never a moving branch.
        file_path: Repository-relative path with no traversal components.

    Returns:
        An HTTPS raw.githubusercontent.com URL with encoded path components.

    Raises:
        ValueError: If the host, repository, commit, or path is unsupported.
    """
    if (not re.fullmatch(r"github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository)
            or repository.split("/")[-1] in (".", "..")
            or not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit)
            or not file_path or "\\" in file_path
            or any(part in ("", ".", "..") for part in file_path.split("/"))
            or any(ord(character) < 32 for character in file_path)):
        raise ValueError("Context requires a public GitHub path and a full commit hash.")
    repository_path = repository.removeprefix("github.com/")
    return f"https://raw.githubusercontent.com/{repository_path}/{commit}/{quote(file_path, safe='/')}"


def fetch_public_file_text(
    repository: str, commit: str | None, file_path: str, cache: dict[str, Any],
    ca_bundle_path: str | None,
) -> dict[str, Any]:
    """Fetch a commit-pinned public file once, caching text and failure status.

    No GitHub or OpenAI credentials are sent. Missing, oversized, non-UTF-8, or
    inaccessible files remain explicit gaps; links in their contents are ignored.

    Args:
        repository: Sourcegraph repository name.
        commit: Full immutable commit hash.
        file_path: Repository-relative file path.
        cache: In-memory download cache shared across product groups.
        ca_bundle_path: Optional certificate authority bundle.

    Returns:
        Text and URL on success, or a concise error and optional HTTP status.
    """
    cache_key = identify_evidence([repository, commit, file_path])
    if cache_key not in cache:
        try:
            url = build_public_file_url(repository, commit, file_path)
            request = Request(url, headers={"User-Agent": "nasa-eo-evidence-classifier/0.1"})
            contents = read_bounded_https(request, 20.0, ca_bundle_path).decode("utf-8-sig")
            if "\x00" in contents:
                raise ValueError("File contains binary data.")
            cache[cache_key] = {"text": contents, "url": url}
        except HTTPError as error:
            cache[cache_key] = {"error": f"HTTP {error.code}", "http_status": error.code}
            error.close()
        except (OSError, URLError, ValueError) as error:
            cache[cache_key] = {"error": f"Context unavailable ({type(error).__name__})."}
    return cache[cache_key]


def make_line_evidence(file_record: dict[str, Any], lines: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    """Create stable citations, splitting very long lines without discarding text.

    Args:
        file_record: Repository, path, and commit metadata for the excerpt.
        lines: One-based line numbers and original text.
        kind: matching_line, surrounding_code, or readme_context.

    Returns:
        Citation objects with text fragments of at most 500 characters each.
        Fragments retain their original line number and character offset.
    """
    evidence = []
    for line in lines:
        line_text = line["text"]
        for offset in range(0, max(1, len(line_text)), 500):
            item = {
                "repository": file_record["repository"], "file": file_record["file"],
                "commit": file_record["commit"], "line_number": line["line_number"],
                "character_offset": offset, "text": line_text[offset:offset + 500], "kind": kind,
            }
            item["evidence_id"] = identify_evidence(item)
            evidence.append(item)
    return evidence


def enrich_evidence_group(
    group: dict[str, Any], cache: dict[str, Any], offline: bool,
    context_lines: int, ca_bundle_path: str | None,
) -> None:
    """Add saved matching lines, surrounding code, and bounded README excerpts.

    Matching lines are always retained, even if the fetched file disagrees. Read
    only windows around the saved matches; README context is the first 80 lines
    at each observed commit. Record missing/truncated context instead of guessing.

    Args:
        group: Repository/product group to enrich in place.
        cache: Download cache shared across groups.
        offline: Retain matching lines only, with no network requests.
        context_lines: Number of preceding/following lines per match.
        ca_bundle_path: Optional certificate authority bundle.
    """
    evidence_by_id = {}
    warnings = group["warnings"]
    group["notebook_cells"] = []
    group["notebook_decoded_lines"] = []
    group["evidence_gaps"] = []
    for file_record in group["files"]:
        evidence = make_line_evidence(file_record, file_record["matching_lines"], "matching_line")
        if offline:
            warnings.append("Offline preparation: surrounding code and README context were not fetched.")
        else:
            fetched = fetch_public_file_text(group["repository"], file_record["commit"], file_record["file"], cache, ca_bundle_path)
            if "error" in fetched:
                warnings.append(f"{file_record['file']}: {fetched['error']}")
            else:
                source_lines = fetched["text"].splitlines()
                matching_numbers = {line["line_number"] for line in file_record["matching_lines"]}
                selected_numbers = set()
                for line in file_record["matching_lines"]:
                    number = line["line_number"]
                    if number > len(source_lines) or source_lines[number - 1] != line["text"]:
                        warnings.append(f"{file_record['file']}:{number}: saved line differs from fetched context.")
                    selected_numbers.update(range(max(1, number - context_lines), min(len(source_lines), number + context_lines) + 1))
                context = [{"line_number": number, "text": source_lines[number - 1]}
                           for number in sorted(selected_numbers - matching_numbers)]
                evidence.extend(make_line_evidence(file_record, context, "surrounding_code"))
                if file_record["file"].lower().endswith(".ipynb"):
                    matching_lines_agree = all(
                        0 < line["line_number"] <= len(source_lines)
                        and source_lines[line["line_number"] - 1] == line["text"]
                        for line in file_record["matching_lines"]
                    )
                    if matching_lines_agree:
                        cells, decoded_lines, gaps = decode_notebook_source_cells(fetched["text"], file_record, evidence)
                        # Full selected cells may extend beyond the normal hit
                        # window. Retain their raw lines too, so decoded context
                        # fragments have their own source citations in the audit.
                        retained_numbers = {item["line_number"] for item in evidence}
                        cell_numbers = {number for cell in cells for number in range(cell["line_number"], cell["end_line"] + 1)}
                        evidence.extend(make_line_evidence(file_record, [
                            {"line_number": number, "text": source_lines[number - 1]}
                            for number in sorted(cell_numbers - retained_numbers)
                        ], "surrounding_code"))
                        for cell in cells:
                            cell["source_ids"] = [item["evidence_id"] for item in evidence
                                                  if cell["line_number"] <= item["line_number"] <= cell["end_line"]]
                            for source_mapping in cell["source_line_map"]:
                                source_mapping["source_ids"] = [item["evidence_id"] for item in evidence
                                                                if source_mapping["line_number"] <= item["line_number"] <= source_mapping["end_line"]]
                        group["notebook_cells"].extend(cells)
                        group["notebook_decoded_lines"].append({
                            "file": file_record["file"], "commit": file_record["commit"],
                            "line_numbers": sorted(decoded_lines),
                        })
                        group["evidence_gaps"].extend(gaps)
                    else:
                        group["evidence_gaps"].append(
                            f"{file_record['file']}: differing saved/fetched notebook lines prevent reliable cell decoding."
                        )
        for item in evidence:
            evidence_by_id[item["evidence_id"]] = item
    if not offline:
        for commit in sorted({record["commit"] or "" for record in group["files"]}):
            found_readme = False
            for readme_path in README_PATHS:
                fetched = fetch_public_file_text(group["repository"], commit, readme_path, cache, ca_bundle_path)
                if "text" in fetched:
                    source_lines = fetched["text"].splitlines()
                    readme_record = {"repository": group["repository"], "commit": commit, "file": readme_path}
                    readme_lines = [{"line_number": number, "text": text}
                                    for number, text in enumerate(source_lines[:80], start=1)]
                    for item in make_line_evidence(readme_record, readme_lines, "readme_context"):
                        evidence_by_id[item["evidence_id"]] = item
                    if len(source_lines) > 80:
                        warnings.append(f"README context at {commit} is limited to its first 80 lines.")
                    found_readme = True
                    break
                if fetched.get("http_status") != 404:
                    break
            if not found_readme:
                warnings.append(f"README context unavailable at {commit or 'unknown commit'}.")
    group["warnings"] = sorted(set(warnings))
    group["evidence"] = sorted(evidence_by_id.values(), key=lambda item: (
        item["file"], item["commit"] or "", item["line_number"], item["character_offset"], item["kind"],
    ))


def build_evidence_packets(group: dict[str, Any], max_packet_bytes: int) -> list[dict[str, Any]]:
    """Package matched code blocks with concise product and repository context.

    Raw line evidence and complete catalog metadata remain unchanged in the
    group's audit. Version-two packets use short citation IDs, readable notebook
    text, and deduplicated blocks. README background accompanies matches and does
    not create independent classification requests. Archived evidence can be
    repackaged without network access or modifying its original saved files.

    Args:
        group: Enriched repository/product group.
        max_packet_bytes: Maximum UTF-8 JSON bytes per model input packet.

    Returns:
        Ordered packets; an empty list for groups without usable matching lines.

    Raises:
        ValueError: If metadata or locations cannot fit; no matches are dropped.
    """
    return package_evidence_blocks(group, max_packet_bytes)

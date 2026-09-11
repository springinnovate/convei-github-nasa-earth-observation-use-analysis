"""Find the first public repository reference to a delimited data product term."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
import re
import sys
from typing import Any, Iterable, TextIO
from urllib.parse import quote, urlencode

from nasa_eo_search.sourcegraph import (
    DEFAULT_SOURCEGRAPH_ENDPOINT,
    ServerSentEvent,
    SourcegraphError,
    SourcegraphProtocolError,
    decode_sourcegraph_event_payload,
    stream_sourcegraph_search_events,
)


def build_preview_query(search_phrase: str, all_hosts: bool) -> str:
    """Build a delimited public-content search limited to one result.

    Args:
        search_phrase: Literal product identifier or phrase to find in contents.
            Adjacent ASCII letters or digits prevent a match. Underscores and
            punctuation count as delimiters, allowing product filenames.
        all_hosts: Include every public code host indexed by Sourcegraph.

    Returns:
        A case-insensitive Sourcegraph regular expression query with a 15-second
        server search timeout. User text is escaped, not interpreted as regex.
    """

    # RE2 does not support lookbehind. Consume delimiters on either side instead.
    # Quoting content makes Sourcegraph treat the regex as literal text. Encode
    # whitespace and quotes so the pattern remains one unquoted query parameter.
    escaped_phrase = "".join(
        rf"\x{{{ord(character):x}}}" if character.isspace() or character in "\"'"
        else re.escape(character)
        for character in search_phrase
    )
    bounded_pattern = r"(^|[^A-Za-z0-9])" + escaped_phrase + r"($|[^A-Za-z0-9])"
    host_filter = "" if all_hosts else r"repo:^github\.com/ "
    return (
        f'{host_filter}visibility:public type:file patternType:regexp case:no '
        f'content:{bounded_pattern} count:1 timeout:15s'
    )


def first_content_match(
    search_events: Iterable[ServerSentEvent], diagnostics_output: TextIO,
) -> dict[str, Any] | None:
    """Consume events only until the first matching file is received.

    Args:
        search_events: Sourcegraph events; the caller owns and closes the stream.
        diagnostics_output: Destination for server warnings and progress.

    Returns:
        The first content match, or None after a complete response with no match.

    Raises:
        SourcegraphProtocolError: If match data are malformed or the stream ends
            before a completion event.
        SourcegraphError: If Sourcegraph reports an error alert.
    """

    for search_event in search_events:
        event_payload = decode_sourcegraph_event_payload(search_event)
        if search_event.event_type == "matches":
            if not isinstance(event_payload, list):
                raise SourcegraphProtocolError("Sourcegraph matches must be a list.")
            for content_match in event_payload:
                if not isinstance(content_match, dict):
                    raise SourcegraphProtocolError("Sourcegraph match must be an object.")
                if content_match.get("type") == "content":
                    return content_match
        elif search_event.event_type == "alert":
            print(f"Sourcegraph alert: {json.dumps(event_payload)}", file=diagnostics_output)
            if isinstance(event_payload, dict) and event_payload.get("severity") == "error":
                raise SourcegraphError("Sourcegraph reported a search error.")
        elif search_event.event_type == "progress":
            print("Sourcegraph is searching...", file=diagnostics_output, flush=True)
            if isinstance(event_payload, dict) and event_payload.get("skipped"):
                print(
                    f"Search scope/limits: {json.dumps(event_payload['skipped'])}",
                    file=diagnostics_output,
                )
        elif search_event.event_type == "done":
            return None
    raise SourcegraphProtocolError("Sourcegraph stream ended before search completion.")


def summarize_repository_match(content_match: dict[str, Any]) -> dict[str, Any]:
    """Format a file match with enough evidence to inspect it on the code host.

    Args:
        content_match: A Sourcegraph content match with repository and path fields.

    Returns:
        Repository, stars, file, language, revision, and numbered matching lines.

    Raises:
        SourcegraphProtocolError: If required repository or path fields are missing.
    """

    repository_name = content_match.get("repository")
    file_path = content_match.get("path")
    if not isinstance(repository_name, str) or not isinstance(file_path, str):
        raise SourcegraphProtocolError("Match is missing its repository or file path.")
    commit_id = content_match.get("commit")
    repository_url = f"https://{repository_name}"
    file_url = None
    if repository_name.startswith("github.com/") and isinstance(commit_id, str):
        file_url = f"{repository_url}/blob/{quote(commit_id, safe='')}/{quote(file_path)}"
    matching_lines = [
        {"line_number": line_match["lineNumber"] + 1, "text": line_match.get("line", "")}
        for line_match in content_match.get("lineMatches", [])
        if isinstance(line_match, dict) and isinstance(line_match.get("lineNumber"), int)
    ]
    return {
        "repository": repository_name,
        "repository_url": repository_url,
        "stars": content_match.get("repoStars"),
        "file": file_path,
        "file_url": file_url,
        "language": content_match.get("language"),
        "commit": commit_id,
        "matching_lines": matching_lines,
        "interpretation": "Candidate reference; inspect the code to establish data use.",
    }


def main(argument_values: list[str] | None = None) -> int:
    """Search public repositories, print the first reference, and close the stream.

    Args:
        argument_values: Arguments without the executable name; defaults to sys.argv.

    Returns:
        Zero for a match, one for an error, three for no returned match, or 130
        for interruption. Argparse exits with two for invalid arguments.
    """

    argument_parser = argparse.ArgumentParser(
        prog="nasa-repo-preview",
        description="Find one public repository reference to a data product or phrase.",
    )
    argument_parser.add_argument(
        "phrase",
        help="delimited literal term, e.g. ATL03; excludes substrings like MATL03",
    )
    argument_parser.add_argument(
        "--all-hosts", action="store_true",
        help="include other public code hosts in Sourcegraph's index (default: GitHub)",
    )
    argument_parser.add_argument("--ca-bundle", help="optional PEM certificate authority bundle")
    parsed_arguments = argument_parser.parse_args(argument_values)
    search_phrase = parsed_arguments.phrase.strip()
    if not search_phrase or any(ord(character) < 32 for character in search_phrase):
        argument_parser.error("phrase must be nonempty and contain no control characters")
    search_query = build_preview_query(search_phrase, parsed_arguments.all_hosts)
    print(
        f"Searching public repositories via Sourcegraph for {search_phrase!r}...",
        file=sys.stderr, flush=True,
    )
    try:
        with closing(stream_sourcegraph_search_events(
            DEFAULT_SOURCEGRAPH_ENDPOINT, search_query,
            os.environ.get("SOURCEGRAPH_TOKEN"), 20.0,
            ca_bundle_path=parsed_arguments.ca_bundle,
        )) as search_events:
            content_match = first_content_match(search_events, sys.stderr)
        if content_match is None:
            print(
                "No file match returned by this search. Check scope/limits above.",
                file=sys.stderr,
            )
            return 3
        print(json.dumps({
            "query": search_query,
            "search_url": "https://sourcegraph.com/search?" + urlencode({"q": search_query}),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "match": summarize_repository_match(content_match),
        }, indent=2, ensure_ascii=True))
        print("Found one matching file; closed the search and finished.", file=sys.stderr)
    except SourcegraphError as search_error:
        print(f"nasa-repo-preview: {search_error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Repository search interrupted.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

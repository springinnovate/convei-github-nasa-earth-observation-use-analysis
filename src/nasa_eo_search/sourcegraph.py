"""Run one query against Sourcegraph's server-sent event search endpoint."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import socket
import ssl
import sys
from collections.abc import Iterable, Iterator
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import certifi


DEFAULT_ENDPOINT = "https://sourcegraph.com/.api/search/stream"
USER_AGENT = "nasa-eo-sourcegraph-search/0.1"


class SourcegraphError(RuntimeError):
    """Base error for a failed Sourcegraph search."""


class SourcegraphProtocolError(SourcegraphError):
    """Raised when Sourcegraph returns a malformed event stream."""


@dataclass(frozen=True)
class ServerSentEvent:
    """One decoded server-sent event before its JSON payload is parsed."""

    event: str
    data: str


def iter_sse(lines: Iterable[bytes | str]) -> Iterator[ServerSentEvent]:
    """Parse an iterable of lines using the server-sent events framing rules."""

    event_name = "message"
    data_lines: list[str] = []

    for raw_line in lines:
        if isinstance(raw_line, bytes):
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError as error:
                raise SourcegraphProtocolError(
                    "Sourcegraph returned non-UTF-8 event data"
                ) from error
        else:
            line = raw_line

        line = line.rstrip("\r\n")
        if not line:
            if data_lines:
                yield ServerSentEvent(event_name, "\n".join(data_lines))
            event_name = "message"
            data_lines = []
            continue

        if line.startswith(":"):
            continue

        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]

        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)

    if data_lines:
        yield ServerSentEvent(event_name, "\n".join(data_lines))


def decode_event(event: ServerSentEvent) -> Any:
    """Decode Sourcegraph's JSON event payload."""

    try:
        return json.loads(event.data)
    except json.JSONDecodeError as error:
        raise SourcegraphProtocolError(
            f"Sourcegraph event {event.event!r} did not contain valid JSON"
        ) from error


def build_request(endpoint: str, query: str, token: str | None) -> Request:
    """Build the single GET request used for a search."""

    separator = "&" if "?" in endpoint else "?"
    url = f"{endpoint}{separator}{urlencode({'q': query, 'v': 'V3'})}"
    headers = {
        "Accept": "text/event-stream",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"token {token}"
    return Request(url, headers=headers, method="GET")


def build_ssl_context(ca_bundle: str | None) -> ssl.SSLContext:
    """Create a TLS context without reading the platform certificate store."""

    bundle = ca_bundle or os.environ.get("SSL_CERT_FILE") or certifi.where()
    try:
        return ssl.create_default_context(cafile=bundle)
    except (OSError, ssl.SSLError) as error:
        raise SourcegraphError(
            f"Could not load the TLS CA bundle {bundle!r}: {error}"
        ) from error


def stream_search(
    endpoint: str,
    query: str,
    token: str | None,
    timeout: float,
    ca_bundle: str | None = None,
) -> Iterator[ServerSentEvent]:
    """Make one request and yield events as Sourcegraph sends them."""

    request = build_request(endpoint, query, token)
    context = build_ssl_context(ca_bundle)
    try:
        with urlopen(  # noqa: S310
            request, timeout=timeout, context=context
        ) as response:
            content_type = response.headers.get_content_type()
            if content_type != "text/event-stream":
                raise SourcegraphProtocolError(
                    "Sourcegraph returned "
                    f"{content_type!r}, expected 'text/event-stream'"
                )
            yield from iter_sse(response)
    except HTTPError as error:
        detail = error.read(500).decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise SourcegraphError(
            f"Sourcegraph returned HTTP {error.code}{suffix}"
        ) from error
    except (URLError, TimeoutError, socket.timeout) as error:
        reason = getattr(error, "reason", error)
        raise SourcegraphError(f"Could not reach Sourcegraph: {reason}") from error
    except ssl.SSLError as error:
        raise SourcegraphError(f"Sourcegraph TLS connection failed: {error}") from error


def emit_results(
    events: Iterable[ServerSentEvent],
    query: str,
    output: TextIO,
    diagnostics: TextIO,
    *,
    raw_events: bool = False,
    trace: bool = False,
) -> int:
    """Write JSONL results and return the number of flattened matches."""

    emitted_matches = 0
    reported_matches: int | None = None
    reported_repositories: int | None = None

    for event in events:
        payload = decode_event(event)

        if trace:
            print(f"sourcegraph event={event.event}", file=diagnostics)

        if raw_events:
            print(
                json.dumps(
                    {"event": event.event, "data": payload},
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                file=output,
            )
        elif event.event == "matches":
            if not isinstance(payload, list):
                raise SourcegraphProtocolError(
                    "Sourcegraph 'matches' event payload was not a list"
                )
            for match in payload:
                print(
                    json.dumps(
                        {"query": query, "match": match},
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                    file=output,
                )
                emitted_matches += 1

        if event.event == "progress" and isinstance(payload, dict):
            match_count = payload.get("matchCount")
            repository_count = payload.get("repositoriesCount")
            if isinstance(match_count, int):
                reported_matches = match_count
            if isinstance(repository_count, int):
                reported_repositories = repository_count

    summary_matches = reported_matches if reported_matches is not None else emitted_matches
    repository_text = (
        f" repositories={reported_repositories}"
        if reported_repositories is not None
        else ""
    )
    print(
        f"sourcegraph complete matches={summary_matches}{repository_text} "
        f"records_written={emitted_matches if not raw_events else 'raw'}",
        file=diagnostics,
    )
    return emitted_matches


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sourcegraph-search",
        description="Send one query to Sourcegraph's streaming search API.",
    )
    parser.add_argument(
        "query",
        help="raw Sourcegraph query, including filters such as count:100",
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"stream API endpoint (default: {DEFAULT_ENDPOINT})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="connection/read timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--ca-bundle",
        metavar="PATH",
        help="PEM CA bundle (default: SSL_CERT_FILE or Certifi)",
    )
    parser.add_argument(
        "--token-env",
        default="SOURCEGRAPH_TOKEN",
        metavar="NAME",
        help="environment variable containing an optional token",
    )
    parser.add_argument(
        "--raw-events",
        action="store_true",
        help="write every decoded SSE event instead of flattened matches",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="write each received event type to standard error",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    if args.timeout <= 0:
        print("sourcegraph-search: --timeout must be greater than zero", file=sys.stderr)
        return 2

    token = os.environ.get(args.token_env)
    try:
        events = stream_search(
            args.endpoint,
            args.query,
            token,
            args.timeout,
            ca_bundle=args.ca_bundle,
        )
        emit_results(
            events,
            args.query,
            sys.stdout,
            sys.stderr,
            raw_events=args.raw_events,
            trace=args.trace,
        )
    except SourcegraphError as error:
        print(f"sourcegraph-search: {error}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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


DEFAULT_SOURCEGRAPH_ENDPOINT = "https://sourcegraph.com/.api/search/stream"
SOURCEGRAPH_CLIENT_USER_AGENT = "nasa-eo-sourcegraph-search/0.1"


class SourcegraphError(RuntimeError):
    """Base error for a failed Sourcegraph search."""


class SourcegraphProtocolError(SourcegraphError):
    """Raised when Sourcegraph returns a malformed event stream."""


@dataclass(frozen=True)
class ServerSentEvent:
    """One decoded server-sent event before its JSON payload is parsed.

    Attributes:
        event_type: Name supplied by the server-sent event ``event`` field.
        payload_text: Text assembled from the event's one or more ``data`` fields.
    """

    event_type: str
    payload_text: str


def parse_server_sent_events(
    response_lines: Iterable[bytes | str],
) -> Iterator[ServerSentEvent]:
    """Parse response lines that use server-sent event framing.

    Args:
        response_lines: Binary or text lines read from an HTTP event-stream response.

    Yields:
        Each complete server-sent event found in ``response_lines``.

    Raises:
        SourcegraphProtocolError: If a binary response line is not valid UTF-8.
    """

    event_name = "message"
    event_payload_lines: list[str] = []

    for response_line in response_lines:
        if isinstance(response_line, bytes):
            try:
                decoded_line = response_line.decode("utf-8")
            except UnicodeDecodeError as unicode_decode_error:
                raise SourcegraphProtocolError(
                    "Sourcegraph returned non-UTF-8 event data"
                ) from unicode_decode_error
        else:
            decoded_line = response_line

        decoded_line = decoded_line.rstrip("\r\n")
        if not decoded_line:
            if event_payload_lines:
                yield ServerSentEvent(
                    event_name, "\n".join(event_payload_lines)
                )
            event_name = "message"
            event_payload_lines = []
            continue

        if decoded_line.startswith(":"):
            continue

        field_name, field_separator, field_value = decoded_line.partition(":")
        if field_separator and field_value.startswith(" "):
            field_value = field_value[1:]

        if field_name == "event":
            event_name = field_value
        elif field_name == "data":
            event_payload_lines.append(field_value)

    if event_payload_lines:
        yield ServerSentEvent(event_name, "\n".join(event_payload_lines))


def decode_sourcegraph_event_payload(server_sent_event: ServerSentEvent) -> Any:
    """Decode the JSON payload carried by a Sourcegraph event.

    Args:
        server_sent_event: Event whose payload text contains Sourcegraph JSON.

    Returns:
        The Python value represented by the event's JSON payload.

    Raises:
        SourcegraphProtocolError: If the event payload is not valid JSON.
    """

    try:
        return json.loads(server_sent_event.payload_text)
    except json.JSONDecodeError as json_decode_error:
        raise SourcegraphProtocolError(
            "Sourcegraph event "
            f"{server_sent_event.event_type!r} did not contain valid JSON"
        ) from json_decode_error


def build_sourcegraph_search_request(
    endpoint_url: str,
    search_query: str,
    access_token: str | None,
) -> Request:
    """Build the single HTTP GET request used for a Sourcegraph search.

    Args:
        endpoint_url: URL of the Sourcegraph Stream API endpoint.
        search_query: Raw Sourcegraph query, including any query filters.
        access_token: Optional Sourcegraph access token.

    Returns:
        A configured request for the Sourcegraph Stream API.
    """

    query_separator = "&" if "?" in endpoint_url else "?"
    encoded_query_parameters = urlencode({"q": search_query, "v": "V3"})
    request_url = f"{endpoint_url}{query_separator}{encoded_query_parameters}"
    request_headers = {
        "Accept": "text/event-stream",
        "User-Agent": SOURCEGRAPH_CLIENT_USER_AGENT,
    }
    if access_token:
        request_headers["Authorization"] = f"token {access_token}"
    return Request(request_url, headers=request_headers, method="GET")


def build_tls_context(ca_bundle_path: str | None) -> ssl.SSLContext:
    """Create a TLS context without reading the platform certificate store.

    Args:
        ca_bundle_path: Optional path to a PEM certificate authority bundle.

    Returns:
        A client TLS context configured with the selected CA bundle.

    Raises:
        SourcegraphError: If the selected CA bundle cannot be loaded.
    """

    resolved_ca_bundle_path = (
        ca_bundle_path or os.environ.get("SSL_CERT_FILE") or certifi.where()
    )
    try:
        return ssl.create_default_context(cafile=resolved_ca_bundle_path)
    except (OSError, ssl.SSLError) as tls_configuration_error:
        raise SourcegraphError(
            "Could not load the TLS CA bundle "
            f"{resolved_ca_bundle_path!r}: {tls_configuration_error}"
        ) from tls_configuration_error


def stream_sourcegraph_search_events(
    endpoint_url: str,
    search_query: str,
    access_token: str | None,
    timeout_seconds: float,
    ca_bundle_path: str | None = None,
) -> Iterator[ServerSentEvent]:
    """Send one search request and stream Sourcegraph's response events.

    Args:
        endpoint_url: URL of the Sourcegraph Stream API endpoint.
        search_query: Raw Sourcegraph query, including any query filters.
        access_token: Optional Sourcegraph access token.
        timeout_seconds: Socket connection and read timeout in seconds.
        ca_bundle_path: Optional path to a PEM certificate authority bundle.

    Yields:
        Parsed events as Sourcegraph sends them.

    Raises:
        SourcegraphError: If the request, TLS connection, or HTTP response fails.
        SourcegraphProtocolError: If the response is not a server-sent event stream.
    """

    search_request = build_sourcegraph_search_request(
        endpoint_url, search_query, access_token
    )
    tls_context = build_tls_context(ca_bundle_path)
    try:
        with urlopen(  # noqa: S310
            search_request, timeout=timeout_seconds, context=tls_context
        ) as http_response:
            response_content_type = http_response.headers.get_content_type()
            if response_content_type != "text/event-stream":
                raise SourcegraphProtocolError(
                    "Sourcegraph returned "
                    f"{response_content_type!r}, expected 'text/event-stream'"
                )
            yield from parse_server_sent_events(http_response)
    except HTTPError as http_error:
        response_detail = http_error.read(500).decode(
            "utf-8", errors="replace"
        ).strip()
        response_detail_suffix = (
            f": {response_detail}" if response_detail else ""
        )
        raise SourcegraphError(
            f"Sourcegraph returned HTTP {http_error.code}{response_detail_suffix}"
        ) from http_error
    except (URLError, TimeoutError, socket.timeout) as connection_error:
        connection_failure_reason = getattr(
            connection_error, "reason", connection_error
        )
        raise SourcegraphError(
            f"Could not reach Sourcegraph: {connection_failure_reason}"
        ) from connection_error
    except ssl.SSLError as tls_connection_error:
        raise SourcegraphError(
            f"Sourcegraph TLS connection failed: {tls_connection_error}"
        ) from tls_connection_error


def write_search_results(
    search_events: Iterable[ServerSentEvent],
    search_query: str,
    results_output: TextIO,
    diagnostics_output: TextIO,
    *,
    write_raw_events: bool = False,
    trace_events: bool = False,
) -> int:
    """Write Sourcegraph search events as JSON Lines and print a summary.

    Args:
        search_events: Parsed events from a Sourcegraph search response.
        search_query: Query to include in each flattened match record.
        results_output: Text stream that receives JSON Lines records.
        diagnostics_output: Text stream that receives trace and summary messages.
        write_raw_events: Write all event types instead of flattened match records.
        trace_events: Write each received event type to ``diagnostics_output``.

    Returns:
        The number of flattened match records written. Raw event mode returns zero.

    Raises:
        SourcegraphProtocolError: If an event has malformed JSON or a ``matches``
            payload is not a list.
    """

    written_match_records = 0
    sourcegraph_match_count: int | None = None
    sourcegraph_repository_count: int | None = None

    for search_event in search_events:
        decoded_event_payload = decode_sourcegraph_event_payload(search_event)

        if trace_events:
            print(
                f"sourcegraph event={search_event.event_type}",
                file=diagnostics_output,
            )

        if write_raw_events:
            print(
                json.dumps(
                    {
                        "event": search_event.event_type,
                        "data": decoded_event_payload,
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                file=results_output,
            )
        elif search_event.event_type == "matches":
            if not isinstance(decoded_event_payload, list):
                raise SourcegraphProtocolError(
                    "Sourcegraph 'matches' event payload was not a list"
                )
            for search_match in decoded_event_payload:
                print(
                    json.dumps(
                        {"query": search_query, "match": search_match},
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                    file=results_output,
                )
                written_match_records += 1

        if search_event.event_type == "progress" and isinstance(
            decoded_event_payload, dict
        ):
            reported_match_count = decoded_event_payload.get("matchCount")
            reported_repository_count = decoded_event_payload.get(
                "repositoriesCount"
            )
            if isinstance(reported_match_count, int):
                sourcegraph_match_count = reported_match_count
            if isinstance(reported_repository_count, int):
                sourcegraph_repository_count = reported_repository_count

    summary_match_count = (
        sourcegraph_match_count
        if sourcegraph_match_count is not None
        else written_match_records
    )
    repository_summary = (
        f" repositories={sourcegraph_repository_count}"
        if sourcegraph_repository_count is not None
        else ""
    )
    print(
        f"sourcegraph complete matches={summary_match_count}{repository_summary} "
        "records_written="
        f"{written_match_records if not write_raw_events else 'raw'}",
        file=diagnostics_output,
    )
    return written_match_records


def create_command_line_parser() -> argparse.ArgumentParser:
    """Create the parser for ``sourcegraph-search`` command-line arguments.

    Returns:
        The configured command-line argument parser.
    """

    argument_parser = argparse.ArgumentParser(
        prog="sourcegraph-search",
        description="Send one query to Sourcegraph's streaming search API.",
    )
    argument_parser.add_argument(
        "query",
        help="raw Sourcegraph query, including filters such as count:100",
    )
    argument_parser.add_argument(
        "--endpoint",
        default=DEFAULT_SOURCEGRAPH_ENDPOINT,
        help=f"stream API endpoint (default: {DEFAULT_SOURCEGRAPH_ENDPOINT})",
    )
    argument_parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="connection/read timeout in seconds (default: 60)",
    )
    argument_parser.add_argument(
        "--ca-bundle",
        metavar="PATH",
        help="PEM CA bundle (default: SSL_CERT_FILE or Certifi)",
    )
    argument_parser.add_argument(
        "--token-env",
        default="SOURCEGRAPH_TOKEN",
        metavar="NAME",
        help="environment variable containing an optional token",
    )
    argument_parser.add_argument(
        "--raw-events",
        action="store_true",
        help="write every decoded SSE event instead of flattened matches",
    )
    argument_parser.add_argument(
        "--trace",
        action="store_true",
        help="write each received event type to standard error",
    )
    return argument_parser


def main(argument_values: list[str] | None = None) -> int:
    """Run one Sourcegraph search from command-line arguments.

    Args:
        argument_values: Optional argument list without the executable name. Uses
            ``sys.argv`` when omitted.

    Returns:
        Zero on success, one for a Sourcegraph failure, or two for invalid input.
    """

    parsed_arguments = create_command_line_parser().parse_args(argument_values)
    if parsed_arguments.timeout <= 0:
        print(
            "sourcegraph-search: --timeout must be greater than zero",
            file=sys.stderr,
        )
        return 2

    access_token = os.environ.get(parsed_arguments.token_env)
    try:
        search_events = stream_sourcegraph_search_events(
            parsed_arguments.endpoint,
            parsed_arguments.query,
            access_token,
            parsed_arguments.timeout,
            ca_bundle_path=parsed_arguments.ca_bundle,
        )
        write_search_results(
            search_events,
            parsed_arguments.query,
            sys.stdout,
            sys.stderr,
            write_raw_events=parsed_arguments.raw_events,
            trace_events=parsed_arguments.trace,
        )
    except SourcegraphError as sourcegraph_error:
        print(f"sourcegraph-search: {sourcegraph_error}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

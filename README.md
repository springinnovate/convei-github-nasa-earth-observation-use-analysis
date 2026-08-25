# NASA Earth Observation use analysis

This repository is beginning with a deliberately small tracer: a command-line
client that sends one query to Sourcegraph's public streaming search API and
writes the matching code records as JSON Lines.

## Install

Python 3.10 or newer is required. Certifi is the only runtime dependency.

```console
python -m pip install -e .
```

## Run one search

The positional argument is a raw Sourcegraph query. Quote it so your shell
passes it as one argument.

```console
sourcegraph-search "earthdata.nasa.gov count:10"
```

Each line written to standard output is an independent JSON object:

```json
{"query":"earthdata.nasa.gov count:10","match":{"type":"content","path":"example.py","repository":"github.com/example/project"}}
```

Progress and the final match count go to standard error, so results can be
redirected without mixing in status messages:

```console
sourcegraph-search "earthaccess.search_data count:all" > matches.jsonl
```

Sourcegraph queries can include its normal filters and pattern syntax. For
example:

```console
sourcegraph-search "earthdata.nasa.gov lang:Python fork:no archived:no count:100"
```

The command sends exactly one HTTP request. It does not generate related
queries, clone repositories, or verify that a match really uses a NASA dataset.
Those are later pipeline stages.

### Trace the event stream

Use `--trace` to print each Sourcegraph event type to standard error:

```console
sourcegraph-search --trace "HLSL30 count:10"
```

Use `--raw-events` when debugging the API itself. In this mode, every decoded
server-sent event is written to standard output instead of flattening only the
matches:

```console
sourcegraph-search --raw-events "ATL03 count:10"
```

The public Sourcegraph instance does not require a token. If a token is needed,
put it in `SOURCEGRAPH_TOKEN`; it is deliberately not accepted as a command-line
argument so it does not appear in shell history. A self-hosted endpoint can be
selected with `--endpoint`.

### TLS certificates

The client uses Certifi's curated CA bundle instead of the Windows certificate
store. This avoids certificate-store parsing failures seen with some Conda
Python installations on Windows.

If a network proxy requires a private CA, supply its PEM bundle explicitly:

```console
sourcegraph-search --ca-bundle company-ca.pem "earthdata.nasa.gov count:10"
```

The `SSL_CERT_FILE` environment variable is also honored when `--ca-bundle` is
not supplied.

## Test

```console
python -m unittest discover -s tests -v
```

Tests use recorded in-memory event streams and make no network requests.

## Current boundary

This tracer establishes the transport and output contract for one search. It
does not yet include:

- a NASA Common Metadata Repository catalog;
- generated aliases or product identifiers;
- multiple-query orchestration;
- repository checkout and match verification;
- persistence, aggregation, or scoring.

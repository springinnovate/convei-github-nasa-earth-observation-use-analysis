# NASA data references in public code

Command-line tools for finding NASA product references in public repositories
through Sourcegraph and looking up product identifiers in NASA's catalog.

## Install

Use Python 3.10 or newer. From the repository directory, run:

```console
python -m pip install -e .
```

Run this command again after pulling changes that add command-line tools.

## Collect repository matches

Search for a product identifier and save the results:

```console
nasa-repo-search ATL03 --output atl03-search
```

Choose a new output directory for each run. It contains:

| File | Contents |
| --- | --- |
| `matches.jsonl` | One JSON object per returned match, including the query, repository, file, revision, and matching lines. |
| `events.jsonl` | Search progress, warnings, and completion messages from Sourcegraph. |
| `summary.json` | Query, run times, saved-record count, number of repositories with matches, and completion status. |

Matches are written as they arrive. The terminal shows the number of saved
records and distinct repositories containing matches.

The search includes public GitHub repositories, forks, and archived repositories
in Sourcegraph's index. Add `--all-hosts` to include other indexed public hosts:

```console
nasa-repo-search ATL03 --all-hosts --output atl03-all-hosts
```

Searches are case-insensitive. Product terms are matched at line boundaries or
separators such as spaces, quotes, underscores, dots, slashes, and hyphens.
For example, `ATL03` matches `ATL03_007`, `ATL03.h5`, and `test_atl03`.
Punctuation within the search phrase is treated literally.

The command requests all results using Sourcegraph's `count:all` option. Coverage
and completion apply to Sourcegraph's indexed content. A matching reference
needs review in its code context to determine how the product is used.

### Run status

Check `summary.json` after a search:

| Status | Meaning | Exit code |
| --- | --- | --- |
| `finished_no_reported_limits` | Sourcegraph sent final progress and completion messages with an empty warning list. | 0 |
| `incomplete` | The run received warnings or ended before both completion messages arrived. Inspect `events.jsonl`. | 4 |
| `failed` | A connection, response, or file error stopped collection. Previously saved matches remain available. | 1 |
| `interrupted` | The search was interrupted with Ctrl+C. Previously saved matches remain available. | 130 |
| `running` | Collection is active, or the process stopped before writing its final summary. | — |

Server warnings are retained throughout the run. The server search timeout is
60 seconds; the socket timeout allows 65 seconds for a connection or read.

## Preview one repository match

Display the first matching file:

```console
nasa-repo-preview ATL03
nasa-repo-preview "earthdata.nasa.gov" --all-hosts
```

The JSON output includes the repository URL, stars when available, file path,
language, commit, and matching lines. GitHub matches include a link to the file
at the reported revision. The command exits after the first file arrives.
Its default scope excludes forks and archived repositories.

Save the preview by redirecting standard output:

```console
nasa-repo-preview ATL03 > first-repository.json
```

Progress and errors go to standard error. Exit code 3 means the search returned
no matching file. The server timeout is 15 seconds and the socket timeout is
20 seconds.

## Look up a NASA product

```console
nasa-product-preview --short-name ATL03
```

This queries NASA's Common Metadata Repository (CMR) for one collection tagged
for the Earth Observing System Data and Information System (EOSDIS). A collection
describes a dataset and version. The output contains its title, description,
version, provider, and identifiers that can be used as code-search terms.
Each term records the catalog field it came from.

Omit `--short-name` to return the first collection selected by CMR. Use
`--timeout 10` to set the connection/read timeout in seconds; the default is 20.
Exit code 3 means the catalog returned no matching collection.

## Run a custom Sourcegraph query

Use `sourcegraph-search` to supply filters and patterns directly:

```console
sourcegraph-search --trace "repo:^github.com/ ATL03 lang:Python count:100 timeout:30s"
```

Results are JSON Lines on standard output. `--trace` prints received event types
to standard error; `--raw-events` writes all decoded events to standard output.
See the [Sourcegraph query syntax](https://sourcegraph.com/docs/code-search/queries)
for available filters.

## Connection settings

The public services used by these commands accept unauthenticated requests.
For Sourcegraph authentication, set `SOURCEGRAPH_TOKEN`. The
`sourcegraph-search` command also accepts `--endpoint` for another instance.

HTTPS connections use Certifi's certificate authority bundle. If your network
requires a private certificate authority, provide its PEM file with
`--ca-bundle PATH` or the `SSL_CERT_FILE` environment variable.

If a command is missing from PATH, use the Python module in your installed
environment, for example:

```console
python -m nasa_eo_search.repo_search ATL03 --output atl03-search
```

The other modules are `nasa_eo_search.repo_preview`, `nasa_eo_search.cmr`, and
`nasa_eo_search.sourcegraph`. Run any command with `--help` for its options.

## Tests

```console
python -m unittest discover -s tests -v
```

The tests use recorded responses and mock network connections.

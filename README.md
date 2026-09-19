# NASA data references in public code

Command-line tools for finding NASA product references in public repositories
through Sourcegraph and looking up product identifiers in NASA's catalog.

## Install

Use Python 3.10 or newer. From the repository directory, run:

```console
python -m pip install -e .
```

Run this command again after pulling changes that add command-line tools.

## Collect the NASA product catalog

Save the public collections tagged for NASA's Earth Observing System Data and
Information System (EOSDIS) in the Common Metadata Repository (CMR):

```console
nasa-catalog-collect --output nasa-catalog
```

Choose a new output directory. The command retrieves successive catalog pages
and prints the collection and product counts as it runs.

| File | Contents |
| --- | --- |
| `collections.jsonl` | Original collection records, each with its request URL, retrieval time, and page number. Written as pages arrive. |
| `products.jsonl` | One product per provider and exact short name, with its collection versions, titles, descriptions, identifiers, and candidate search terms. Written when collection ends. |
| `summary.json` | Catalog scope, request URL, timestamps, reported totals, saved counts, warnings, and run status. |

JSON Lines (`.jsonl`) files contain one JSON object per line. In `products.jsonl`,
`collections` preserves the details of each version. `candidate_signatures`
contains terms drawn from short names, entry IDs, and collection concept IDs.
Each term has a `sources` list naming the collection and metadata field it came
from, plus a `review_status` of `unreviewed`.

Versions sharing the same provider and exact short name are grouped together;
identical search terms appear once within that group. Names from different
providers remain separate. Review the terms for ambiguity before using them in
repository searches, for example `nasa-repo-search ATL03 --output atl03-search`.

For a small trial run, filter to one product and request one record per page:

```console
nasa-catalog-collect --short-name ATL03 --page-size 1 --output catalog-atl03
```

`--page-size` accepts 1–2000 records (default: 500). `--timeout` sets the socket
connection/read timeout in seconds (default: 30). `--ca-bundle` uses the
certificate settings described below. All commands can also be launched as a
module: `python -m nasa_eo_search.catalog --output nasa-catalog`.

Check `summary.json` before using the catalog:

| Status | Meaning | Exit code |
| --- | --- | --- |
| `complete` | Pagination ended, reported totals stayed consistent, and the unique collection count matches the reported total. An empty result is also complete. | 0 |
| `incomplete` | Counts changed or disagreed, records repeated, or a pagination cursor repeated. See `warnings`. | 4 |
| `failed` | A request, response, or output-file error stopped the run. See `error`. | 1 |
| `interrupted` | Collection was interrupted with Ctrl+C. | 130 |
| `running` | Collection is active, or the process stopped before recording a final status. | — |

On a request failure or Ctrl+C, previously collected records and product groups
are retained. Rerun in a new directory to collect a fresh catalog. A file-system
failure may also prevent output files or the final summary from being written;
the command reports that error in the terminal.

Coverage is the public EOSDIS-tagged CMR catalog at retrieval time, with an
optional short-name filter. CMR is a live catalog: records can change during a
run. The command follows NASA's [Search After pagination](https://cmr.earthdata.nasa.gov/search/site/docs/search/api.html#search-after).

## Export the catalog to CSV

Create a spreadsheet-readable table from a saved catalog:

```console
nasa-catalog-export nasa-catalog --output nasa-products.csv
```

The command reads `products.jsonl` and `summary.json` from the catalog directory.
Choose a new output filename in an existing directory. Each product becomes one
CSV row; the terminal reports the exported count and the source catalog status.

Open the CSV in your spreadsheet application. In Excel, use **Data → From
Text/CSV**, select UTF-8 and a comma delimiter, and import identifiers such as
`short_name` as Text to preserve their spelling. Enable Wrap Text to view the
multiline cells.

The columns include:

- `provider`, `short_name`, and `collection_count` for filtering products.
- `collection_titles`, `collection_descriptions`, `collection_versions`,
  `collection_concept_ids`, and `collection_entry_ids`. Entries are numbered
  `[1]`, `[2]`, and so on in the original collection order. The same number refers
  to the same collection across these columns. A numbered blank means the value
  was absent from the catalog.
- `candidate_search_terms`, `candidate_review_statuses`, and `candidate_sources`.
  These use their own shared numbering. Each source identifies the collection
  concept ID and metadata field that supplied that term.
- `source_catalog_directory` and the `catalog_*` columns for source status,
  scope, request URL, run times, warnings, and errors.

Incomplete, failed, and interrupted catalogs can be exported when their saved
product count matches the file. Their status is printed as a warning and included
in every row. Wait for a `running` catalog to finish before exporting. A completed
empty catalog produces a header-only CSV, with its status reported in the terminal.

CSV fields are quoted, and Unicode, commas, quotation marks, and embedded newlines
are preserved. Formula-like cells receive a visible `Text: ` prefix to keep them
ordinary text; the original values remain in the JSON files. This addresses the
[formula-prefix risk described by OWASP](https://community.owasp.org/attacks/CSV_Injection).
Review spreadsheet import settings when moving the file between applications.
The exporter warns if a cell exceeds Excel's
[32,767-character limit](https://support.microsoft.com/en-us/excel/excel-specifications-and-limits)
and retains the complete text in the CSV.

Exit code 0 means export succeeded, including an export of a partial catalog.
Exit code 1 means invalid catalog data or a file error; 130 means interruption.
Validation finishes before the CSV is created. A write failure or interruption
removes the newly created partial export when the file system permits cleanup.
Existing output files and source catalog files are preserved.

The module equivalent is:

```console
python -m nasa_eo_search.catalog_csv nasa-catalog --output nasa-products.csv
```

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

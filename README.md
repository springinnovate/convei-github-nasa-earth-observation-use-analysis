# NASA data references in public code

Collect NASA product names and identifiers, export them to a spreadsheet, and
search public code for references to a product using Sourcegraph.

## Install

Use Python 3.10 or newer. From the repository directory, run:

```console
python -m pip install -e .
```

Run this command again after pulling changes that add command-line tools.

## Start here

Collect the catalog and export it for review:

```console
nasa-catalog-collect --output nasa-catalog
nasa-catalog-export nasa-catalog --output nasa-products.csv
```

Open `nasa-products.csv` in a spreadsheet to inspect products and their candidate
search terms. To search for a term you have selected, run:

```console
nasa-repo-search ATL03 --output atl03-search
```

Use a new output directory or CSV filename for each run. Run any command with
`--help` to see its options.

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

JSON Lines (`.jsonl`) files contain one JSON object per line. A collection is a
dataset version. Collections with the same provider and exact short name are
grouped into one product. Each product includes candidate search terms drawn
from its identifiers, with the source of each term recorded. Terms start with
the review status `unreviewed`; inspect them for ambiguity before searching.

For a small trial run, filter to one product and request one record per page:

```console
nasa-catalog-collect --short-name ATL03 --page-size 1 --output catalog-atl03
```

`--page-size` accepts 1–2000 records (default: 500). `--timeout` sets the socket
connection/read timeout in seconds (default: 30). `--ca-bundle` uses the
certificate settings described below.

Check `summary.json` before using the catalog:

| Status | Meaning | Exit code |
| --- | --- | --- |
| `complete` | Collection finished and the saved count matches NASA's reported total. | 0 |
| `incomplete` | Counts or page results were inconsistent. See `warnings`. | 4 |
| `failed` | A request, response, or output-file error stopped the run. See `error`. | 1 |
| `interrupted` | Collection was interrupted with Ctrl+C. | 130 |
| `running` | Collection is active, or the process stopped before recording a final status. | — |

After a connection failure or Ctrl+C, you can inspect the results saved so far.
Rerun in a new directory to collect a fresh catalog. Check terminal errors as
well as `summary.json`, especially after a file-writing failure.

Coverage is the public EOSDIS-tagged CMR catalog at retrieval time, with an
optional short-name filter. Records can change during collection; check the run
status before using the results.

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

Check `catalog_status` when reviewing the CSV. Exports of incomplete, failed, or
interrupted catalogs contain the products saved so far and carry a warning.
Wait for a `running` catalog to finish before exporting. An empty catalog
produces a CSV containing only column headings.

Some values have a visible `Text: ` prefix so spreadsheets treat them as text.
The original values are in `products.jsonl`. If the exporter reports a cell-size
warning, consult that file for the full value if your spreadsheet truncates it.

Exit code 0 means export succeeded, including an export of a partial catalog.
Exit code 1 means invalid catalog data or a file error; 130 means interruption.
For a count-mismatch or invalid-data error, check the source catalog's
`summary.json` and `products.jsonl`. For a file error, check the destination
directory and choose an unused output filename.

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

The command collects the results returned by Sourcegraph for its indexed
content. Follow the matching files to see how each product is used.

### Run status

Check `summary.json` after a search:

| Status | Meaning | Exit code |
| --- | --- | --- |
| `finished_no_reported_limits` | Sourcegraph reported completion with no search-limit warnings. | 0 |
| `incomplete` | The search returned warnings or ended before reporting completion. Inspect `events.jsonl`. | 4 |
| `failed` | A connection, response, or file error stopped collection. Previously saved matches remain available. | 1 |
| `interrupted` | The search was interrupted with Ctrl+C. Previously saved matches remain available. | 130 |
| `running` | Collection is active, or the process stopped before writing its final summary. | — |

The search timeout is 60 seconds; a connection or read can wait up to 65 seconds.

## Preview one repository match

Display the first matching file:

```console
nasa-repo-preview ATL03
nasa-repo-preview "earthdata.nasa.gov" --all-hosts
```

The JSON output includes the repository URL, stars when available, file path,
language, commit, and matching lines. GitHub matches include a link to the file
at the reported revision. The command exits after the first file arrives.
Its default scope is active, non-fork public repositories in Sourcegraph's index.

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

Use these module names with `python -m`, followed by the same command arguments:

| Command | Module |
| --- | --- |
| `nasa-catalog-collect` | `nasa_eo_search.catalog` |
| `nasa-catalog-export` | `nasa_eo_search.catalog_csv` |
| `nasa-repo-search` | `nasa_eo_search.repo_search` |
| `nasa-repo-preview` | `nasa_eo_search.repo_preview` |
| `nasa-product-preview` | `nasa_eo_search.cmr` |
| `sourcegraph-search` | `nasa_eo_search.sourcegraph` |

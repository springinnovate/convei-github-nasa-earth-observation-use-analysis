# NASA data references in public code

Collect NASA product names and identifiers, export them to a spreadsheet, and
search public code for references using Sourcegraph, and classify saved evidence
into a repository/product review spreadsheet.

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

## Search a selected group of products

Create a CSV containing the products you want to search. Use the exact `provider`
and `short_name` values from your catalog. For example, save this as `pilot.csv`:

```csv
provider,short_name,search_term
NSIDC_CPRD,ATL03,ATL03
NSIDC_CPRD,ATL06,ATL06
```

`search_term` is optional and defaults to the product's short name. Each term must
be one of that product's catalog candidates. You can also save selected rows from
the catalog export as a new CSV: extra columns are accepted. Add `search_term`
to choose an identifier other than the short name. Selecting a term records your
choice in the batch plan and preserves its catalog review status and sources.

Start the batch:

```console
nasa-repo-batch start nasa-catalog --selection pilot.csv --output pilot-search
```

The command validates the whole selection before searching. It defaults to at
most 20 distinct terms; use `--max-searches 50` to allow a larger selection.
Identical terms shared by products are searched once and linked to each selected
product. Searches run sequentially with a one-second pause between requests.
Progress shows the query number, term, attempt, and saved match counts.

The search covers public GitHub content in Sourcegraph's index, including forks
and archives. Add `--all-hosts` when starting a batch to include other indexed
public code hosts. Each query has the same timeouts and term matching as
`nasa-repo-search`. Inspect the matched code to establish product use.

### Inspect batch results

| File | Contents |
| --- | --- |
| `plan.json` | Selected terms, associated products and catalog sources, query scope, and source catalog status. |
| `searches.csv` | One row per product/query with the latest attempt's status, match count, warnings, and errors. Includes searches with zero matches. |
| `matches.csv` | Product-linked file matches, repository/owner, stars when available, file type, language, revision, matching text, and GitHub file links. |
| `summary.json` | Batch status, counts by query status, and CSV row counts. |
| `searches/<search-id>/attempt-000001/` | Original `matches.jsonl`, `events.jsonl`, and `summary.json` for each attempt. |

Reports refresh after each query; save a copy if you add your own review notes.
`matches.csv` retains matches from all attempts;
filter `latest_attempt` to `true` for the current results. Use `attempt_status` to
distinguish completed searches from partial results. Matching text includes
one-based line numbers. Multiple product associations and repeated attempts can
produce multiple rows for the same file. CSV fields use the same text protection
as the catalog export.

### Stop and resume

Press Ctrl+C to stop. Resume using the saved batch directory:

```console
nasa-repo-batch resume pilot-search
```

Resume uses `plan.json`, so the original catalog and selection CSV can be moved
after the batch starts. Verified completed searches are skipped. Pending,
incomplete, failed, or interrupted queries are searched again from the beginning,
once per resume, in new attempt directories. Earlier attempts remain available.
To change the selected terms or scope, start a new batch directory.

| Search status | Meaning |
| --- | --- |
| `pending` | The query has not started. |
| `finished_no_reported_limits` | Sourcegraph reported completion with no warnings, and saved evidence passed validation. |
| `incomplete` | Sourcegraph reported limits or ended before confirming completion. |
| `failed` | A request/file error occurred, or saved evidence was missing or damaged. See `error` in `searches.csv`. |
| `interrupted` | The search was stopped, or its previous process exited before finalizing the attempt. |

Batch exit codes are 0 when all queries completed, 4 for incomplete results,
1 for input/file errors or failed queries, and 130 for interruption. Check the
terminal and `summary.json` after each run. A file-writing failure can leave the
previous report snapshot in place. Close a CSV in your spreadsheet application
if it prevents a report from being updated, then resume the batch.

One process can run in a batch directory at a time. `batch.lock` is released by
the operating system when the process exits; the file stays in place for reuse.
Both subcommands accept `--ca-bundle PATH`; Sourcegraph authentication uses
`SOURCEGRAPH_TOKEN` as described below.

## Classify repository/product evidence

Use the saved batch directory and its source catalog to prepare evidence:

```console
nasa-evidence-classify prepare pilot-search --catalog nasa-catalog --output pilot-review
```

The input is the **batch directory**, containing `plan.json` and the original
search-attempt files. The command groups matches by repository, provider, and
product. Repeated matches across attempts and search terms contribute evidence
once, with their original sources retained. Older revisions remain distinguishable.
Products with no returned matches have no repository row; inspect the batch's
`searches.csv` for those products.

Preparation reads catalog titles, descriptions, versions, and identifiers. It
fetches public GitHub files at the saved commit and selects ten lines before and
after each match, plus the first 80 lines of an available README at that commit.
Downloads are capped at 2 MB and cached during preparation. Source URLs and code
are treated as data; only validated, commit-pinned GitHub raw-file URLs are fetched.
Missing context, unsupported hosts/revisions, and partial searches appear as
warnings. Your original catalog and search results stay unchanged.

Preparation makes no OpenAI calls and needs no API key. Add `--offline` to use
only the saved matching lines, without GitHub requests. Choose a new output
directory for each preparation. Interrupting preparation leaves the original
inputs available; run preparation again in a new directory.

Open `pilot-review/review.csv` to inspect the groups and warnings. `plan.json`
contains the exact evidence and planned request packets. Large groups are split
into packets of at most 24,000 UTF-8 JSON bytes; matching text is retained,
including fragments of unusually long lines. The catalog metadata is repeated
in each packet. For example, five repository/product groups can require more
than five API calls once code context is included.

Use `--context-lines 20` to change the surrounding-code window (0–100), or
`--packet-bytes 48000` to change the evidence packet cap (minimum 4096).
If the metadata and one fragment cannot fit, that group is marked `blocked`;
increase the packet cap and prepare a new directory. Packet size is measured in
bytes, not model tokens, and must fit the selected model's input limits.

### Preview API work and set your key

Choose an OpenAI model that supports the Responses API with
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).
Replace `YOUR_MODEL` in these examples with its model identifier:

```console
nasa-evidence-classify run pilot-review --model YOUR_MODEL --dry-run
```

This prints cached and eligible request counts, request JSON bytes, and the
maximum generated tokens for the next invocation. It makes no API calls, needs
no key, and does not change the prepared files. The byte count is not a token or
price estimate; API cost depends on the selected model and actual token usage.

Set `OPENAI_API_KEY` in the terminal environment. For Git Bash, enter it without
displaying it or putting its value in a shell command:

```bash
read -rsp "OpenAI API key: " OPENAI_API_KEY
export OPENAI_API_KEY
```

For PowerShell:

```powershell
$env:OPENAI_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'OpenAI API key' -AsSecureString)).Password
```

Keep the key out of project files and version control. Running classification
sends the prepared public-code excerpts and NASA metadata to OpenAI. The
credential is sent only to the OpenAI endpoint and is not written to receipts.
Requests use `store: false`; inspect your API project's data settings as well.

### Run and resume classification

Start with a small request limit:

```console
nasa-evidence-classify run pilot-review --model YOUR_MODEL --max-requests 5
```

Repeat the same command to continue. Successful saved responses are validated
and reused. `--max-requests` limits new calls **per invocation** (default: 20).
`--max-output-tokens` caps generated tokens per call, including reasoning tokens
(default: 2048; allowed: 256–32768). These are request and output-token limits,
not a dollar budget. The first run fixes the model and output-token cap for that
directory; supply the same settings on resume. Prepare a new directory to change
them or the evidence. Only one process may write to a review directory at a time.

A failed request stops the current invocation. Ordinary resume can process
remaining unstarted requests, but failed or uncertain requests require an explicit
retry after you inspect their saved response and error:

```console
nasa-evidence-classify run pilot-review --model YOUR_MODEL --retry-failed
```

The command writes a receipt before each call and saves the raw response before
validating it. A refused, incomplete, malformed, or invalidly cited response is
reported as a failure. Ctrl+C can leave a request with no saved response even if
OpenAI processed it; retrying may incur another charge. There are no automatic
network retries. Both subcommands accept `--ca-bundle PATH`.

### Review the classifications

Each row in `review.csv` assesses one repository/product pair:

| Category | Evidence interpretation |
| --- | --- |
| `direct_use` | Application or analysis code explicitly accesses or processes the product. |
| `supporting_software` | Tools, libraries, tests, fixtures, or examples demonstrate product support. |
| `reference_only` | Relevant prose or citations mention the product. |
| `unrelated` | The matching identifier means something else. |
| `unclear` | Evidence is ambiguous, conflicting, insufficient, or not fully processed. |

The CSV includes an explanation, supporting file/line links, `needs_review`,
source-search completeness, warnings, and processing status. The model must cite
supplied evidence identifiers; those identifiers are checked before accepting a
response. Check the cited code yourself: a valid citation and a consistent output
format do not prove that the interpretation is correct or that code was executed.

For split groups, relevant code evidence takes precedence over a prose mention.
Ambiguous matching evidence or a relevant/unrelated conflict produces `unclear`.
An unclear packet containing only supplementary context is neutral. Split groups,
context gaps, partial searches, and ambiguous decisions are flagged for review.

| Processing status | Meaning |
| --- | --- |
| `pending` | Some packets have not been classified, including after a request limit. |
| `complete` | Every packet has a saved, validated model response. |
| `failed` | A request failed validation or has an uncertain outcome. Inspect `error` and the receipt. |
| `blocked` | No usable matching lines, or metadata/evidence cannot fit the packet cap. |

`complete` describes model processing. Search coverage remains the public content
returned by Sourcegraph, and `search_complete` records whether the selected
product queries finished without reported limits. Neither status guarantees all
GitHub repositories were searched or that the model's judgment is correct.

| File | Contents |
| --- | --- |
| `review.csv` | One repository/product row, including blank `human_category` and `human_notes` columns. |
| `plan.json` | Frozen source metadata, original match provenance, numbered evidence fragments, and request packets. |
| `prompt.json` | Classification instructions and prompt version used by this preparation. |
| `settings.json` | Model and output-token cap, saved when classification starts. |
| `requests/<request-hash>/attempt-000001.json` | Credential-free request, timestamps, raw model response when available, validation result, and error. Retries get new numbered files. |
| `summary.json` | Group/request counts, processing status, recorded token usage across attempts, and attempts without usage data. |

Reports refresh during classification. **Save a copy of `review.csv` before adding
review notes**; subsequent runs regenerate it. Text fields use the same spreadsheet
protection as the catalog export. Original evidence remains in JSON if a spreadsheet
truncates a large cell. Prepared files are sufficient to resume after the source
catalog or batch is moved.

Exit code 0 means preparation/preview succeeded or classification processing
completed; 4 means pending or blocked work; 1 means invalid inputs, file errors,
or failed requests; 130 means interruption. Inspect terminal errors as well as
`summary.json`, especially if an open spreadsheet prevents report replacement.

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
| `nasa-repo-batch` | `nasa_eo_search.repo_batch` |
| `nasa-evidence-classify` | `nasa_eo_search.evidence_classify` |
| `nasa-repo-preview` | `nasa_eo_search.repo_preview` |
| `nasa-product-preview` | `nasa_eo_search.cmr` |
| `sourcegraph-search` | `nasa_eo_search.sourcegraph` |

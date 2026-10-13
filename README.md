# NASA data references in public code

Collect NASA product names and identifiers, export them to a spreadsheet, and
search public code for references using Sourcegraph, and classify saved evidence
into a repository/product review spreadsheet. Review the evidence yourself and
compare your judgments with the saved model results without additional API calls.

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
contains the original evidence, readable evidence blocks, and planned requests.
Nearby code stays together, and notebook source text is decoded into readable
excerpts with source locations. Identical blocks share their text while retaining
their source occurrences in the audit data. Repository and product descriptions
are compact; README background accompanies matches rather than getting its own
classification request. Any unavailable or omitted context remains explicit.

Groups that fit are sent together. Larger groups are split into packets of at
most 24,000 UTF-8 JSON bytes, retaining matched text and splitting oversized
blocks when necessary. Five repository/product groups can still require more
than five API calls. A request limit counts calls, not CSV rows.

Use `--context-lines 20` to change the surrounding-code window (0–100), or
`--packet-bytes 48000` to change the evidence packet cap (minimum 4096).
If the metadata and one fragment cannot fit, that group is marked `blocked`;
increase the packet cap and prepare a new directory. Packet size is measured in
bytes, not model tokens, and must fit the selected model's input limits.

### Reuse saved evidence with the current format

New preparations use format 2 and prompt `repository-product-v2`. To rebuild
inputs from an existing format-1 or format-2 preparation:

```console
nasa-evidence-classify repack pilot-review --output pilot-review-v2
nasa-evidence-classify run pilot-review-v2 --model YOUR_MODEL --dry-run
```

`repack` reads saved excerpts only: it makes no GitHub or OpenAI requests, needs
no key, and leaves the source directory unchanged. It preserves the original
catalog and match provenance, but cannot recover code that was never collected.
Choose a new output directory; `--packet-bytes` sets the same size cap as in
`prepare`.

Old CSVs and receipts remain available. The current `run` command requires a
format-2 preparation and will reject format 1 before reading a key or writing
files. Repacked inputs require new classifications: old paid responses are not
copied or treated as answers to the changed requests.

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

Create `secrets/openai.key` in the directory where you run the command. Save
**only your raw API key** in this UTF-8 text file: one line, without quotes,
JSON, or an `OPENAI_API_KEY=` prefix. A trailing newline is accepted. No
environment variable is required. To choose a different location explicitly:

```console
nasa-evidence-classify run pilot-review --model YOUR_MODEL --api-key-file ./secrets/openai.key --max-requests 5
```

Credentials are checked in this order:

1. The file selected with `--api-key-file PATH`.
2. A nonblank `OPENAI_API_KEY` environment variable, when no file is selected.
3. `./secrets/openai.key`, relative to the current working directory.

An explicitly selected file must be readable and valid; the command reports an
error instead of falling back to another credential. Files may contain at most
4096 bytes. Preparation, `--dry-run`, and fully cached runs do not read the key.
Key contents and the selected file path are not saved in plans or request receipts,
so you can change the credential location when resuming.

The repository ignores `secrets/`, `secretes/`, and `*.key` files in Git. The
file is still plaintext: restrict access to your account, keep it out of shared
folders, and never commit or paste the key into logs or issues. For a custom
filename outside those ignored paths, add your own ignore rule. Git ignore rules
do not protect files that were already tracked.

Running classification sends the prepared public-code excerpts and NASA metadata
to OpenAI. The credential is sent only to the OpenAI endpoint and is not written
to receipts.
Requests use `store: false`; inspect your API project's data settings as well.

### Run and resume classification

Start with a small request limit:

```console
nasa-evidence-classify run pilot-review --model YOUR_MODEL --max-requests 5
```

Repeat the same command to continue. Successful saved responses are validated
and reused. `--max-requests` limits new calls **per invocation** (default: 20).
Stopping at that limit can leave assessments pending even when every submitted
request succeeded. The terminal reports completed and pending assessments;
check `summary.json` before treating the whole run as finished.
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

The CSV includes one concise conclusion, up to three supporting file/line links,
evidence types, observed product versions, `needs_review`, and explicit review
reasons. Source-search completeness, context limitations, warnings, and processing
status remain separate. Classifications concern the product across versions:
an ATL03 version 003 example is evidence about ATL03, without establishing
compatibility with version 007. Observed versions require citations to code or
documentation excerpts, not just catalog metadata.

The model must cite
supplied evidence identifiers; those identifiers are checked before accepting a
response. Check the cited code yourself: a valid citation and a consistent output
format do not prove that the interpretation is correct or that code was executed.

For split groups, relevant code evidence takes precedence over a prose mention.
Ambiguous matching evidence or a relevant/unrelated conflict produces `unclear`.
An unclear packet containing only supplementary context is neutral. Multiple
packets or a bounded README excerpt alone do not force review. Missing matched
context, partial searches, conflicting evidence, and model-reported uncertainty
have explicit review reasons. `reference_only` describes the collected evidence;
it does not establish that the repository lacks a relevant implementation.

The concise report is assembled from validated packet judgments without an extra
model call. `audit.json` preserves all packet explanations and citations, including
qualifications that do not fit in the short CSV conclusion. Check that file when
reviewing a split or conflicting assessment.

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
| `plan.json` | Frozen full source metadata, original line evidence and match provenance, compact blocks with source mappings, and request packets. |
| `audit.json` | Full packet classifications, source citations, review reasons, and evidence limitations for each assessment. |
| `prompt.json` | Classification instructions and prompt version used by this preparation. |
| `settings.json` | Model and output-token cap, saved when classification starts. |
| `requests/<request-hash>/attempt-000001.json` | Credential-free request, timestamps, raw model response when available, validation result, and error. Retries get new numbered files. |
| `summary.json` | Group/request counts, processing status, recorded token usage across attempts, and attempts without usage data. |

Reports refresh during classification. Use the separate human-review workflow below
for persistent labels; the blank human columns in `review.csv` are regenerated and
are not imported automatically. Text fields use the same spreadsheet
protection as the catalog export. Original evidence remains in JSON if a spreadsheet
truncates a large cell. Prepared files are sufficient to resume after the source
catalog or batch is moved.

Exit code 0 means preparation/repacking/preview succeeded or classification processing
completed; 4 means pending or blocked work; 1 means invalid inputs, file errors,
or failed requests; 130 means interruption. Inspect terminal errors as well as
`summary.json`, especially if an open spreadsheet prevents report replacement.

## Review evidence and measure classification quality

`nasa-evidence-review` works entirely offline with saved results. It needs no API
key and makes no model calls or repository downloads. After installing the new
command with `python -m pip install -e .`, create a review from a format-2
classification directory, such as your completed `pilot-review-v3`:

```console
nasa-evidence-review prepare pilot-review-v3 --output pilot-human-review
```

Open `pilot-human-review/evidence.html` in your browser and
`pilot-human-review/human-labels.csv` in your spreadsheet application. The page
shows the collected evidence and pinned source links, with model conclusions
hidden. Each CSV row is one repository/product pair. The five existing pilot
assessments can be kept this way as regression examples: repeatable checks of
previous cases, not a new held-out test.

Fill in these columns; leave the repository, product, and identifier columns intact:

| Column | What to enter |
| --- | --- |
| `human_category` | One of the five categories above. Leave blank if you have not reviewed the pair. |
| `supporting_citation` | One evidence block ID, such as `E1`, or its exact pinned source URL from the page. Required for a category other than `unclear`. |
| `evidence_sufficiency` | `sufficient`, `insufficient`, or `uncertain`: is the supplied evidence enough to make your judgment? |
| `missing_context_reason` | What decisive context is missing. Required for insufficient or uncertain evidence. |
| `notes` | Optional reasoning or findings from inspecting the wider repository. |
| `review_minutes` | Optional time spent reviewing this pair, in minutes. Blank means unrecorded, not zero. |

Judge the supplied evidence before opening model conclusions. If you inspect the
wider repository and discover omitted context, record it in `notes` and
`missing_context_reason`. Keep that separate from your judgment of the supplied
excerpts. Insufficient or uncertain evidence must be labeled `unclear`, not
`unrelated`. A definitive category requires sufficient evidence and a citation.
These checks validate the form of a label; a person still needs to judge its meaning.

Save the labels as UTF-8 CSV, then compare:

```console
nasa-evidence-review compare pilot-human-review --output pilot-validation
```

Open `pilot-validation/metrics.html` for readable counts and rates, or
`comparison.csv` to inspect individual disagreements. `metrics.json` contains the
full breakdown. The command reads your labels but never rewrites them. It also
leaves the original classifications unchanged. Choose a new output directory
when calculating reports again after editing labels.

The comparison separates:

- Category agreement/disagreement among completed, reviewed pairs with definitive
  human and model categories. Model `unclear` is a separate abstention, not silently
  counted as a correct or incorrect definitive judgment.
- False use/support claims: model `direct_use` or `supporting_software` when the
  reviewer finds only `reference_only` or `unrelated`.
- Missed supported findings: the reverse situation. Human-supported cases where
  the model abstained are reported separately with the same denominator.
- Insufficient evidence, missing context, model review flags, and recorded human
  review time.
- Planned/completed requests, saved attempts, and recorded input/output tokens for
  the selected evidence. Attempts without recorded usage are counted; token totals
  are not dollar estimates.

Every rate has a numerator and denominator. Missing, stale, unreviewed, unfinished,
and unclear cases have explicit exclusion counts. A zero denominator means the
rate is unavailable, not zero percent. Small convenience samples describe those
examples, not classification accuracy across GitHub.

### Preserve labels and compare another saved run

The review directory freezes its evidence, baseline responses, and sample selection:

| File | Contents |
| --- | --- |
| `human-labels.csv` | Your editable judgments, stored separately from machine reports. |
| `evidence.html` | All selected evidence excerpts and source links without model conclusions. |
| `evidence.json` | Frozen source evidence, packets, and search provenance. |
| `machine-snapshot.json` | Baseline judgments and saved request/response receipts. Open after labeling to avoid influencing your review. |
| `manifest.json` | Content-checked sample membership, evidence revisions, and splits. Do not edit this file. |
| `selection.csv` | Editable starting worksheet for making a different selection in a new review directory. |
| `known-examples.csv` | Empty worksheet for the independent retrieval check below. |

A label is tied to both repository/product identity and an evidence revision.
Changed excerpts, context, packaging, or evidence limitations make it stale;
timestamps, moved folders, and different model responses alone do not. Comparisons
never silently carry labels onto changed evidence. Re-review a changed example in
a new review directory; keep the old labels for its original revision.

Compare the same human labels with a different saved classification run:

```console
nasa-evidence-review compare pilot-human-review --classifications another-review --output another-validation
```

Here `another-review` must already exist; this command does not create new model
responses. Omit `--classifications` to use the frozen baseline, even after the
original classification folder has moved. Use `--labels PATH` to read a separately
maintained label CSV. Labels for other groups/revisions remain unused and are
counted in `unused_label_rows`. Each comparison also saves `comparison-inputs.json`
to pin the precise machine snapshot, labels, and selection behind its metrics.

### Separate calibration cases from test cases

By default every saved pair is an `ordinary` `regression` example. For a broader
sample, edit the generated `selection.csv`: keep the pairs you want and set:

- `split`: `development` for rubric calibration, `test` for held-out checking,
  or `regression` for previously examined cases.
- `sample_kind`: `ordinary` for ordinary sampled results or `challenge` for
  deliberately difficult identifiers, versions, examples, or missing context.
- `family`: a shared name for a repository and any forks or closely related
  repositories. The default is the repository name. Set related families yourself;
  this offline command cannot discover fork relationships.

Freeze that edited selection in a new review:

```console
nasa-evidence-review prepare pilot-review-v3 --selection pilot-human-review/selection.csv --output selected-human-review
```

The selection must refer to pairs in the supplied classification directory. It
rejects duplicate or unknown pairs, different families for the same repository,
and families assigned to more than one split. All products from a repository stay
together. Editing the worksheet does not alter an existing frozen review.

For a 50-pair sample across about ten products, roughly ten calibration pairs and
forty test pairs is a useful starting allocation; adjust for whole repository
families. Check both ordinary and challenging examples, label test evidence before
viewing model conclusions, and avoid tuning against those test judgments. Reports
break out splits and sample kinds separately; the command does not perform random
sampling or turn a hand-selected set into a representative population estimate.

### Check whether known repositories were retrieved

Search quality is a separate question from whether the classifier judged a hit
correctly. Fill the empty `known-examples.csv` with examples you know from
documentation or domain knowledge, independently of the measured search's hits.
About twenty varied examples is a manageable starting check.

Each row needs `repository` in `github.com/owner/repository` form, `provider`,
`short_name`, a supporting `source_url`, and `discovery_basis` set to
`independent_documentation` or `domain_knowledge`. Leave `miss_reason` blank until
you investigate, or use `query_signature`, `index_coverage`, `search_limits`,
`revision_access`, or `unknown`. Record the investigation in `notes`. The tool
records these declarations but cannot verify their independence or infer why a
repository was missed.

```console
nasa-evidence-review retrieval pilot-review-v3 --known-examples pilot-human-review/known-examples.csv --output pilot-retrieval
```

`retrieval.csv` reports `found`, `searched_zero_not_observed`, `incomplete`, or
`not_searched`, along with saved queries and scope. `found` means a saved file hit
for that repository/product, regardless of its later model category; inspect it
to confirm it is relevant. No hit after a completed query is not proof of no use.
`retrieval-summary.json` gives counts and known-example retrieval rates for all
listed examples, examples with started searches, and examples with completed
searches. These rates are **not GitHub-wide recall**. Frozen inputs are saved in
`retrieval-inputs.json`.

All review subcommands require new output directories. Exit code 0 means files
were generated, even if labels or model work are unfinished; 1 means an input/file
error, 2 means invalid command syntax, and 130 means interruption.

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
| `nasa-evidence-review` | `nasa_eo_search.evidence_review` |
| `nasa-repo-preview` | `nasa_eo_search.repo_preview` |
| `nasa-product-preview` | `nasa_eo_search.cmr` |
| `sourcegraph-search` | `nasa_eo_search.sourcegraph` |

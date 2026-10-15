# NASA dataset references in public code

Use three commands to collect NASA dataset metadata, search public code for its
identifiers, and have GPT classify the matching files. The main outputs are
`datasets.csv`, `matches.csv`, and `classified-matches.csv`.

## Install and try one dataset

Use Python 3.10 or newer. From this repository directory:

```console
python -m pip install -e .
```

Run this again after pulling changes that add commands. If you already have the
saved `nasa-catalog` directory, try ATL03 in a new output directory:

```console
nasa-eo search nasa-catalog --short-name ATL03 --output atl03-files --max-requests 5
```

This makes at most five search calls, including retries. Repeat the same command
to continue; verified completed searches are skipped. Check `summary.json` and
`matches.csv` after each run. A capped trial can finish before finding any hits.

Put your raw OpenAI API key in `./secrets/openai.key`, as described below. Replace
`YOUR_MODEL` with a model identifier available to your API account that supports
Responses structured outputs. Once you have saved matches, classify them:

```console
nasa-eo classify atl03-files --model YOUR_MODEL --api-key-file ./secrets/openai.key --output atl03-classified
```

Open `atl03-files/matches.csv` to see the search hits and
`atl03-classified/classified-matches.csv` to see GPT's assessments. Classification
automatically gathers nearby code; there is no separate preparation command.

## 1. Collect datasets

For a new catalog, use a new output directory:

```console
nasa-eo catalog --output nasa-catalog
```

This queries NASA's Common Metadata Repository (CMR) for public collections
tagged for the Earth Observing System Data and Information System (EOSDIS).
A collection describes a dataset version. Collections sharing the same provider
and exact short name become one dataset row.

For a small collection instead:

```console
nasa-eo catalog --short-name ATL03 --output catalog-atl03
```

| Output | Contents |
| --- | --- |
| `datasets.csv` | One row per provider/short-name dataset, combining its collected versions. |
| `products.jsonl` | Grouped metadata and candidate identifiers used by the search command. |
| `collections.jsonl` | Original collection records with request URLs and retrieval times. |
| `summary.json` | Catalog scope, status, counts, warnings, and errors. |

The dataset table contains `provider`, `short_name`, `dataset_name`,
`description`, `source_url`, `known_from`, `candidate_search_terms`, and
`candidate_sources`. The source URL is the saved catalog request; candidate
sources identify the collection and metadata field supplying each term.

To export a table from an existing catalog without contacting NASA again:

```console
nasa-eo catalog --from nasa-catalog --output nasa-datasets.csv
```

The CSV filename must be new. The search command reads the saved catalog
**directory**, not this CSV, so there is no spreadsheet selection step.

Check the catalog's `summary.json`. `complete` means collection finished with
consistent counts; `incomplete`, `failed`, and `interrupted` describe partial
results. Their status and warnings remain visible in exported metadata. A fresh
collection requires a new directory. Catalog coverage is the selected public
EOSDIS-tagged CMR records at collection time, not every NASA dataset.

## 2. Search public code

Start with one or a few datasets and inspect their results before a larger run.
Repeat `--short-name` to select several exact dataset names:

```console
nasa-eo search nasa-catalog --short-name ATL03 --short-name ATL06 --output ice-files --dry-run
nasa-eo search nasa-catalog --short-name ATL03 --short-name ATL06 --output ice-files --max-requests 5
```

`--dry-run` prints dataset, distinct-term, and query counts plus the scope, without
searching or writing files. With no `--short-name`, every collected dataset and
candidate identifier is selected. A full catalog can produce hundreds of
thousands of partition queries; it is not a small trial.

Each selected dataset contributes its collected short name, entry identifiers,
and collection concept identifiers. Identical terms shared by datasets use one
set of queries associated with each dataset. The query treats each term as
literal text with non-alphanumeric boundaries: `ATL03` can match `ATL03_007` or
`test_atl03`, but not `MATL03`. Matching is case-insensitive.

Search uses these literal metadata identifiers, not semantic similarity or
invented download URLs and filename patterns. A hit can still be unrelated to NASA.

Searches cover **public GitHub content in Sourcegraph's index**, including forks
and archived repositories. Add `--all-hosts` to include other indexed public
code hosts. Neither setting searches every public repository.

A [small live trial](docs/search-reliability-trial.md) found that broad searches
could miss known indexed files even without reported limits. The current
partition strategy is not validated for a full-catalog run.

Each term is searched in nine nonoverlapping repository groups, called
partitions. Eight divide GitHub owners by their first character: digits/A–C,
D–F, G–I, J–L, M–O, P–R, S–U, and V–Z, including lowercase equivalents.
The remainder includes every other GitHub prefix and, with `--all-hosts`, other
code hosts. These filters cover the selected scope without handpicked repositories.

### Limits, retries, and resume

Queries run sequentially with a default 10-second server search timeout. Set
`--search-timeout 20` for a new run to change it; allowed values are 1–60 seconds.
Connection time and retry delays can make a call take longer. The saved timeout,
selected datasets, and host scope are fixed: use a new output directory to change
them. Omit the timeout on resume to retain its saved value.

`--max-requests 5` caps all new Sourcegraph calls in that invocation, including
retries. There is no default request cap. Each partition has a default lifetime
allowance of two attempts; ordinary resume consumes the remaining allowance
without resetting it. Verified completed partitions are skipped, while retries
restart only the unfinished partition and preserve earlier attempt files.

Repeat the same command and output directory to resume, keeping the catalog
unchanged. Inspect warnings before adding `--retry-incomplete`: it grants two
further attempts to each previously attempted, unfinished partition reached in
that run. The saved allowance survives a request cap or interruption; later
ordinary resumes can use what remains. New partitions retain their two attempts.

Three consecutive unsuccessful calls stop the invocation. HTTP 400, 401, or 403
stops it immediately; a long server-requested retry delay is saved for later
resume. Read the stop reason in `progress.json` or `summary.json` before retrying.
Press Ctrl+C to stop and retain collected results.

Existing schema-version-1 outputs keep their original global-query strategy.
They reject the new timeout, request-cap, and retry flags. Use a new output
directory to get partitioned searches; old results are not overwritten or migrated.

### Search output and completion

`matches.csv` has one row per **dataset, repository, file, and commit**. Multiple
matching lines, search terms, and repeated attempts are combined in that row;
different revisions remain separate. Its columns are:

- `match_id`, `provider`, `short_name`, `dataset_name`.
- `repository`, `file`, `commit`, `line_numbers`, `snippet`, `source_url`.
- `search_terms`, `search_complete`, `warnings`.

`searches.csv` includes every dataset/query association, even with no hits, and a
`repository_partition` column for new runs. A `scope_note` identifies confirmed
empty repository groups; these count as completed zero-hit searches, with the
original service alert retained in raw attempt files. `plan.json` preserves terms, sources,
scope, partitions, and timeout; `searches/` retains original attempt data.
`progress.json` is replaced atomically for each request. It and `summary.json`
show current status counts, the current query, and the stop reason. Combined CSVs
refresh at startup and when execution stops, including Ctrl+C; while searching,
their last refresh time is recorded separately from the live counts.

| Query status | Meaning |
| --- | --- |
| `pending` | The query has not started. |
| `finished_no_reported_limits` | The service confirmed completion without reported limits, and saved evidence passed validation. |
| `incomplete` | Limits were reported, the stream ended early, or results failed the partition-scope check. |
| `failed` | A request, file, or saved-evidence validation error occurred. |
| `interrupted` | Processing stopped before the attempt finished. |

For partitioned runs, `search_complete=true` requires a complete source catalog
and successful completion of every selected term's nine partitions for that
dataset. A finished partition with zero hits is different from one that timed
out. Neither a complete run nor a missing match establishes complete GitHub
coverage or absence of use.

## 3. Classify matching files

The input is a saved search directory, not a CSV. You can also use an existing
`nasa-repo-batch` result such as `pilot-search`:

```console
nasa-eo classify pilot-search --catalog nasa-catalog --model YOUR_MODEL --api-key-file ./secrets/openai.key --output pilot-file-classifications
```

You do not need to rerun NASA collection or repository searches. `--catalog`
is optional when the catalog still exists at the location saved in the search
plan. Use a new classification output directory; older repository-level
judgments are not reused as judgments about individual files.

For each matched file and revision, the command collects nearby public GitHub
code at that commit and a bounded README excerpt for background. It sends the
file's evidence and dataset metadata to OpenAI. Other matching files are assessed
separately. No repository code is executed. Unavailable context remains explicit.

Preview classification work first if desired:

```console
nasa-eo classify atl03-files --model YOUR_MODEL --output atl03-classified --dry-run
```

A new-run preview uses saved snippets only, so downloaded context can change its
request count. A dry run makes no downloads or model calls, reads no key, and
writes no output. **`--offline` is different:** it skips public-code downloads
but still sends saved snippets to GPT and can incur API charges.

By default classification processes all eligible requests. To limit new API
calls for one invocation, add `--max-requests 5`. This counts requests, not files;
a large file can need several evidence packets and API calls. A limit can leave
rows pending even when every submitted request succeeded.

Repeat the same classification command to continue. Valid successful responses
are reused. Keep the saved search/catalog unchanged and use the same model and
output-token settings. Context options apply when creating a new output; resumed
runs reuse their frozen excerpts. Use a new directory to change the evidence.

A failed request stops the invocation. Ordinary resume can process other
unstarted requests; inspect errors before explicitly retrying failed or uncertain
requests with `--retry-failed`. An interrupted request may already have been
processed by OpenAI, so retrying it can incur another charge.

### Read the classification table

`classified-matches.csv` retains the original match columns and adds `status`,
`category`, `explanation`, `gpt_model`, `evidence_citations`, and `error`.
It remains one row per dataset/repository/file/commit, not one row per repository.

| Category | What the collected file evidence establishes |
| --- | --- |
| `direct_use` | Application or analysis code accesses, downloads, or processes the dataset. |
| `supporting_software` | A tool, library, test, fixture, or example demonstrates dataset support. |
| `reference_only` | Relevant prose mentions the dataset without demonstrating code use. |
| `unrelated` | The matching identifier clearly means something else. |
| `unclear` | Evidence is insufficient or conflicting, or processing is unfinished. |

Older product versions still count as evidence about that dataset; they do not
establish compatibility with the newest version. A classification describes the
supplied excerpts, not everything the repository does or proof of actual execution.
Evidence links support inspection; a validated citation does not guarantee that
the model interpreted the code correctly.

Processing `status` is separate: `complete` means all required responses were
saved and validated; `pending` means work remains; `failed` means a request or
response failed; `blocked` means usable evidence could not be prepared. Check
`summary.json` before treating a run as finished. `audit.json` contains full
explanations and limitations; `plan.json`, `prompt.json`, `settings.json`, and
`requests/` retain the evidence, settings, and credential-free request receipts.

Optional controls include `--context-lines 20` (default 10),
`--packet-bytes 48000` (default 24000), and `--max-output-tokens 8192` (default
2048). Packet bytes are not model tokens; request/output limits are not a dollar
budget. Choose the same output-token cap when resuming an existing run.

## Credentials, files, and troubleshooting

Save only the raw API key in the UTF-8 file `./secrets/openai.key`: one line,
without quotes, JSON, or an `OPENAI_API_KEY=` prefix. A trailing newline is fine.
Credential lookup uses an explicit `--api-key-file` first; otherwise a nonblank
`OPENAI_API_KEY` environment variable takes precedence over the default file.
An invalid explicit file causes an error rather than silently falling back.
Dry runs and fully cached classifications do not read a key.

Git ignores `secrets/`, `secretes/`, and `*.key`, but these files are plaintext.
Restrict access, keep them out of shared folders, and never commit or paste the
key. Already tracked files are not protected by ignore rules. Key contents and
the chosen key-file path are not written to saved request receipts.

CSV reports are quoted UTF-8 with a byte-order mark for spreadsheets. Formula-like
cells receive a `Text: ` prefix; raw JSON preserves the original text. Combined
reports refresh at the start and end of each search/classification invocation,
including Ctrl+C; individual results are saved as they arrive. Copy reports before
adding personal notes.
Close an open spreadsheet if it prevents an updated CSV from being written.

Commands exit 0 on success or preview, 4 for incomplete/pending/blocked work,
1 for input/service/file errors, and 130 after interruption. Check terminal
errors and `summary.json`, especially after a file-writing failure.

All three commands accept `--ca-bundle PATH` for a trusted PEM certificate bundle;
`SSL_CERT_FILE` is also supported. Sourcegraph authentication uses
`SOURCEGRAPH_TOKEN`. No OpenAI key is needed for catalog collection or code search.

If `nasa-eo` is missing from PATH, use `python -m nasa_eo_search.pipeline` with the
same arguments. Run `nasa-eo --help` or a subcommand's `--help` for options.

The lower-level commands remain available: `nasa-catalog-collect`,
`nasa-catalog-export`, `nasa-repo-search`, `nasa-repo-batch`,
`nasa-evidence-classify`, `nasa-product-preview`, `nasa-repo-preview`, and
`sourcegraph-search`. Use their `--help` when working with their existing outputs.

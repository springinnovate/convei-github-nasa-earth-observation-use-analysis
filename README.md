# NASA dataset references in public code

Use three commands to collect NASA dataset metadata, find references in public
repositories, and have GPT classify the matching files. The main tables are
`datasets.csv`, `matches.csv`, and `classified-matches.csv`.

## Install and try a small sample

Use Python 3.10 or newer. From this repository directory:

```console
python -m pip install -e .
```

For GitHub discovery, install the [GitHub CLI](https://cli.github.com/) and sign in:

```console
gh auth login
```

Search uses that login without copying a GitHub token into this project. Only
confirmed-public results are saved. Search and local scanning make no GPT calls.

If you already have the saved `nasa-catalog` directory, start a small run:

```console
nasa-eo search nasa-catalog --short-name ATL03 --short-name ARIA_S1_GUNW --short-name ASF_ECMWF_SMST --github-pages 2 --max-repositories 3 --max-archive-mb 25 --max-total-mb 75 --max-requests 5 --output github-pilot
```

This makes at most five discovery calls and attempts at most three snapshots.
A capped run exits 4; a run with errors exits 1. Either can retain usable results,
so inspect its tables and summary. Repeat the same command to resume.
Add `--dry-run` to preview without network calls or writes.

After inspecting `github-pilot/matches.csv`, preview classification:

```console
nasa-eo classify github-pilot --model YOUR_MODEL --max-requests 3 --output github-pilot-classified --dry-run
```

Replace `YOUR_MODEL` with a model available to your OpenAI API account that
supports Responses structured outputs. To make up to three **paid** requests:

```console
nasa-eo classify github-pilot --model YOUR_MODEL --api-key-file ./secrets/openai.key --max-requests 3 --output github-pilot-classified
```

Open `github-pilot-classified/classified-matches.csv` for the judgments. A request
cap can leave rows pending. Repeating the command allows another capped group
of requests; successful responses are reused.

## 1. Collect datasets

For a new catalog, use a new output directory:

```console
nasa-eo catalog --output nasa-catalog
```

This queries NASA's Common Metadata Repository (CMR) for public collections
tagged for the Earth Observing System Data and Information System (EOSDIS).
A collection describes a dataset version. Collections sharing the same provider
and exact short name become one dataset row. For a small catalog, add
`--short-name ATL03` and use a new directory.

| Output | Contents |
| --- | --- |
| `datasets.csv` | One row per provider/short-name dataset: names, descriptions, source URL, why it is included, and candidate identifiers. |
| `products.jsonl` | Grouped original metadata and identifier provenance. |
| `collections.jsonl` | Original collection records with request URLs and retrieval times. |
| `summary.json` | Catalog scope, completion, counts, warnings, and errors. |

To export an existing catalog without contacting NASA again:

```console
nasa-eo catalog --from nasa-catalog --output nasa-datasets.csv
```

The CSV filename must be new. Search reads the saved catalog **directory**, not
the CSV. Catalog coverage is the public EOSDIS-tagged CMR records collected at
that time, not every NASA dataset. Partial catalogs retain their warnings.

## 2. Find repositories and scan their code

New searches default to GitHub. Selected dataset short names are sent to code
search to discover candidate repositories. Repositories are selected across
queries up to the saved cap. No known examples are inserted automatically.

Each selected public repository is downloaded at a recorded default-branch
commit. The command then searches **all candidate identifiers in the saved
catalog** together within that snapshot. `--short-name` limits remote discovery,
not local scanning: a repository found through ATL03 may also contain ATL06.
Verified snapshots are reused on resume. Repository code is never extracted,
installed, or executed.

Local matches are case-insensitive with ASCII non-alphanumeric boundaries:
`ATL03_007` and `test_atl03` match `ATL03`; `MATL03` does not. Identifiers come
from saved metadata: short names, entry IDs, and collection concept IDs. Names
such as `SEA` can still match ordinary words. A hit is not verified data use.

### Bounds and resume

| Option | New GitHub run behavior |
| --- | --- |
| `--short-name NAME` | Select discovery products; repeat for several. Without it, all catalog short names are planned, still subject to the invocation request cap. |
| `--max-requests N` | At most N new discovery calls this invocation; default 10. Snapshot metadata/download calls are separate, bounded by the repository cap. |
| `--github-pages N` | Maximum 100-result pages per term, 1–10; default 10. |
| `--max-repositories N` | Maximum selected snapshots; default 6. |
| `--max-archive-mb N` | Compressed download limit per snapshot; default 100 decimal MB. |
| `--max-total-mb N` | Total retained snapshot download budget; default 300 decimal MB. Reports/catalog metadata need additional space. |
| `--include-grep-app` | Add bounded public grep.app discovery; ranked results have no pagination or total-count guarantee. |
| `--retry-incomplete` | Explicitly retry unsuccessful work; does not remove saved page/repository/byte caps. |
| `--dry-run` | Validate and preview without network calls, output writes, or key reads. |

Pages are checkpointed individually. Repeat the command and output directory
to continue; successful pages and verified snapshots are reused. Discovery calls
are spaced to respect GitHub's code-search rate. A failure is saved, not silently
treated as zero matches. Keep the catalog and frozen options unchanged; use a new
directory to change fixed bounds. The request cap applies to each invocation.

Each ZIP scan reads at most 200 MB uncompressed, 20,000 files, and 2 MB per file.
Binary, non-UTF-8, oversized, duplicate, unsafe, or unreadable entries are
recorded as exclusions. Git Large File Storage objects and submodule contents
are not downloaded.

### Search output

| Output | Contents |
| --- | --- |
| `matches.csv` | One row per dataset/repository/file/commit, combining matching terms and bounded snippets with source links. |
| `searches.csv` | Query/page progress and limitations, including zero-result and unsuccessful searches. |
| `repositories.csv` | Discovery provenance, selected/skipped repositories, pinned revisions, download and scan outcomes. |
| `summary.json` | Overall status, counts, and coverage warnings; fixed bounds are in `plan.json`. |
| `plan.json`, `catalog.json`, `snapshots.json` | Frozen run choices, catalog metadata, and snapshot manifest. |
| Saved discovery and snapshot files | Public-only search receipts, pinned archives, and full raw scan matches with checksums. |

Match columns are `match_id`, `provider`, `short_name`, `dataset_name`,
`repository`, `file`, `commit`, `line_numbers`, `snippet`, `source_url`,
`search_terms`, `search_complete`, and `warnings`. Truncation is marked; full raw
matches remain saved separately. Distinct files/revisions remain separate rows.
Forks can contain duplicate code; separate repositories need not be independent
examples of use.

`complete` means configured work finished without outstanding reported limits;
`incomplete` means caps, unfinished discovery, or snapshot exclusions remain.
`failed` and `interrupted` preserve usable saved evidence. These statuses do not
measure how much of GitHub was discoverable.

GitHub returns at most 1,000 results per query from a restricted index, not every
file or revision. Authenticated results are filtered to confirmed public
repositories before saving; nonpublic hits can occupy part of a page. Current
downloaded snapshots may differ from versions indexed by a service.
`search_complete` stays false: no status or zero-hit query establishes exhaustive
GitHub coverage or absence of NASA data use.

### Existing Sourcegraph searches

Existing output automatically retains its saved backend. To create a new
Sourcegraph run explicitly:

```console
nasa-eo search nasa-catalog --backend sourcegraph --short-name ATL03 --max-requests 5 --output sourcegraph-pilot
```

These retain their partitioned query, checkpoint, retry, and report behavior.
`--all-hosts` and `--search-timeout` apply only to Sourcegraph. Schema-version-1
outputs retain their original global-query strategy and reject partition-only
controls. Changing backend requires a new directory. Sourcegraph's global and
repository-specific searches can cover different repositories; a successful
query does not establish that every public repository was searched.

## 3. Classify saved files

Input is a saved search **directory**, not a CSV. GitHub snapshot searches and
existing Sourcegraph searches use the same classification command/categories.

GitHub classification uses bounded matching and nearby code from the saved,
verified revision, without re-downloading public context. Sourcegraph
classification normally fetches nearby code at the recorded commit and bounded
README context. Other matched files are assessed separately; no code is executed.

For each dataset/file, the report keeps up to 12 excerpts containing matches,
normally at most 600 characters each (up to 1,000 for long identifiers).
It can also include up to ten lines before and after each selected matching
line, with a combined limit of 12,000 text characters. The same excerpts feed
the CSV and classifier. Warnings identify omitted text; the full scan results
and ZIP remain available. These excerpt limits are fixed;
`--context-lines` controls fetched Sourcegraph context only.

| Category | What the supplied evidence establishes |
| --- | --- |
| `direct_use` | Application/analysis code accesses, downloads, or processes the dataset. |
| `supporting_software` | A library, tool, test, fixture, or worked example demonstrates support. |
| `reference_only` | Relevant prose mentions the dataset without demonstrating code use. |
| `unrelated` | The identifier clearly means something else. |
| `unclear` | Evidence is insufficient, conflicting, or unfinished. |

`classified-matches.csv` retains original match columns and adds `status`,
`category`, `explanation`, `gpt_model`, `evidence_citations`, and `error`.
Processing status is separate: `complete` means necessary responses were saved
and validated; `pending` means work remains; `failed` means a request/response
failed; `blocked` means usable evidence could not be prepared.

`--max-requests 3` caps new model calls, not files. Without a cap, classification
processes all eligible requests and can incur substantial API charges. A file
may need multiple packets. `--dry-run` makes no model calls, downloads, key reads,
or writes. `--offline` skips public-code downloads but still makes paid model
calls using saved evidence.

Repeat the same classification command to resume. Frozen evidence and successful
responses are reused. Keep the source search/catalog, model, and output-token
settings unchanged. A changed search requires a new classification directory.
Failures or uncertain requests require `--retry-failed`; an interrupted request
may already have incurred a charge.

Options include `--context-lines 20` (default 10 for fetched Sourcegraph context),
`--packet-bytes 48000` (default 24000), and `--max-output-tokens 8192` (default
2048). Packet bytes are not tokens; request limits are not dollar budgets.
`--catalog PATH` supplies a moved source catalog for existing search formats.

Older product versions count as evidence about the family, not proof of latest
version compatibility. Judgments describe excerpts, not actual execution,
scientific impact, or indirect dependency use. Manually check a sample before
treating classifications as measurements.

## Credentials and troubleshooting

Save only the raw OpenAI API key in `./secrets/openai.key`: one UTF-8 line without
quotes, JSON, or an `OPENAI_API_KEY=` prefix. A trailing newline is fine.
An explicit `--api-key-file` takes precedence; otherwise a nonblank
`OPENAI_API_KEY` precedes the default file. An invalid explicit file fails rather
than falling back. Dry runs and cached classifications do not read a key.

Git ignores `secrets/`, `secretes/`, and `*.key`, but these files are plaintext.
Restrict access and never commit or paste the key. Already tracked files are not
protected by ignore rules. Receipts do not contain credentials.

CSV reports are quoted UTF-8 with a byte-order mark. Cells that a spreadsheet
could interpret as formulas receive a `Text: ` prefix so Excel or similar
programs display them as text instead of calculating them. Saved JSON keeps the
original values without that prefix. Copy tables before adding
personal notes. Close a spreadsheet if it blocks report updates.

Commands exit 0 on success/preview, 4 for incomplete/pending/blocked work,
1 for errors, and 130 after interruption. Inspect `summary.json` and warnings.
Saved files remain available after failures.

Use `--ca-bundle PATH` or `SSL_CERT_FILE` for a trusted PEM certificate bundle
where supported. GitHub CLI manages its own connection configuration.
Sourcegraph authentication uses `SOURCEGRAPH_TOKEN`. Classification settings,
receipts, and the frozen audit live alongside the classified table.

If `nasa-eo` is not on PATH, use `python -m nasa_eo_search.pipeline` with the same
arguments. Lower-level commands remain available; use their `--help` for
existing formats and options.

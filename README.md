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
confirmed-public results are saved. Default discovery and local scanning make no
GPT calls. Optional web discovery makes paid OpenAI requests.

If you already have the saved `nasa-catalog` directory, start a small run:

```console
nasa-eo search nasa-catalog --short-name ATL03 --short-name ARIA_S1_GUNW --short-name ASF_ECMWF_SMST --github-pages 2 --max-repositories 3 --max-archive-mb 25 --max-total-mb 75 --max-requests 6 --output github-pilot
```

This makes at most six discovery calls and attempts at most three snapshots.
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

New searches use both GitHub code search and repository search across names,
descriptions, and READMEs. A repository-search hit identifies a candidate;
the downloaded snapshot supplies actual file matches. Both sources count
against `--max-requests` and have separate saved pages and completion statuses.

Selection takes turns across datasets and search sources, beginning with web
discovery when enabled, then repository search. It prefers distinct repository names, repositories not
reported as forks, and repositories whose reported size does not exceed the
archive-size limit. Reported size is a rough guide, not a ZIP-size guarantee;
unknown sizes remain eligible. Deferred candidates fill otherwise unused slots.
Matching repository names are a diversity heuristic, not proof of shared code.
The selected order also determines download order. Existing selections remain
fixed on resume, including failed downloads, and count toward the repository cap.

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

GitHub match tables use the saved catalog to separate overlapping dataset names.
For example, `SPL3SMP` inside the distinct `SPL3SMP_E` identifier belongs to the
enhanced product. A separate `SPL3SMP` mention still gets its own row. Versions
and filename suffixes remain valid matches when the catalog does not identify
them as another product. Shared catalog identifiers retain every recorded owner.
`summary.json` records excluded product/file associations and identifier
occurrences per dataset association under `identity_filtering`; raw scans and
ZIPs retain the original text. This check depends on the saved catalog and does
not resolve unknown aliases.

To add discovery terms, create an optional UTF-8 CSV with these exact headers:

```csv
short_name,term,source_url
MUR-JPL-L4-GLOB-v4.1,jplMURSST41,https://coastwatch.pfeg.noaa.gov/erddap/info/jplMURSST41/index.html
```

The [NOAA metadata](https://coastwatch.pfeg.noaa.gov/erddap/info/jplMURSST41/index.html)
links this ERDDAP identifier to the NASA MUR product. Each CSV row must name a
dataset in your catalog and provide an HTTPS source for the alias. Add
`--discovery-aliases discovery-aliases.csv` to a new search. Canonical dataset
names are queried before aliases; aliases share their dataset's selection queue.
The selected rows and source URLs are frozen in the plan and shown in
`searches.csv`. On resume, the CSV may be omitted; supplying changed aliases
requires a new output directory.

Discovery aliases broaden the candidate list only. Local scanning and
classification still use the saved catalog identifiers. A repository found only
through an alias can therefore have no local catalog matches; that does not
establish absence of data use.

### Optional web discovery

Add `--web-search-model YOUR_MODEL` to a new GitHub search to include paid OpenAI
web discovery. Use a model available to your API account that supports the
[Responses web-search tool](https://developers.openai.com/api/docs/guides/tools-web-search).
The key lookup uses `OPENAI_API_KEY`, then `./secrets/openai.key`; an explicit
`--api-key-file PATH` takes precedence. Keys and key-file paths are not saved in
the search plan. Dry runs and resumes with no new web work do not read a key.

For each selected dataset, the command prepares an exact short-name query and,
when the catalog has a useful title, a second query adding up to six title words.
Both target `github.com`. The plan records the title, catalog provenance, model,
and requested queries. Discovery aliases remain separate GitHub/grep.app terms.

Each new web request counts toward `--max-requests` and allows at most two web
tool calls and 2,000 output tokens. A tool call can contain multiple search
strings; the response's actual queries are recorded separately and differences
are flagged. Only URLs supplied by the web tool are accepted as candidates;
model-written answers are not treated as evidence. Up to twenty candidate
repositories per response are checked through GitHub for public visibility,
with up to five source URLs retained per repository. Those metadata calls are
additional to the discovery-call cap. The selected snapshot count remains
bounded by `--max-repositories` across all discovery sources.

Web discovery supplies a ranked sample without a total-count or pagination
guarantee. It does not establish dataset use. The local scan and classification
commands process the selected repositories as usual. Saved web responses are
reused; `--retry-incomplete` can repeat failed, interrupted, or incomplete paid
requests and may incur another charge. Completed limited samples are not retried
automatically. Omit the model flag to reuse a saved setting; changing the model,
queries, or enabling web search on an older run requires a fresh output directory.

### Bounds and resume

| Option | New GitHub run behavior |
| --- | --- |
| `--short-name NAME` | Select discovery products; repeat for several. Without it, all catalog short names are planned, still subject to the invocation request cap. |
| `--max-requests N` | At most N new discovery calls this invocation; default 10. Snapshot metadata/download calls are separate, bounded by the repository cap. |
| `--github-pages N` | Maximum 100-result pages per term and GitHub search source, 1–10; default 10. |
| `--github-discovery code-and-readme` | Default for new runs; adds repository/README discovery. `code` uses only code search. |
| `--discovery-aliases PATH` | Optional CSV of additional discovery terms and their source URLs. |
| `--web-search-model MODEL` | Opt in to paid web discovery, one model request per selected canonical dataset; saved runs retain their setting. |
| `--api-key-file PATH` | Explicit OpenAI key file for web discovery. Without it, use the environment or `./secrets/openai.key`. |
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
Older GitHub output directories retain code-only discovery and their original
selection order. Choose a new output directory to use the combined search and
diversity preference.

Each ZIP scan reads at most 200 MB uncompressed, 20,000 files, and 2 MB per file.
Binary, non-UTF-8, oversized, duplicate, unsafe, or unreadable entries are
recorded as exclusions. Git Large File Storage objects and submodule contents
are not downloaded.

### Search output

| Output | Contents |
| --- | --- |
| `matches.csv` | One row per dataset/repository/file/commit, combining matching terms and bounded snippets with source links. |
| `searches.csv` | Requested queries, actual web queries, model/token usage, dataset associations, alias sources, page progress, candidate repository/file counts, and limitations. Repository-only hits have zero candidate files. |
| `discovery.csv` | Candidate repositories and discovery locations, including verified public source URLs for web results. File evidence comes from the local scan. |
| `repositories.csv` | Discovery provenance, selection reasons/name groups, available public metadata, pinned revisions, download and scan outcomes. |
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
The [repository-search qualifiers](https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories)
describe the additional index searched by `code-and-readme`.

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

Input is a saved search **directory**. GitHub snapshot searches and existing
Sourcegraph searches use the same classification command/categories.

To classify only particular datasets, repeat `--short-name` with their exact,
case-sensitive names:

```sh
nasa-eo classify github-pilot --short-name ATL03 --short-name ATL06 --model YOUR_MODEL --max-requests 3 --output selected-classifications --dry-run
```

To choose individual matches, copy `github-pilot/matches.csv` to
`selected-matches.csv` and keep only the rows you want. Preserve the `match_id`
column and its values; other columns may remain and are ignored when selecting.
The saved search supplies the actual code evidence, even if you edit the copy.

```sh
nasa-eo classify github-pilot --selection selected-matches.csv --model YOUR_MODEL --max-requests 3 --output chosen-classifications --dry-run
```

Replace `YOUR_MODEL`, inspect the preview, then remove `--dry-run` to classify.
Without either selector, a new run includes all saved matches. If both selectors
are supplied, a match must be in the CSV and belong to a named dataset.
Unknown names or IDs, empty selections, and selections with no matching files
fail before model calls. Duplicate names or IDs select the same match once.

The output saves the selection. To resume, repeat the same selectors or omit
both; the original selection CSV is not needed when both are omitted. Use a new
`--output` directory for a different selection. `--max-requests` limits new model
calls; it does not choose a varied sample or change the saved selection.

GitHub classification uses bounded matching and enclosing code from the saved,
verified revision, without re-downloading public context. Sourcegraph
classification normally fetches nearby code at the recorded commit, adds bounded
enclosing definitions when available, and includes bounded README context.
Other matched files are assessed separately; no code is executed.

For each dataset/file in a GitHub snapshot, the report keeps up to 12 excerpts containing matches,
normally at most 600 characters each (up to 1,000 for long identifiers).
It can also include up to ten lines before and after each selected matching
line, with a combined limit of 12,000 text characters. Classification retains
these original excerpts and can add enclosing Python functions/classes or
configuration blocks, including nearby uses of an identifier table or YAML
anchor. Small scripts can include their complete source (up to 160 lines and
6,000 characters); Python files containing functions or classes keep selective
context. Leading comments help explain the file's purpose.

For notebooks, classification reads source cells in the matching section and
includes introductory text when it fits. Added notebook context excludes stored
outputs and metadata, retains cell locations, and stops at section boundaries.
Original matching excerpts remain available even when a notebook cannot be decoded.

For Markdown and R Markdown, classification includes relevant section headings
and bounded introductory text so worked examples retain their stated purpose.
Headings inside code blocks are not treated as document headings.

Added code stays in the same file and revision, has source citations, and uses
at most 6,000 additional characters within the combined 12,000-character limit.
Other files fall back to bounded nearby context. Warnings identify omitted text;
the full scan results and ZIP remain available. These limits are fixed;
`--context-lines` controls fetched Sourcegraph context only.

| Category | What the supplied evidence establishes |
| --- | --- |
| `direct_use` | An analysis or end-user application consumes the dataset to answer its own question or produce an application result. |
| `supporting_software` | A reusable library, service, adapter, processing tool, test, tutorial, or worked demonstration enables dataset access or processing. It may itself read data, calculate values, or display a plot. |
| `reference_only` | Relevant prose mentions the dataset without demonstrating code use. |
| `unrelated` | The identifier clearly means something else. |
| `unclear` | Evidence is insufficient, conflicting, or unfinished. |

A worked example that loads a dataset and plots it to demonstrate a tool counts
as supporting software. An analysis that uses the dataset to answer its own
question counts as direct use. The classifier considers explanatory comments,
notebook text, and the code together. A file's name, location, or notebook format
alone does not establish its purpose; ambiguous cases remain `unclear`.

The classifier also receives catalog mappings for identifiers actually present
in its evidence, including collection IDs, catalog versions, and NASA metadata
links. These explain which dataset an identifier names; the code must still
show its role. A collection ID alone does not establish data use.

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
Existing runs retain their saved prompt and inputs. Use a new `--output`
directory to gather context again and apply the current category guidance,
even when the source search has not changed. The existing output keeps its
original assessments; the new run can make paid model requests.

For GitHub runs, a new classification directory also applies catalog identity
filtering to the saved snapshots. Older classification directories keep their
original dataset associations and display a notice when resumed. No additional
repository download is needed to prepare the new evidence. New model requests
can incur charges; use `--dry-run` to inspect the count first.

Resume revalidates saved responses. A previously rejected response that now
passes validation is reused without another model call. Use `--retry-failed`
to resubmit remaining failed or uncertain requests; an interrupted request may
already have incurred a charge.

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

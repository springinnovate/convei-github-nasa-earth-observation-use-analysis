# Finding NASA Earth observation data in public software

## What this project is trying to learn

NASA Earth observation data are used in research, education, public services,
commercial applications, and software libraries. Much of that work is visible
in public GitHub repositories, but there is no ready-made inventory that says
which projects use which NASA data products or what they do with the data.

This project will build that inventory. Our research question is:

> Which public GitHub repositories directly or indirectly use NASA Earth
> observation data products, and how are those products used?

A **repository** is the public project folder hosted on GitHub. It may contain
software, notebooks, configuration, documentation, and examples. We do not
assume that every repository is a finished application or that every textual
mention proves actual data use. The project will preserve the evidence for each
finding and assign a confidence level instead of treating every search result
as equally meaningful.

The work has three connected phases:

1. Turn NASA's data catalog into a dictionary of terms that may appear in code.
2. Search public GitHub code for those terms and describe the repositories and
   the evidence found in them.
3. Discover products derived from NASA data and conduct follow-up searches for
   uses that no longer mention NASA or the original product.

In short:

`NASA catalog -> code-like signatures -> repository matches -> verified uses -> derivative products -> follow-up searches`

The result should support questions such as:

- Which NASA products appear most often in public software?
- Are they downloaded, transformed, visualized, used to train models, or only
  mentioned in documentation?
- Which programming languages and tools are common around each product?
- What kinds of organizations and individuals publish the work?
- Which important uses are hidden behind the name of a downstream product?

## What counts as use?

We will distinguish several kinds of evidence.

| Classification | Meaning | Example evidence |
| --- | --- | --- |
| Direct use | The project identifies, requests, opens, or processes a NASA product. | A product short name passed to the CMR API, an Earthdata URL, or code opening a recognizable granule filename. |
| Indirect use | The project uses a downstream product that was made from NASA data, even if the code does not name NASA. | Code downloading HydroSHEDS version 1, which was derived primarily from SRTM elevation data. |
| Probable use | Several clues indicate use, but the available file does not prove that data were obtained or processed. | A product identifier in configuration plus a related processing library in the same repository. |
| Mention only | The term appears, but the repository is not using the data. | A bibliography, catalog listing, test fixture, copied example, or unrelated acronym. |

This distinction is essential. A search engine finds matching text; it does not
understand research intent. Automated rules can rank evidence, but a sample of
results will need human review and high-value claims should remain traceable to
the exact file and revision where the evidence was found.

## Phase 1: build the NASA product and signature catalog

### Goal

Create a reproducible list of NASA Earth observation products and the textual
forms, or **signatures**, by which each product might be recognized in a
repository.

NASA's [Common Metadata Repository (CMR)](https://www.earthdata.nasa.gov/about/esdis/eosdis/cmr)
is the authoritative starting point because it unifies NASA Earth science
collection and granule metadata. At the time this plan was written (August 24,
2026), the [EOSDIS provider holdings directory](https://cmr.earthdata.nasa.gov/search/site/collections/directory/eosdis)
listed 12,003 collection records. A collection record is not necessarily a
unique product family: separate versions, processing levels, providers, and
near-real-time variants may need to be grouped or kept distinct depending on
the question being asked.

### How a NASA product might look "in code"

The catalog title alone is rarely sufficient. We will generate and verify
several kinds of signatures for each product:

The examples below are illustrative, not the Phase 1 product list itself. In
NASA terminology, a collection describes a product or version while a
**granule** is an individual data item, such as one file, scene, orbit, or time
slice within that collection.

- **Short names and versions**, such as `MOD13Q1.061`, `ATL03`, `GEDI02_A.002`,
  or `M2T1NXSLV`.
- **Granule filename patterns**, including stable prefixes, date fields, tile
  identifiers, version numbers, and extensions such as `.hdf`, `.h5`, `.nc`,
  or `.tif`.
- **CMR identifiers and request parameters**, such as a collection concept ID
  or `short_name="ATL03"` in a search call.
- **Distribution locations**, including Earthdata and DAAC URLs, API endpoints,
  cloud bucket names, STAC collection identifiers, and path structures.
- **Mission, platform, and instrument aliases**, such as MODIS, VIIRS, SMAP,
  ICESat-2, GEDI, MERRA-2, GPM, SWOT, and EMIT. Broad names are useful but have
  a lower confidence because they can occur without product use.
- **Software ecosystem forms**, such as constants used by `earthaccess`,
  examples in a DAAC client, or a Google Earth Engine dataset path that maps
  back to a NASA collection.

Landsat illustrates why provenance must be explicit: it is a joint NASA/USGS
program, and distribution identifiers may look more like USGS than NASA. The
catalog should retain the responsible agencies and providers rather than force
every product into a NASA-only label.

Each signature record should contain:

- the normalized product family, specific collection, version, and processing
  level;
- CMR concept ID, short name, title, provider/DAAC, platform, and instrument;
- the exact term or pattern to search;
- the place the signature came from and the date it was checked;
- whether it is expected in code, filenames, configuration, URLs, notebooks,
  or documentation;
- a specificity rating: strong, medium, or broad;
- known collisions, ambiguous acronyms, and suggested exclusion terms.

### Proposed method

1. Harvest tagged EOSDIS collections through the
   [CMR Search API](https://cmr.earthdata.nasa.gov/search/site/docs/search/api.html),
   NASA's automated catalog-search interface, using its Search-After mechanism
   so the harvest is repeatable.
2. Preserve the source metadata, then normalize product families, versions,
   providers, platforms, and instruments into separate fields.
3. Extract identifiers, access links, file conventions, and service metadata.
4. Generate conservative search signatures and known negative examples.
5. Test signatures against a small set of known repositories. Remove noisy
   terms and document ambiguous ones before a broad search.

### Difficulty, time, and storage

**Relative difficulty: medium to high.** Downloading CMR metadata is
straightforward. Recognizing product families, finding real filename patterns,
and controlling ambiguous aliases require Earth science knowledge and review.

Planning range for one researcher/engineer with periodic review by a NASA data
specialist:

- a useful pilot covering representative product families: about 1 week;
- a broad, reviewed first catalog: about 3-6 weeks;
- approximately 50,000-250,000 candidate signatures before noisy or duplicate
  forms are pruned;
- about 10-100 MB for the normalized signature index; and
- roughly 0.5-2 GB if full source metadata and intermediate extracts are also
  retained.

These are planning estimates, not measured results. The first pilot should
report actual signatures per collection, duplicate rates, and false-positive
rates so the range can be revised.

### Phase 1 output

The primary output is a versioned product/signature catalog that explains why
each search term belongs to a product. It should be useful independently of any
one search provider and should allow a result to be traced back to CMR or
another authoritative source.

## Phase 2: search public GitHub repositories and record use

### Goal

Search for the Phase 1 signatures, retain the matching evidence, group matches
by repository and product, and enrich each repository with enough context to
understand what the project is and how current or influential it may be.

### Search approach

The first search service is Sourcegraph's public code index: a searchable
catalog built from repository contents. Sourcegraph says
its [open-source index contains more than two million repositories](https://sourcegraph.com/docs/dotcom/indexing-open-source-code)
from GitHub and other code hosts. Queries will be restricted to public GitHub
repositories and sent through the
[streaming search API](https://sourcegraph.com/docs/api/stream-api).

Searches will begin with the most specific identifiers and patterns. Broader
mission names will be combined with contextual clues such as file types,
Earthdata domains, access functions, or related identifiers. Large searches
will be divided by signature family, filename or language when needed so that
timeouts and result limits are visible rather than silently losing matches.

We will store match metadata and small evidence snippets, not a copy of every
repository. The GitHub repository API can then add public facts such as the
owner login, account type, description, topics, primary language, star and fork
counts, license, archive status, and activity dates. GitHub documents these
fields in its [repository API](https://docs.github.com/en/rest/repos/repos).

### What to record

Three linked records keep the evidence understandable and avoid repeating the
same repository information for every matching line.

#### Search run

- query and signature catalog version;
- search provider, start time, finish time, and software version;
- requested scope, including treatment of forks and archived repositories;
- reported match and repository counts;
- timeouts, limits, excluded files, warnings, and whether the result was
  reported as complete.

#### Repository

- GitHub repository URL and stable identifier;
- repository name and public owner login;
- whether GitHub reports the owner as a person or an organization;
- repository description and topics;
- primary and detected programming languages;
- stars and forks, recorded as a dated snapshot rather than a permanent score;
- license, creation date, last push date, archive/fork status, and default
  branch;
- the repository's stated purpose, classified from its README and topics as,
  for example, research, education, operational workflow, application,
  reusable library, data preparation, model training, or infrastructure.

The GitHub login is the **publisher of the repository**, not necessarily the
legal owner of the code, the employer of every contributor, or the organization
that used the data. We should not infer affiliation or ownership when it is not
publicly stated.

#### Product occurrence

- NASA product and signature IDs;
- exact repository revision or commit searched;
- file path, extension, detected language, and file role: source code,
  notebook, configuration, dependency file, test, documentation, or data
  manifest;
- matching text and a stable link to the evidence where possible;
- action suggested by the surrounding code: search, download, open, transform,
  analyze, visualize, train a model, redistribute, cite, or mention;
- direct, indirect, probable, or mention-only classification;
- automated confidence score, review status, and reviewer notes;
- duplicate, copied-example, generated-file, and fork relationships.

The repository's purpose and the action around a match will initially be
machine-assisted classifications. They should not be presented as facts until
validated, and a statistically useful sample should be reviewed by people.

### Coverage: what "all public GitHub repositories" means in practice

The research goal is broad GitHub coverage, but the initial measurement is all
eligible public GitHub repositories available in the chosen search index at the
time of each run. Those are not identical populations.

Sourcegraph's documented defaults and index rules create known gaps:

- global searches cover indexed repositories, not every public repository that
  exists on GitHub;
- the default branch is the normal indexed target, so old branches and much of
  repository history are outside the initial scan;
- archived repositories and forks are excluded by default unless requested;
- binary files, invalid UTF-8 text, files larger than 1 MB, and some unusually
  complex files are skipped by the index; and
- an unscoped global result can lag the latest default-branch revision.

These behaviors are documented in Sourcegraph's
[search configuration](https://sourcegraph.com/docs/admin/search) and
[Sourcegraph.com overview](https://sourcegraph.com/docs/dotcom). They matter
for Earth observation because data manifests, large notebooks, and binary
scientific formats can exceed index limits.

Every published count must therefore name the source, date, query/catalog
version, and completeness warnings. A coverage audit should:

1. test a reference set of repositories known to use NASA data;
2. compare a sample with GitHub's own search results;
3. log all Sourcegraph exclusions, limits, timeouts, and index revisions;
4. measure separate runs that include archived repositories and, where useful,
   forks; and
5. report findings as "found in the searched index," not as a guaranteed census
   of all GitHub.

Additional indexes or a targeted GitHub crawl can later fill measured gaps.
Mirroring and indexing all public GitHub code ourselves would be a very
different, multi-terabyte infrastructure project and is not part of the first
implementation.

### Difficulty, time, and storage

**Relative difficulty: high.** The mechanics of one search are simple. The
scale, result limits, ambiguous terms, duplicate forks, repository enrichment,
and distinction between a mention and actual use make the full phase difficult.

Initial planning assumptions are:

- a 100-300-signature pilot: approximately 2-5 days to search, enrich, inspect,
  and tune;
- a first broad search: approximately 4-12 weeks, including query execution,
  retries, repository enrichment, deduplication, and quality review;
- a system designed to handle 100,000-10 million raw matches across
  10,000-500,000 candidate repositories, as capacity bounds rather than a
  prediction of the final count;
- roughly 1-50 GB for raw match metadata and evidence snippets; and
- roughly 1-10 GB for deduplicated, analysis-ready tables and indexes.

Search computation may finish in days or weeks; reviewing purpose and use is
likely to take longer. Query grouping and service rate limits will determine
wall-clock time. The pilot will replace these ranges with observed matches per
signature, bytes per match, API throughput, and review minutes per repository.

### Phase 2 output

The output is a reproducible evidence dataset, not just a list of URLs. It will
support analysis by product, repository, publisher type, file role, language,
use action, confidence, and time while retaining the match needed to audit each
claim.

## Phase 3: discover and search for derivative products

### Goal

Find products and services created from NASA observations, add their provenance
and code signatures to a derivative catalog, and run a second search pass. This
captures indirect use that cannot be found by searching only for NASA names.

### How derivative discovery works

Phase 2 repositories can reveal downstream names in READMEs, citations,
download URLs, dependencies, comments, output filenames, workflow steps, and
products repeatedly mentioned next to a NASA identifier. Candidate derivatives
will be ranked by repeated co-occurrence and explicit provenance statements,
then checked against authoritative documentation.

The data model becomes a provenance graph:

`NASA source product -> transformation or producer -> derivative product -> code signatures -> repositories`

Each derivative record should name its source products, version, producer,
transformation when known, supporting citation, signatures, and confidence.
Different versions must be treated carefully because their input data can
change.

### Concrete example: SRTM to HydroSHEDS

[HydroSHEDS version 1 technical documentation](https://data.hydrosheds.org/file/technical-documentation/HydroSHEDS_TechDoc_v1_4.pdf)
states that it is derived primarily from three-arc-second Shuttle Radar
Topography Mission (SRTM) elevation data. The first search pass might find
repositories that mention both SRTM and HydroSHEDS. After verifying the
relationship, the derivative catalog could add signatures such as:

- `HydroSHEDS`;
- `HydroBASINS` and `HydroRIVERS`;
- `hydrosheds.org` download URLs; and
- recognizable HydroSHEDS filenames and layer codes.

The follow-up search may then find a flood model that downloads HydroBASINS but
never uses the words NASA or SRTM. That is legitimate indirect NASA data use,
provided it is tied to the appropriate HydroSHEDS version and reported as a
provenance relationship rather than direct access to SRTM.

### Difficulty, time, and storage

**Relative difficulty: high and iterative.** Product lineage can be many steps
long, provenance may be poorly documented, names change between versions, and
not every product mentioned near a NASA product is derived from it.

Planning range:

- 2-6 weeks for the first ranked derivative review and follow-up campaign;
- hundreds to thousands of candidate derivative products, narrowed by evidence
  and domain review;
- approximately 0.1-5 GB for provenance records, citations, candidate evidence,
  and follow-up results beyond the Phase 2 store; and
- recurring follow-up passes as new derivatives are confirmed.

Human provenance review is the limiting step. The aim is a defensible graph,
not the largest possible list of loosely related products.

### Phase 3 output

The output is a cited derivative-product catalog plus additional repository
occurrences linked through explicit lineage paths to NASA source data.

## Program estimates at a glance

These estimates assume one researcher/engineer, periodic Earth observation
domain review, public default-branch text search, repository metadata and small
evidence snippets rather than complete code archives, and normal public-service
limits. Phases will overlap, and every range should be recalibrated after its
pilot.

| Phase | Relative difficulty | First useful result | Broad first version | Planning-scale storage |
| --- | --- | --- | --- | --- |
| 1. Product signatures | Medium-high | About 1 week | About 3-6 weeks | 10-100 MB normalized; 0.5-2 GB with source metadata |
| 2. Repository evidence | High | About 2-5 days for a pilot | About 4-12 weeks | 1-50 GB raw; 1-10 GB curated |
| 3. Derivative discovery | High, iterative | About 2 weeks | About 2-6 weeks for the first loop | 0.1-5 GB beyond Phase 2 |

Storage is relatively modest because Sourcegraph maintains the full-text code
index. This project stores the product dictionary, queries, match evidence,
repository facts, review decisions, and provenance—not a duplicate of GitHub.

## Quality, reproducibility, and responsible interpretation

The following rules apply across all phases:

- Keep the original source, query, date, catalog version, and code revision for
  every claim.
- Never equate a string match with confirmed use.
- Measure false positives and missed known repositories for each signature
  class.
- Deduplicate forks, generated files, vendored dependencies, and copied
  examples without discarding their relationships.
- Treat stars as a changing popularity signal, not a measure of scientific
  importance or data impact.
- Treat a GitHub account as the public repository publisher; do not infer a
  person's employer, identity, or legal ownership.
- Retain only the code excerpts needed to explain a match, with links to the
  public source and its license.
- Publish counts with coverage limitations and uncertainty.

## Data tables we expect to produce

The exact storage technology can change, but the information should remain
separated into linked tables or files:

- **products**: normalized NASA collections and product families;
- **signatures**: searchable names, identifiers, patterns, locations, and
  confidence;
- **search runs**: queries, scope, timing, completeness, and warnings;
- **repositories**: public GitHub metadata and classified purpose;
- **occurrences**: product-to-file evidence and use classification;
- **derivatives**: cited product lineage and follow-up signatures; and
- **reviews**: human decisions, notes, and sampling information.

This structure lets us improve classifications without rerunning every search
and lets another researcher reproduce or challenge an individual finding.

## Current software: one-query Sourcegraph tracer

The repository currently contains the first small building block for Phase 2:
a command-line client that sends one raw query to Sourcegraph's public streaming
search API and writes matching file records as JSON Lines. It does not yet build
the Phase 1 catalog, run multiple queries, enrich repositories, classify use, or
persist an analysis database.

### Install

Python 3.10 or newer is required. Certifi is the only runtime dependency.

```console
python -m pip install -e .
```

### Run one search

Quote the query so the shell passes it as one argument:

```console
sourcegraph-search "earthdata.nasa.gov count:10 timeout:15s"
```

Each standard-output line is an independent JSON object. Progress and the final
count go to standard error, so output can be redirected safely:

```console
sourcegraph-search "ATL03 count:100 timeout:30s" > matches.jsonl
```

Useful diagnostic options are:

```console
sourcegraph-search --trace "HLSL30 count:10 timeout:15s"
sourcegraph-search --raw-events "MOD13Q1 count:10 timeout:15s"
```

The public Sourcegraph instance does not require a token. If one is needed, put
it in `SOURCEGRAPH_TOKEN`. A self-hosted endpoint can be selected with
`--endpoint`.

The client uses Certifi's certificate authority bundle to avoid certificate
store parsing failures seen with some Conda Python installations on Windows. A
network proxy that requires a private certificate authority can provide a PEM
bundle with `--ca-bundle PATH` or the `SSL_CERT_FILE` environment variable.

### Test

```console
python -m unittest discover -s tests -v
```

Tests use in-memory event streams and make no network requests.

## Near-term implementation sequence

1. Define the Phase 1 product and signature schemas.
2. Harvest a small, representative set of CMR collections.
3. Build and manually review the first 100-300 signatures.
4. Extend the current tracer to record complete search-run metadata and results.
5. Run the Phase 2 pilot, enrich repository metadata, and measure quality and
   storage.
6. Revise scale estimates before expanding the catalog or search campaign.
7. Add the derivative/provenance schema and conduct the first Phase 3 loop.

# Finding NASA Earth observation data in public software

> Which public GitHub repositories contain evidence of direct or indirect use of NASA Earth observation data products, and how are those products used?

We plan to examine public GitHub repositories to understand where and how NASA Earth observation data products are used in public software, both directly and through downstream products that may no longer identify NASA as a source. Our goal is to produce a reproducible, evidence-backed picture of this use. We expect the work to yield two reusable resources: a traceable catalog of NASA products and their search signatures, and a browsable and downloadable catalog of repositories, matches, and supporting evidence. Together, these products will show which NASA data products appear in public software, the purposes they appear to serve, and the strength of the evidence for each identified use. We expect to adapt our methods as the work reveals new terminology, relationships, and forms of use.

Our intial approach is to break this into three phases:

1. Identify NASA Earth observation data products in the Common Metadata Repository (CMR) and convert their names, identifiers, access paths, and filename conventions into searchable terms and patterns. We will expand these as we discover additional identifiers and conventions. The primary output of this phase will be a versioned catalog of products and their search signatures, documenting the evidence linking each term or pattern to a product. The catalog will be independent of any single search provider, and every entry will be traceable to CMR or another authoritative source.

2. Search public GitHub repository contents using those product search terms and patterns, characterize the repositories containing matches, and preserve the evidence linking each match to a NASA Earth observation data product. The resulting evidence base will be useful in its own right and will also reveal downstream products, lineage relationships, and additional search terms to pursue in Phase 3.

3. Build on the downstream products and lineage relationships we discover in  Phase 2 and identify data products derived from NASA Earth observation data, document their lineage to the original NASA source products, and search public GitHub repository contents for references to those downstream products that do not mention NASA or the source products.

We will present the Phase 2 evidence base as a catalog of GitHub repositories that reference NASA Earth observation data products. We'll set this up so that users will be able to browse or filter the catalog by NASA product, mission, instrument, repository, programming language, application area, type of reference, and confidence in the match.

## Resources we'll use

NASA's [Common Metadata Repository](https://www.earthdata.nasa.gov/about/esdis/eosdis/cmr) is a searchable catalog of Earth science datasets. It includes NASA Earth observation products, as well as data outside our scope. In Phase 1, we will use it as the starting point for identifying relevant products and deriving their search terms and patterns.

[Sourcegraph Public Code Search](https://sourcegraph.com/search) provides keyword, regular expression, and filename searches across its index of public GitHub repositories. It will be our initial code search provider for Phase 2. Because the product and search signature catalog will be provider independent, we will be able to use additional search providers as the work develops.

## Try finding a NASA data reference in public code

Run this to search public GitHub repository contents for a product name or phrase
and stop at the first matching file:

```console
python -m pip install -e .
nasa-repo-preview ATL03
```

The command contacts Sourcegraph's public code search and prints a repository
link, star count when available, file path, language, commit, and matching code
lines. It closes the connection as soon as the first file match arrives. The
first match is whichever arrives first; it is not ranked as the best example.
An immediate status message and subsequent search progress appear on screen.

Other examples:

```console
nasa-repo-preview "earthdata.nasa.gov"
nasa-repo-preview ATL03 --all-hosts
nasa-repo-preview ATL03 > first-repository.json
```

`--all-hosts` includes other public code hosts indexed by Sourcegraph. Searches
use a delimited literal phrase, a one-result limit, a 15-second server search timeout,
and a 20-second socket timeout. These timeouts are not a strict wall-clock
deadline. You can interrupt with Ctrl+C. JSON results go to stdout; status and
search warnings go to stderr. Exit codes are 0 for a match, 1 for an error,
2 for invalid arguments, and 3 if no match is returned.

This tests the repository-discovery step directly. It does not contact NASA's
catalog. The result is evidence to inspect: a mention alone does not establish
use. Coverage is Sourcegraph's public index, with forks and archived repositories
excluded by default. No returned match does not prove that no repositories use
the product. Search limits and exclusions reported by the service are displayed.

Matching is case-insensitive and rejects occurrences joined directly to ASCII
letters or digits: searching `ATL03` will not match `MATL03`, `ATL030`, or
`ATL03X`. Underscores, dots, slashes, hyphens, quotes, and whitespace count as
separators, so it can find `ATL03_007`, `ATL03.h5`, and `test_atl03`. This is a
product-token rule rather than Python-style whole-word matching, which would
exclude underscore-separated filenames. Punctuation in your search phrase is
literal (for example, the dots in `earthdata.nasa.gov` are not wildcards).
These boundaries remove substring collisions; context is still needed to decide
whether a matching token refers to the NASA product.

The existing `sourcegraph-search` command accepts a full Sourcegraph query for
larger searches. The optional catalog lookup below serves a different purpose:
finding names to search for. If a command is missing from your PATH, use
`python -m nasa_eo_search.repo_preview ATL03` in the installed environment.

## Optional: preview a product in NASA's catalog

The `nasa-product-preview` command asks NASA CMR for one EOSDIS collection,
prints its name, version, provider, collection ID, and candidate code search
terms, then exits. This gives us a small working example to inspect before we
build the larger catalog.

Install or update the commands with Python 3.10 or newer (run this again if you
installed the earlier Sourcegraph tool):

```console
python -m pip install -e .
nasa-product-preview
```

To try a recognizable product, use its short name:

```console
nasa-product-preview --short-name ATL03
```

For example, an ATL03 result can include `ATL03`, a collection entry identifier
such as `ATL03_007`, and a CMR concept ID. Each candidate term identifies the
metadata field it came from and is marked unreviewed. The output also includes
the request URL and retrieval time. Versions and CMR's first result can change;
this command does not select or promise the newest version.

The command prints an immediate status message, requests `page_size=1` through
the [CMR Search API](https://cmr.earthdata.nasa.gov/search/site/docs/search/api.html),
and stops after that response. It needs no NASA account or token. It downloads
collection metadata only. The `gov.nasa.eosdis` tag selects an initial catalog
scope; whether each collection fits our NASA product definition still needs
review. No pagination, granule downloads, or GitHub searches run automatically.

Success demonstrates that we can reach CMR and extract traceable candidate terms.
It does not yet tell us whether those terms find relevant GitHub repositories,
whether a mention represents use, or how complete the product catalog will be.
Filename conventions and additional aliases will need further evidence.

Output is formatted JSON, which is readable on screen and can also be saved:

```console
nasa-product-preview --short-name ATL03 > first-product.json
```

Status messages go to standard error so the saved JSON contains only the result.
Exit codes are 0 for a result, 1 for an error, 2 for invalid arguments, and 3 for
no matching collection. A missing match is reported explicitly. The default
connection/read timeout is 20 seconds; `--timeout 10` changes it. This is a
socket timeout, not a guaranteed total runtime. Ctrl+C interrupts the request.
TLS uses Certifi to support Windows/Conda certificate handling; `--ca-bundle PATH`
or `SSL_CERT_FILE` can supply a private certificate authority bundle if required.

If the command is not on your PATH, use the same Python environment directly:

```console
python -m nasa_eo_search.cmr --short-name ATL03
```

After inspecting the returned terms, a separate manual Phase 2 check can use the
existing search command:

```console
sourcegraph-search --trace "repo:^github.com/ ATL03 count:10 timeout:15s"
```

This searches Sourcegraph's indexed GitHub repositories and returns candidate
references for inspection. It is not a measure of complete GitHub coverage.

Run the offline test suite with:

```console
python -m unittest discover -s tests -v
```

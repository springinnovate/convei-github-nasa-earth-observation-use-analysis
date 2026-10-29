# Web discovery pilot — issue #34

On 2026-10-29, adding web search recovered NASA RHEAS, which the preceding
GitHub discovery pilot missed. Four of the five independent reference
repositories were discovered. The nightingale repository remained missing.
All twenty selected repositories downloaded and scanned; selection did not
force known examples into the sample.

## Fixed inputs and results

The pilot used the same saved catalog, five products, cited MUR alias for GitHub
discovery, two GitHub pages per term/source, twenty selected repository attempts,
100 MB per archive, and 500 MB total. Web discovery added one paid model request
per canonical product. Each requested these queries, frozen before the run:

- `site:github.com "SHORT_NAME"`
- The same query plus up to six meaningful words from the saved catalog title.

| Product | Added catalog-title words |
| --- | --- |
| MOD13Q1 | MODIS Terra Vegetation Indices |
| GPM_3IMERGDF | GPM IMERG Final Precipitation |
| SPL3SMP | SMAP Radiometer EASE Soil Moisture |
| SRTMGL1 | Shuttle Radar Topography Mission |
| MUR-JPL-L4-GLOB-v4.1 | GHRSST MUR Foundation Sea Surface Temperature |

No known repository names, handpicked candidates, or control-specific query
terms were supplied. All ten requested query strings appeared unchanged in the
tool metadata. Earlier targeted searches containing `drought` or `SPL3SMP.004`
are not counted as discoveries in this pilot; neither term was derived by the
frozen rule. Query rules were not revised after inspecting these results.

| Measure | Previous pilot (#31) | With web discovery |
| --- | ---: | ---: |
| GitHub discovery requests | 21 | 21 |
| Paid web model requests | 0 | 5 |
| Combined candidate repositories | 1,277 | 1,294 |
| Independent reference repositories discovered | 3/5 | 4/5 |
| Selected / scanned snapshots | 20 / 17 | 20 / 20 |
| Download failures | 3 | 0 |

Web search supplied 43 distinct public repository candidates, of which sixteen
were absent from the other search sources in this run. The current GitHub-only
candidate set was 1,278, one more than in the preceding run; the entire increase
of seventeen should not be attributed to web search. Sources are ranked and
incomplete, and this convenience sample does not estimate GitHub-wide recall.

## What happened to the known examples

The [comparison CSV](web-discovery-controls.csv) contains pinned independent
evidence links, source provenance, and separate discovery and scan outcomes.

| Reference repository | Current outcome |
| --- | --- |
| BritishTrustForOrnithology/nightingale-connectivity | Not discovered by the fixed queries |
| rehsani/imerg-dggs | Discovered by web and repository search; selected and scanned, producing three GPM product/file rows |
| nasa/RHEAS | Newly discovered through web search; outside the twenty-repository sample |
| csdms/bmi-topography | Discovered by web and repository search; outside the sample |
| lkuiucsb/Sea-Surface-temperature | Discovered by web and repository search; outside the sample |

The earlier sample selected Sea-Surface-temperature; this sample selected
imerg-dggs instead. Finding more candidates does not ensure that every reference
repository enters a fixed-size sample. Distinct repository names remain a
selection heuristic, not proof of independent adoption.

## Usage, limits, and validation

The run used `gpt-6.1-sol` for five paid Responses requests, with five reported
web tool calls containing ten search strings. Reported usage was 64,045 input
tokens and 2,499 output tokens. GitHub additionally received 46 public-candidate
metadata checks; these are separate from the discovery-request cap and snapshot
downloads. No classification requests were made. Dollar cost is not estimated.

Only URLs from completed web-tool source records were accepted. Public
visibility was checked before saving candidate details. Model-written prose was
not used as discovery evidence. Each response allowed at most two web tool calls,
2,000 output tokens, twenty candidate checks, and five source URLs per repository.

The twenty ZIPs occupied 148,034,659 bytes. Six scans reported no exclusions;
fourteen reported skipped files. The run therefore retains `status: incomplete`
and `search_complete: false`. Its 11,983 dataset/file rows span the entire saved
catalog and are candidate references, not verified data uses or an accuracy score.

All 454 automated tests passed. A real saved-run resume made zero network calls
and read no key, preserving checksums for 69 plan, receipt, archive, scan, and
match files. Tests cover source validation, public-only persistence, request
bounds, frozen queries/model, key safety, explicit retries, and legacy behavior.

## Reproduce a bounded run

Install the branch, keep the existing `nasa-catalog`, and use the same
`discovery-aliases.csv` shown in the [preceding pilot](discovery-coverage-pilot.md).
Choose a fresh output directory:

```console
nasa-eo search nasa-catalog --short-name MOD13Q1 --short-name GPM_3IMERGDF --short-name SPL3SMP --short-name SRTMGL1 --short-name MUR-JPL-L4-GLOB-v4.1 --discovery-aliases discovery-aliases.csv --web-search-model gpt-6.1-sol --api-key-file secrets/openai.key --github-pages 2 --max-repositories 20 --max-archive-mb 100 --max-total-mb 500 --max-requests 26 --output web-discovery-pilot
```

Add `--dry-run` to inspect the configuration without requests or key reads.
Repeat the same output directory to reuse saved work. Live discovery results
can change. The [OpenAI web-search documentation](https://developers.openai.com/api/docs/guides/tools-web-search)
describes the hosted tool and source metadata used by this command.

Detailed local outputs remain under `search-results/issue34-web-discovery-pilot/`:
`frozen-web-query-plan.json`, `pilot-summary.json`, `coverage-comparison.csv`,
`selected-repositories-comparison.csv`, `resume-check.json`, and `search/`.
Generated archives and full reports are not committed. Earlier pilot outputs
remain intact.

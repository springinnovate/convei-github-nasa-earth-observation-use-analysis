# Discovery coverage pilot — issue #31

On 2026-10-29, the updated command found three of five independently identified
reference repositories; the earlier code-only pilot found none. One entered the
20-repository sample and scanned successfully. The other two were discovered but
fell outside that sample. These five examples are retrieval checks, not a random
sample or an estimate of coverage across GitHub.

Both runs used the same saved NASA catalog, five products, two pages per search,
20 selected repository attempts, 100 MB per archive, and 500 MB total. The new
run adds repository search across names, descriptions, and READMEs, the documented
MUR alias `jplMURSST41`, and selection that prefers distinct repository names.
Failed downloads count toward the 20-repository limit.

| Measure | Earlier pilot | Updated pilot |
| --- | ---: | ---: |
| Discovery requests | 10 | 21 |
| Candidate repositories | 581 | 1,277 |
| Reference repositories discovered | 0/5 | 3/5 |
| Selected repositories | 20 | 20 |
| Distinct names among selected repositories | 14 | 20 |
| Snapshots scanned | 16 | 17 |
| Download failures | 4 | 3 |

The updated run used 12 code-search and 9 repository-search requests, below its
24-request cap. Repository search contributed 647 candidates absent from code
search in that run. The alias contributed 94 candidates absent from all five
canonical-term searches. These counts overlap; they should not be added.
Different request counts and changing indexes prevent attributing the full
before/after difference to one change. Distinct repository names are a diversity
heuristic, not proof of independent projects or independent adoption.

## Independent reference checks

The [comparison CSV](discovery-coverage-controls.csv) includes pinned source
links and separate discovery, selection, and scan outcomes. Three controls came
from the earlier pilot; two additional controls were recorded before inspecting
this run's results. No control repository was inserted into discovery or selection.

| Dataset | Reference repository | Updated outcome |
| --- | --- | --- |
| MOD13Q1 | BritishTrustForOrnithology/nightingale-connectivity | Not discovered |
| GPM_3IMERGDF | rehsani/imerg-dggs | Found by repository search; outside the 20-repository sample |
| SPL3SMP | nasa/RHEAS | Not discovered |
| SRTMGL1 | csdms/bmi-topography | Found by repository search; outside the sample |
| MUR-JPL-L4-GLOB-v4.1 | lkuiucsb/Sea-Surface-temperature | Found by both canonical and alias repository searches; scanned, with two matching files |

The MUR result does not establish that the alias was necessary to find that
repository. Aliases broaden discovery only; local matches still require catalog
identifiers. Alias-only use can therefore remain absent from `matches.csv`.

Separate repository-restricted diagnostics returned zero code-search and
repository-search hits for both missing controls, with `incomplete_results=false`.
Their pinned READMEs omit the canonical product names. README discovery therefore
has no literal identifier to find in those files. The code-search misses persist
even with the repository specified; the response does not explain why. Wider
global pagination alone has not been shown to fix them.

Diagnostics made five repository-search calls before the pilot and four targeted
search calls afterward, separately from the 21 pilot calls. Four initial targeted
attempts failed in the restricted environment before the successful retries. None
of these diagnostics added a candidate or downloaded another snapshot.

## Limits and checks

Three archives exceeded the 100 MB download limit: `Air-Quality-Portal/veda-config-aq`,
`kugeceo/diqiu.luhui.net`, and `ECO2ZTS/GSIF_tutorials`. Their partial downloads
were retained and counted against the total byte budget. Completed ZIPs used
178,901,269 bytes; partial downloads used 300,000,003 bytes, including one overflow
detection byte each. Of the 17 scans, three reported no exclusions and fourteen
reported exclusions. The run correctly retains `status: failed` and
`search_complete: false`; its saved results remain usable.

The new match table contains 5,116 dataset/file rows across the entire saved
catalog. A row is a candidate reference, not verified data use. The earlier
snapshots produced 15,195 rows under the same dataset-identity policy. Different
selection and repeated catalog copies make raw row totals unsuitable as a
coverage or accuracy score. No GPT classification calls were made.

All 422 automated tests passed. A real resume with external operations blocked
made zero network calls and preserved checksums for 61 saved evidence, plan, and
match files. The existing failed downloads were not retried automatically.

## Reproduce a bounded run

After installing the branch, save this as `discovery-aliases.csv`:

```csv
short_name,term,source_url
MUR-JPL-L4-GLOB-v4.1,jplMURSST41,https://coastwatch.pfeg.noaa.gov/erddap/info/jplMURSST41/index.html
```

The [NOAA metadata](https://coastwatch.pfeg.noaa.gov/erddap/info/jplMURSST41/index.html)
links this identifier to the NASA MUR product. From the repository directory,
choose a fresh output directory:

```console
nasa-eo search nasa-catalog --short-name MOD13Q1 --short-name GPM_3IMERGDF --short-name SPL3SMP --short-name SRTMGL1 --short-name MUR-JPL-L4-GLOB-v4.1 --discovery-aliases discovery-aliases.csv --github-pages 2 --max-repositories 20 --max-archive-mb 100 --max-total-mb 500 --max-requests 24 --output discovery-pilot
```

Saved local pilot evidence is under `search-results/issue31-discovery-pilot/`:
`pilot-summary.json`, `coverage-comparison.csv`, `selected-repositories-comparison.csv`,
`missing-controls-diagnostic.json`, `resume-check.json`, and the `search/`
receipts, archives, and match tables. These
large generated files are not committed. The previous pilot outputs remain intact.

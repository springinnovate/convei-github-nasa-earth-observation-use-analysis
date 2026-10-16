# Sourcegraph search reliability trial — 2026-10-15

## Result

Keep issue #22's pull request in draft. The request cap, saved progress,
bounded retries, and automatic failure stop work, but nine fixed repository
groups do **not** yet provide reliable discovery of public NASA references.
Do not restart the full catalog on the strength of these results.

The original `nasa-full-search` directory was left unchanged. This trial used
new directories under the ignored `search-results/` directory. It made no NASA
catalog requests, OpenAI requests, or classification calls.

## Small catalog searches

The saved catalog contributed three identifiers for each selected dataset.
Each identifier was split into nine repository groups, with a 10-second server
timeout, at most two attempts per group, and sequential requests within each
run. Some diagnostic runs overlapped, so timings are observations, not an
isolated performance benchmark. A server timeout is not a total wall-clock cap.

| Dataset reached | Requests made, including retries | Saved candidate file rows | Repositories | Sum of request time | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| ARIA_S1_GUNW | 6 | 0 | 0 | 73.0 seconds | Incomplete; initial invocation stopped on repeated failures, then a one-request resume test paused at its cap. |
| ATL03 | 13 | 80 | 9 | 155.9 seconds | Incomplete; stopped on repeated unsuccessful responses. |
| ASF_ECMWF_SMST | 6 | 0 | 0 | 82.6 seconds | Incomplete; stopped on repeated failures. |

These are partial runs, not completed evaluations of all nine identifiers.
ATL03's 80 rows include potentially unrelated identifiers, such as Nautobot's
site abbreviation; they are not 80 confirmed examples of NASA data use.

The initial combined run selected all three datasets but stopped while searching
ARIA. Separate runs reached ATL03 and ASF_ECMWF_SMST. Commands below include
`--backend sourcegraph` to select the same service in the current CLI:

```console
python -m nasa_eo_search.pipeline search nasa-catalog --backend sourcegraph --short-name ATL03 --short-name ARIA_S1_GUNW --short-name ASF_ECMWF_SMST --output search-results/issue22-live-pilot --max-requests 5
python -m nasa_eo_search.pipeline search nasa-catalog --backend sourcegraph --short-name ATL03 --output search-results/issue22-atl03-pilot --max-requests 27
python -m nasa_eo_search.pipeline search nasa-catalog --backend sourcegraph --short-name ASF_ECMWF_SMST --output search-results/issue22-rare-pilot --max-requests 27
```

Repeating the first command with `--max-requests 1` made exactly one additional
request, skipped the completed group and exhausted retry budgets, and preserved
the completed attempt's files byte-for-byte. Its final stop reason was
`request_limit_reached`.

## Known-reference controls

These controls used the same identifier-boundary expression, public visibility,
fork/archive inclusion, `count:all`, and 10-second server timeout. Repository
filters were added to the normal query; the resulting files stayed within those
filters.

| Search | Returned files | Repositories | Request time | Service result |
| --- | ---: | ---: | ---: | --- |
| ARIA_S1_GUNW, exact `github.com/ASFHyP3/hyp3` | 11 | 1 | 5.9 seconds | Finished, no reported limits. |
| ATL03, exact `github.com/pydap/pydap` | 9 | 1 | 7.5 seconds | Finished, no reported limits. |
| ASF_ECMWF_SMST_1, exact `github.com/ASFHyP3/hyp3` | 0 | 0 | 5.1 seconds | Finished, no reported limits. |
| ARIA_S1_GUNW, global literal without boundary wrapper | 0 | 0 | 10.5 seconds | Finished, no reported limits. |
| ARIA_S1_GUNW, owner prefix `a`, case-insensitive | 0 | 0 | 17.5 seconds | 4,096 repositories timed out. |
| ARIA_S1_GUNW, owner prefix `as`, case-insensitive | 19 | 2 | 5.4 seconds | Finished, no reported limits. |
| ARIA_S1_GUNW, repeated exact `github.com/ASFHyP3/hyp3` | 11 | 1 | 2.9 seconds | Finished, no reported limits. |

The `as` search recovered all 11 hyp3 files plus eight files from
`github.com/asfadmin/Discovery-asf_search`. Both repositories belong to the
broader digits/A–C group. That broader group's retry returned zero files with
no reported limits. Removing the identifier-boundary wrapper did not fix the
global miss. These observations demonstrate a discovery gap; they do not
establish its underlying service cause or validate two-letter groups generally.

Repository-scoped successes do not prove that those repositories were included
in global search. [Sourcegraph documents different default treatment of
unindexed repositories](https://sourcegraph.com/docs/dotcom) in global and scoped
queries. The measured problem is scope-dependent discovery, not proof that the
same indexed corpus was searched incorrectly.

The rare identifier has no known positive control here. Zero returned files
therefore cannot establish that it is unused.

Control receipts are in `search-results/issue22-controls/`. Additional scope
diagnostics are in
`search-results/issue22-diagnostics/agent-aria-20261015T211827430377Z/`.
Each receipt retains the exact query, timestamps, raw matches, and event stream.
These local evidence files are intentionally not committed.

## Implementation checks and remaining limit

Automated tests exercise request caps, restart behavior, exact saved queries,
retry exhaustion, HTTP retry delays, incomplete zero-hit responses, interrupted
and corrupt attempts, old output compatibility, and consistent completion in
CSV reports and classification inputs. An offline wheel build also succeeded.

The live trial also exposed an empty-group case: Sourcegraph can send an explicit
"No repositories found" alert together with final, zero-hit completion. For a
validated repository partition only, that fully completed empty scope is now
treated as zero hits, with the alert retained as a notice. Other alerts, missing
completion evidence, and timeout warnings remain incomplete. Raw receipts are
unchanged. The two such responses in the ATL03 trial preceded this correction.

Nine groups multiply the current 33,273 identifiers into 299,457 planned queries
before retries. Even if every group finished, this is not demonstrated to be an
acceptable full-catalog runtime. The draft provides safer execution and useful
diagnostic evidence, not a validated all-repository harvesting strategy.

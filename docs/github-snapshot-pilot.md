# GitHub discovery and snapshot pilot — 2026-10-15

The integrated `nasa-eo search` and `nasa-eo classify` workflow completed a
bounded live smoke test. Discovery, local scanning, report generation, resume,
and three paid classifications worked. This validates the connection between
the commands, not search coverage or classification accuracy.

## Search and resume

The existing catalog contained 10,716 dataset families and 33,273 candidate
identifiers. Three short names were used for remote discovery; all catalog
identifiers were searched locally in each selected snapshot.

```console
python -m nasa_eo_search.pipeline search nasa-catalog --short-name ATL03 --short-name ARIA_S1_GUNW --short-name ASF_ECMWF_SMST --github-pages 2 --max-repositories 3 --max-archive-mb 25 --max-total-mb 75 --max-requests 3 --output search-results/issue24-github-pilot
python -m nasa_eo_search.pipeline search nasa-catalog --max-requests 2 --output search-results/issue24-github-pilot
```

The first invocation saved three discovery pages and scanned three snapshots.
The second fetched only the next two pages, retaining the original selection
and limits. Saved archives and raw scan receipts were byte-for-byte unchanged
after resume.

| Discovery term | Pages saved | Public candidate files | Query status |
| --- | ---: | ---: | --- |
| ATL03 | 2 | 200 | Capped; more results remained. |
| ARIA_S1_GUNW | 2 | 163 | Returned all pages available for this query. |
| ASF_ECMWF_SMST | 1 | 4 | Returned all pages available for this query. |

Five discovery calls identified 123 distinct candidate repositories. The saved
three-repository cap selected these snapshots, with no manually inserted
positive controls:

| Repository | Pinned commit | Files scanned |
| --- | --- | ---: |
| `auspatious/pyTMD` | `f034418e0db512299615c035deb338147b2bab6c` | 219 |
| `ACCESS-Cloud-Based-InSAR/hyp3` | `05306c151183720be1484bbc75fbdc85573d46ea` | 159 |
| `mfangaritav/Discovery-asf_search` | `54bc486a0578887e57d8fb88dcfbc3debae7c4d3` | 183 |

The local scans produced **471 dataset/file rows**, including matches to
products beyond the three discovery terms. The largest readable CSV snippet
was 7,511 characters. Full raw matches remain in the saved scan receipts.

Search status was `incomplete`, as expected: page/repository caps and snapshot
file exclusions remained. Each selected snapshot retained its skipped-file
reasons. Neither `pydap/pydap` nor `ASFHyP3/hyp3`, known examples from earlier
checks, appeared in the returned broad-discovery sample. Forks did appear;
separate repositories are not necessarily independent examples of use.
No result establishes exhaustive GitHub coverage.

## Classification

A dry run prepared 471 file assessments with no blocked files. The subsequent
paid run was explicitly limited to three new requests:

```console
python -m nasa_eo_search.pipeline classify search-results/issue24-github-pilot --model gpt-6.1-sol --max-output-tokens 8192 --max-requests 3 --output search-results/issue24-github-classified --dry-run
python -m nasa_eo_search.pipeline classify search-results/issue24-github-pilot --model gpt-6.1-sol --api-key-file ./secrets/openai.key --max-output-tokens 8192 --max-requests 3 --output search-results/issue24-github-classified
```

All three requests succeeded: **3 complete, 468 pending, 0 failed, 0 blocked**.
Recorded usage was 4,018 input tokens and 757 output tokens. No additional model
calls were made. Repeating the paid command would authorize another group of
up to three requests; it is not a cost-free status check.

The three assessed files were `deploy-daac-prod.yml`, `deploy-daac-test.yml`,
and `deploy-edc-sandbox.yml` under `.github/workflows/` in the selected hyp3
fork. Each referenced `job_spec/ARIA_S1_GUNW.yml`. All three judgments were
`unclear`: the supplied file excerpts identify a product-specific configuration
but do not show the referenced job implementation or establish actual data use.
These similar files are a transport/report smoke test, not a varied accuracy
sample. Other files are assessed separately by the current file-level workflow.

## Automated checks and saved evidence

The full offline suite passed: 285 tests. Coverage includes pagination and
resume, public-only discovery receipts, archive limits and unsafe members,
identifier boundaries, bounded excerpts, immutable evidence, classification
integration, and compatibility with existing Sourcegraph outputs. An offline
wheel build also succeeded.

Local evidence is under `search-results/issue24-github-pilot/` and
`search-results/issue24-github-classified/`. These ignored directories contain
the reports and receipts; downloaded code, model responses, and credentials
are not committed. Existing user catalog and search outputs were left unchanged.

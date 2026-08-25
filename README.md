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

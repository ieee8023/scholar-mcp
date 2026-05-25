# Semantic Scholar Python client

A lightweight, synchronous Python client to query the Semantic Scholar API. It mirrors the core functionality used by the Semantic Scholar FastMCP server but is designed for simple script/agent use.

- Works with or without an API key (higher rate limits with a key)
- Handles basic rate limiting and automatic retry on HTTP 429 using `Retry-After`
- Exposes search, details, batch, author, and recommendation endpoints
- **Built-in response caching** to avoid redundant API calls (stored in `~/.cache/semanticscholar/`)

## MCP servers

This package ships two MCP servers with stdio transport. Configure your MCP client to launch:

- `scholar-mcp-scholar` (Semantic Scholar tools)
- `scholar-mcp-download` (PDF download + text extraction)

Tool IDs exposed:

- `scholar-mcp.scholar.search`
- `scholar-mcp.scholar.get`
- `scholar-mcp.scholar.relations`
- `scholar-mcp.scholar.author`
- `scholar-mcp.scholar.recommendations`
- `scholar-mcp-download-extract`

The download tool requires `output_dir` and writes the PDF, extracted text, and `.pdf_cache.json` under that directory. For MCP clients, pass an `output_dir` that points to a `papers` folder inside the workspace (for example: `<workspace>/papers`). Responses include file paths only.

### What to pass as the paper ID

For `scholar-mcp-download-extract`, pass the same kind of identifier you’d use with `scholar-mcp.scholar.get`:

- Best: Semantic Scholar `paperId` from search/get results (a hash-like string).
- Also good: `DOI:<doi>`.
- Also accepted: a bare DOI like `10.1038/...` or a DOI URL like `https://doi.org/10.1038/...`.

Examples:

- `paper_id_or_doi="649def34f8be52c8b66281af98ae884c09aef38b"`
- `paper_id_or_doi="DOI:10.1038/s41746-023-00919-1"`
- `paper_id_or_doi="10.1038/s41746-023-00919-1"`

## Install

```bash
pip install requests
```

Optionally pin in a requirements.txt:

```txt
requests>=2.31
```

## Quick start

```python
from tools.semanticscholar import SemanticScholarClient

s2 = SemanticScholarClient()  # or SemanticScholarClient(api_key="...")

# 1) Relevance search
res = s2.search_papers(
    query="explainable AI medical imaging",
    fields=["paperId", "title", "year", "citationCount", "url"],
    fields_of_study=["Medicine", "Computer Science"],
    open_access_pdf=True,
    limit=10,
)
for p in res.get("data", []):
    print(p["paperId"], p.get("year"), p.get("citationCount"), p["title"]) 

# 2) Get a specific paper (by S2 ID, DOI: prefix, or URL: prefix)
paper = s2.get_paper("DOI:10.1038/s41746-023-00919-1", fields=["title", "abstract", "year", "authors", "venue", "url"])
print(paper["title"], paper.get("year"))

# 3) Find a paper by title
match = s2.title_search("Why Should I Trust You? Explaining the Predictions of Any Classifier", fields=["year", "authors", "url"])
print(match.get("paperId"), match.get("title"))

# 4) Authors of a paper
authors = s2.paper_authors("CorpusId:215416146", fields=["name", "affiliations"], limit=50)
print(len(authors.get("data", [])), "authors")

# 5) Author search and papers
alist = s2.search_authors("Cynthia Rudin", fields=["url", "paperCount", "citationCount"], limit=5)
if alist.get("data"):
    author_id = alist["data"][0]["authorId"]
    pubs = s2.author_papers(author_id, fields=["title", "year", "venue", "url"], limit=20)
    print("papers fetched:", len(pubs.get("data", [])))

# 6) Recommendations for a paper
recs = s2.recommend_for_paper(
    "649def34f8be52c8b66281af98ae884c09aef38b",
    fields="title,year,authors,url",
    limit=20,
    from_pool="recent",
)
print("recommended:", len(recs.get("recommendedPapers", [])))
```

## API overview

All methods return parsed JSON (dict/list). Errors raise `SemanticScholarError`.

- search_papers(query, fields=None, publication_types=None, open_access_pdf=False, min_citation_count=None, year=None, venue=None, fields_of_study=None, offset=0, limit=10)
- bulk_search(query=None, token=None, fields=None, sort=None, publication_types=None, open_access_pdf=False, min_citation_count=None, publication_date_or_year=None, year=None, venue=None, fields_of_study=None)
- title_search(query, fields=None, publication_types=None, open_access_pdf=False, min_citation_count=None, year=None, venue=None, fields_of_study=None)
- get_paper(paper_id, fields=None)
- get_papers_batch(paper_ids, fields=None)
- paper_authors(paper_id, fields=None, offset=0, limit=100)
- paper_citations(paper_id, fields=None, offset=0, limit=100)
- paper_references(paper_id, fields=None, offset=0, limit=100)
- search_authors(query, fields=None, offset=0, limit=100)
- get_author(author_id, fields=None)
- author_papers(author_id, fields=None, offset=0, limit=100)
- author_batch_details(author_ids, fields=None)
- recommend_for_paper(paper_id, fields=None, limit=100, from_pool="recent")
- recommend_for_papers(positive_paper_ids, negative_paper_ids=None, fields=None, limit=100)

Notes
- `fields` accepts a list of field names (e.g., ["title", "year", "citationCount", "authors"]) or a comma string in recommendation methods.
- If `fields` is omitted, the client now requests a larger default field set for papers and authors.
- For IDs you can use: S2 `paperId`, `CorpusId:...`, `DOI:...`, `ARXIV:...`, `PMID:...`, or `URL:...` for supported domains.
- Without an API key, rate limits are lower; the client spaces requests and retries once on 429.
- **Responses are automatically cached** in `~/.cache/semanticscholar/` to avoid redundant API calls and reduce rate limit issues.

## Caching

The client automatically caches all API responses to `~/.cache/semanticscholar/` (configurable). This helps:
- Avoid redundant API calls when running scripts multiple times
- Reduce the risk of hitting rate limits
- Speed up development and testing

```python
# Caching is enabled by default
s2 = SemanticScholarClient()

# Disable caching if needed
s2_no_cache = SemanticScholarClient(use_cache=False)

# Use a custom cache directory
s2_custom = SemanticScholarClient(cache_dir="/path/to/cache")
```

Cache keys are generated based on:
- HTTP method (GET/POST)
- Full URL
- Request parameters
- JSON body (for POST requests)

Identical requests will retrieve the cached response without making an API call.

## Recipes for XAI-in-medicine literature

- Find clinical/medical explanation studies with OA PDFs:

```python
res = s2.search_papers(
    query="(explain* OR interpret*) AND (clinical OR medicine OR healthcare)",
    fields=["paperId", "title", "year", "citationCount", "publicationTypes", "url", "openAccessPdf"],
    publication_types=["JournalArticle", "ClinicalTrial", "Study"],
    fields_of_study=["Medicine", "Computer Science"],
    open_access_pdf=True,
    year="2016-",
    limit=50,
)
```

- Expand a seed paper list into related work:

```python
seed = ["DOI:10.1038/s41746-023-00919-1"]
recs = s2.recommend_for_papers(seed, fields="title,year,venue,url", limit=50)
```

- Build an author-centric view:

```python
alist = s2.search_authors("interpretability medical", fields=["paperCount", "citationCount"], limit=20)
for a in alist.get("data", []):
    details = s2.get_author(a["authorId"], fields=["name", "affiliations", "hIndex", "url"]) 
```

## Tips

- Respect rate limits. If you need higher throughput, obtain an API key from Semantic Scholar.
- Validate you only request fields you need; large nested fields can be slow and rate limited.
- Use batch endpoints (paper/author) when you already have IDs.
- **Leverage caching**: Cached responses are instant and don't count toward rate limits.
- Clear cache periodically if data freshness is critical: `rm -rf ~/.cache/semanticscholar/`

## Troubleshooting

- HTTP 429: The client will wait and retry once using `Retry-After`. Consider slowing down or using an API key. Cached responses help avoid this.
- Cache issues: If you suspect stale data, delete the cache directory and re-run your script.
- HTTP 404: IDs or title queries may not resolve; double-check identifier formats.
- JSON errors: Check `fields` syntax and allowed field names in the official API docs.
 - JSON errors: Check `fields` syntax and allowed field names in the official API docs.
     Important: do NOT request a top-level `doi` field — the Graph API does not accept `doi` as a paper field
     and will return a 400 error. To obtain DOI values, request `externalIds` in `fields` (it contains DOI and
     other external identifiers), or fetch a paper directly with `get_paper("DOI:10.1038/...")`.
     Common allowed paper fields include: `paperId`, `corpusId`, `title`, `abstract`, `venue`, `year`,
     `publicationDate`, `publicationTypes`, `journal`, `authors`, `citationCount`, `referenceCount`,
     `influentialCitationCount`, `isOpenAccess`, `openAccessPdf`, `fieldsOfStudy`, `s2FieldsOfStudy`,
     `tldr`, `externalIds`, `url`.

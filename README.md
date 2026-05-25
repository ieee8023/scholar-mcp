# scholar-mcp

MCP servers wrapping Semantic Scholar search/get APIs plus a PDF download + text extraction helper.

**Tools**
- `scholar-mcp.scholar.search`
- `scholar-mcp.scholar.get`
- `scholar-mcp.scholar.relations`
- `scholar-mcp.scholar.author`
- `scholar-mcp.scholar.recommendations`
- `scholar-mcp.download.extract`
 - `scholar-mcp-download-extract`

**What ID to pass to `scholar-mcp-download-extract`**
- Preferred: Semantic Scholar `paperId` returned by search/get.
- Also supported: `DOI:<doi>`, a bare DOI (`10.*/...`), or a DOI URL (`https://doi.org/...`).

More examples: see [tools/semanticscholar.md](tools/semanticscholar.md).


## To install

Clone this repo and install the package with `pip install -e .` and then add the following to your mcp server list:

```
{
    "servers": {
        "scholar-mcp-scholar": {
            "command": "scholar-mcp-scholar",
            "args": [
                "--semantic-scholar-api-key", "REQUEST AT https://www.semanticscholar.org/product/api",
            ]
        },
        "scholar-mcp-download": {
            "command": "scholar-mcp-download",
            "args": [
                "--download-domain", "https://SCIHUB_DOMAIN",
                "--unpaywall-email", "test@test.com"
            ]
        }
    }
}
```

Otherwise in this package path put

## Example download_config.json

```
{
  "download_domain": "https://SCIHUB_DOMAIN",
  "unpaywall_email": "test@test.com"
}
```

## Example semanticscholar.config.json

```
{
  "SEMANTIC_SCHOLAR_API_KEY": "KEY"
}
```

## Download tool: behavior and steps

The download helper (`tools/download_paper_text.py` and the MCP tool `scholar-mcp-download-extract`)
attempts to obtain a PDF and extract text using multiple strategies in sequence. It tries the least
invasive and most reliable sources first, and falls back progressively when a source is unavailable.

Sequence of download strategies (attempted in this order):

1. Semantic Scholar `openAccessPdf` URL from the paper metadata (fast, preferred when available).
2. Semantic Scholar landing page (follow links on the paper's page to find a PDF or OA link).
3. Direct arXiv PDF download:
   - 3a. If an arXiv ID exists in Semantic Scholar metadata, request the arXiv PDF URL.
   - 3b. If the DOI itself encodes an arXiv identifier (e.g. `10.48550/arXiv.XYZ`), construct the arXiv PDF URL.
4. Unpaywall API lookup (requires a DOI): requests an OA location from Unpaywall and follows the OA PDF link.
5. PMC access via PMCID mapping (if DOI maps to a PMCID): attempts to download from PMC.
6. arXiv search by title (if no arXiv ID is known but title is available): performs a title-based arXiv lookup.

Validation and extraction:

- After downloading a candidate PDF, the tool validates it by extracting metadata (DOI and title) and
  comparing against the expected DOI or title similarity (fallback threshold). This helps avoid saving
  unrelated PDFs.
- Text extraction prefers `PyMuPDF` (fitz) when available, falling back to `pdfminer.six`.
- The tool writes artifacts to the given `output_dir` as `<safe_id>.pdf` and `<safe_id>.txt`, and records
  metadata in a small cache (`tools/.pdf_cache.json`) to avoid re-downloading the same files repeatedly.

Configuration:

- `tools/download_config.json` (optional) can supply `download_domain` (for site-specific mirrors) and
  `unpaywall_email` (for Unpaywall API usage).
- `tools/http_headers.json` can contain per-host headers/cookies to apply when requesting certain domains.

Usage (script):

```bash
PYTHONPATH=. python tools/download_paper_text.py --output-dir papers "DOI:10.1038/s41746-023-00919-1"
```

Usage (MCP): call the tool `scholar-mcp-download-extract` (or the server's registered tool name) with
`paper_id_or_doi` and `output_dir` (recommended: a `papers` folder in your workspace). The tool returns
file paths for the downloaded PDF and extracted text.

If you'd like, I can also:
- Add a concise example showing the returned JSON from the MCP `download.extract` tool.
- Add a small diagram or flowchart that visualizes the sequence of strategies.

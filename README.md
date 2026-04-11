# scholar-mcp

MCP servers wrapping Semantic Scholar search/get APIs plus a PDF download + text extraction helper.

**Tools**
- `scholar-mcp.scholar.search`
- `scholar-mcp.scholar.get`
- `scholar-mcp.scholar.relations`
- `scholar-mcp.scholar.author`
- `scholar-mcp.scholar.recommendations`
- `scholar-mcp.download.extract`

**What ID to pass to `scholar-mcp.download.extract`**
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
      "args": []
    },
  "scholar-mcp-download": {
      "command": "scholar-mcp-download",
      "args": []
    }
  }
}
```



## Example download_config.json

```
{
  "download_domain": "SOME_SCIHUB_DOMAIN",
  "unpaywall_email": "test@test.com"
}
```

## Example semanticscholar.config.json

```
{
  "SEMANTIC_SCHOLAR_API_KEY": "KEY"
}
```

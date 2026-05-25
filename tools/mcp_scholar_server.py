import contextlib
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

from mcp.server.fastmcp import FastMCP

from .semanticscholar import SemanticScholarClient


_LOGGER = logging.getLogger("scholar-mcp.scholar")
# Write logs to stdout so MCP hosts that mark stderr as warnings don't
# display ordinary INFO logs as warnings.
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
_LOGGER.addHandler(_handler)
_LOGGER.setLevel(logging.INFO)


@contextlib.contextmanager
def _redirect_stdout_to_stderr():
    # Keep prints going to stdout (no-op redirect) so they appear with logs.
    with contextlib.redirect_stdout(sys.stdout):
        yield


def _client() -> SemanticScholarClient:
    return SemanticScholarClient()


mcp = FastMCP("scholar-mcp-scholar")


@mcp.tool(name="scholar-mcp-scholar-search")
def scholar_search(
    query: str,
    fields: Optional[List[str]] = None,
    publication_types: Optional[List[str]] = None,
    open_access_pdf: bool = False,
    min_citation_count: Optional[int] = None,
    year: Optional[str] = None,
    venue: Optional[List[str]] = None,
    fields_of_study: Optional[List[str]] = None,
    offset: int = 0,
    limit: int = 10,
) -> Dict[str, Any]:
    """
    Search papers by relevance.

    Agent instructions for callers (important):
    - Do NOT include the literal field name `doi` in the `fields` parameter: the Semantic Scholar
        Graph API does not accept `doi` as a top-level field and including it will cause a HTTP 400 error.
    - To retrieve DOI values, request `externalIds` in `fields` (it contains DOI and other external identifiers),
        or fetch the paper by DOI using `scholar-mcp.scholar.get` with `paper_id="DOI:<doi>"`.
    - Allowed paper fields you may request include:
        paperId, corpusId, title, abstract, venue, year, publicationDate, publicationTypes, journal,
        authors, citationCount, referenceCount, influentialCitationCount, isOpenAccess, openAccessPdf,
        fieldsOfStudy, s2FieldsOfStudy, tldr, externalIds, url
    - Keep `fields` minimal to avoid large responses and rate limits.
    """
    with _redirect_stdout_to_stderr():
        return _client().search_papers(
            query=query,
            fields=fields,
            publication_types=publication_types,
            open_access_pdf=open_access_pdf,
            min_citation_count=min_citation_count,
            year=year,
            venue=venue,
            fields_of_study=fields_of_study,
            offset=offset,
            limit=limit,
        )


@mcp.tool(name="scholar-mcp-scholar-get")
def scholar_get(
    paper_id: str,
    fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Get a paper by Semantic Scholar identifier.

    What to pass as `paper_id` (most reliable first):
    - Semantic Scholar `paperId` from `scholar-mcp.scholar.search` results, e.g.
      "649def34f8be52c8b66281af98ae884c09aef38b"
    - Prefixed identifiers, e.g. "DOI:10.1038/s41746-023-00919-1" or "CorpusId:215416146"

    Tip: If you're unsure, do a `scholar.search` first and reuse the returned `paperId`.
    
    Agent instruction: when specifying `fields` for `scholar-mcp.scholar.get`, do NOT request a top-level
    `doi` field (it is invalid). Request `externalIds` to obtain DOI values, or pass a DOI as the
    `paper_id` argument (for example: `DOI:10.1038/...`).
    """
    with _redirect_stdout_to_stderr():
        return _client().get_paper(paper_id, fields=fields)


@mcp.tool(name="scholar-mcp-scholar-relations")
def scholar_relations(
    paper_id: str,
    relation: str,
    fields: Optional[List[str]] = None,
    offset: int = 0,
    limit: int = 100,
) -> Dict[str, Any]:
    """Get paper citations or references.

    relation: "citations" or "references".

    `paper_id` should usually be a Semantic Scholar `paperId` from search/get.
    """
    relation_norm = (relation or "").strip().lower()
    if relation_norm not in {"citations", "references"}:
        raise ValueError("relation must be 'citations' or 'references'")

    with _redirect_stdout_to_stderr():
        if relation_norm == "citations":
            return _client().paper_citations(paper_id, fields=fields, offset=offset, limit=limit)
        return _client().paper_references(paper_id, fields=fields, offset=offset, limit=limit)


@mcp.tool(name="scholar-mcp-scholar-author")
def scholar_author(
    author_id: Optional[str] = None,
    query: Optional[str] = None,
    fields: Optional[List[str]] = None,
    offset: int = 0,
    limit: int = 100,
) -> Dict[str, Any]:
    """Get an author by ID or search authors by query."""
    if bool(author_id) == bool(query):
        raise ValueError("Provide exactly one of author_id or query")

    with _redirect_stdout_to_stderr():
        if author_id:
            return _client().get_author(author_id, fields=fields)
        return _client().search_authors(query=str(query), fields=fields, offset=offset, limit=limit)


@mcp.tool(name="scholar-mcp-scholar-recommendations")
def scholar_recommendations(
    paper_id: Optional[str] = None,
    positive_paper_ids: Optional[List[str]] = None,
    negative_paper_ids: Optional[List[str]] = None,
    fields: Optional[Sequence[str]] = None,
    limit: int = 100,
    from_pool: str = "recent",
) -> Dict[str, Any]:
    """Get recommendations from a single seed paper or multiple seeds."""
    if paper_id and positive_paper_ids:
        raise ValueError("Provide either paper_id or positive_paper_ids, not both")
    if not paper_id and not positive_paper_ids:
        raise ValueError("Provide paper_id or positive_paper_ids")

    with _redirect_stdout_to_stderr():
        if paper_id:
            return _client().recommend_for_paper(
                paper_id,
                fields=fields,
                limit=limit,
                from_pool=from_pool,
            )
        positive_ids = list(positive_paper_ids or [])
        if not positive_ids:
            raise ValueError("positive_paper_ids cannot be empty")
        return _client().recommend_for_papers(
            positive_paper_ids=positive_ids,
            negative_paper_ids=negative_paper_ids,
            fields=fields,
            limit=limit,
        )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="scholar-mcp-scholar")
    parser.add_argument("--semantic-scholar-api-key", help="Semantic Scholar API key to use")
    args = parser.parse_args()

    # If CLI provided the API key, set the env var that the client will read.
    if args.semantic_scholar_api_key:
        os.environ["SEMANTIC_SCHOLAR_API_KEY"] = args.semantic_scholar_api_key
        _LOGGER.info("Effective args: SEMANTIC_SCHOLAR_API_KEY provided via CLI")
    else:
        if os.environ.get("SEMANTIC_SCHOLAR_API_KEY"):
            _LOGGER.info("SEMANTIC_SCHOLAR_API_KEY is set in the environment")
        else:
            _LOGGER.info("No CLI API key; using package defaults and env vars.")

    mcp.run()


if __name__ == "__main__":
    main()

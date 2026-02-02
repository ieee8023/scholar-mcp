"""
Lightweight Semantic Scholar API client for Python.

This library mirrors the core capabilities used by the Semantic Scholar FastMCP server
but provides a simple, synchronous interface for scripts/agents.

Features
- Relevance search, bulk search, and title match
- Paper details, authors, citations, references (with pagination)
- Author search, details, and papers (with pagination)
- Batch endpoints for papers and authors
- Recommendations (single seed paper and multi-seed)
- Optional API key via SEMANTIC_SCHOLAR_API_KEY env var or constructor
- Basic per-endpoint rate limiting and automatic 429 retry with Retry-After
- Response caching to ~/.cache/semanticscholar/ to avoid redundant API calls

Usage
- See semanticscholar.md in the project root for examples.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import requests


class SemanticScholarError(Exception):
    """Raised for API, validation, or network errors."""
    pass


@dataclass
class Config:
    api_version: str = "v1"
    base_url: str = f"https://api.semanticscholar.org/graph/v1"
    recs_base_url: str = "https://api.semanticscholar.org/recommendations/v1"
    timeout: int = 30  # seconds

    # rate limits: endpoint substring -> (max_requests, seconds)
    # keep simple: restrict expensive endpoints to ~1 req/sec; default ~10 req/sec
    rate_limits: Mapping[str, Tuple[int, int]] = None  # type: ignore

    def __post_init__(self):
        if self.rate_limits is None:
            self.rate_limits = {
                "/paper/search": (1, 1),
                "/paper/batch": (1, 1),
                "/recommendations": (1, 1),
                "DEFAULT": (10, 1),
            }


class SemanticScholarClient:
    """Simple synchronous client for Semantic Scholar.

    - Handles API key, rate limits, and retries
    - Returns parsed JSON (dict/list) on success
    - Raises SemanticScholarError on failures
    - Caches responses to ~/.cache/semanticscholar/ to avoid redundant API calls
    
    Args:
        api_key: Optional API key (or use SEMANTIC_SCHOLAR_API_KEY env var)
        timeout: Request timeout in seconds (default: 30)
        base_url: Base URL for Graph API (default: https://api.semanticscholar.org/graph/v1)
        recs_base_url: Base URL for Recommendations API (default: https://api.semanticscholar.org/recommendations/v1)
        session: Optional requests.Session instance
        rate_limits: Optional custom rate limits dict
        cache_dir: Directory to store cached responses (default: ~/.cache/semanticscholar)
        use_cache: Enable/disable caching (default: True)
    """

    DEFAULT_PAPER_FIELDS: Sequence[str] = (
        "paperId",
        "corpusId",
        "title",
        "abstract",
        "venue",
        "year",
        "publicationDate",
        "publicationTypes",
        "journal",
        "authors",
        "citationCount",
        "referenceCount",
        "influentialCitationCount",
        "isOpenAccess",
        "openAccessPdf",
        "fieldsOfStudy",
        "s2FieldsOfStudy",
        "tldr",
        "externalIds",
        "url",
    )

    DEFAULT_AUTHOR_FIELDS: Sequence[str] = (
        "authorId",
        "name",
        "aliases",
        "affiliations",
        "paperCount",
        "citationCount",
        "hIndex",
        "homepage",
        "externalIds",
        "url",
    )

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: int = Config.timeout,
        base_url: str = Config.base_url,
        recs_base_url: str = Config.recs_base_url,
        session: Optional[requests.Session] = None,
        rate_limits: Optional[Mapping[str, Tuple[int, int]]] = None,
        cache_dir: Optional[str] = None,
        use_cache: bool = True,
    ) -> None:
        # Resolve API key from (1) parameter (2) env var (3) local config file near the running script/module
        self.api_key = api_key or os.getenv("SEMANTIC_SCHOLAR_API_KEY") or self._load_api_key_from_config()
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.recs_base_url = recs_base_url.rstrip("/")
        self.session = session or requests.Session()
        self.rate_limits = rate_limits or Config().rate_limits
        # track last call per endpoint category
        self._last_call_time: Dict[str, float] = {}
        
        # caching setup
        self.use_cache = use_cache
        if cache_dir is None:
            cache_dir = os.path.expanduser("~/.cache/semanticscholar")
        self.cache_dir = Path(cache_dir)
        if self.use_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _load_api_key_from_config(self) -> Optional[str]:
        """Attempt to load an API key from a JSON config file located next to the running script
        (preferred) or next to this module as a fallback. If no config is present, returns None.

        Supported file name: 'semanticscholar.config.json'
        Supported keys inside JSON: 'SEMANTIC_SCHOLAR_API_KEY', 'api_key', or
        nested {'semantic_scholar': {'api_key': '...'}}
        """
        from pathlib import Path
        import sys

        candidate_files = []
        # 1) Directory of the running script (if available)
        try:
            main_file = getattr(sys.modules.get("__main__"), "__file__", None)
            if main_file:
                candidate_files.append(Path(main_file).parent / "semanticscholar.config.json")
        except Exception:
            pass
        # 2) Current working directory
        try:
            candidate_files.append(Path.cwd() / "semanticscholar.config.json")
        except Exception:
            pass
        # 3) Directory of this module (tools/)
        try:
            candidate_files.append(Path(__file__).parent / "semanticscholar.config.json")
        except Exception:
            pass

        for cfg_path in candidate_files:
            try:
                if cfg_path and cfg_path.exists():
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    # Accept several shapes
                    if isinstance(cfg, dict):
                        if "SEMANTIC_SCHOLAR_API_KEY" in cfg:
                            return str(cfg.get("SEMANTIC_SCHOLAR_API_KEY") or "").strip() or None
                        if "api_key" in cfg:
                            return str(cfg.get("api_key") or "").strip() or None
                        ss = cfg.get("semantic_scholar") if isinstance(cfg.get("semantic_scholar"), dict) else None
                        if ss and ss.get("api_key"):
                            return str(ss.get("api_key") or "").strip() or None
            except Exception:
                # Ignore malformed or unreadable config and continue
                continue
        return None

    # --------------------- internal helpers ---------------------
    def _rate_limit_key(self, endpoint: str) -> str:
        for key in self.rate_limits.keys():
            if key != "DEFAULT" and key in endpoint:
                return key
        return "DEFAULT"

    def _acquire_rate_limit(self, endpoint: str) -> None:
        key = self._rate_limit_key(endpoint)
        max_req, seconds = self.rate_limits.get(key, self.rate_limits["DEFAULT"])  # type: ignore
        # simple: allow 1 call every `seconds` for max_req==1; for higher, we just space by seconds/max
        now = time.time()
        last = self._last_call_time.get(key, 0.0)
        min_spacing = seconds / max(max_req, 1)
        delta = now - last
        if delta < min_spacing:
            time.sleep(min_spacing - delta)
        self._last_call_time[key] = time.time()

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def _cache_key(self, method: str, url: str, params: Optional[Mapping[str, Any]] = None, 
                   json_data: Any = None) -> str:
        """Generate a unique cache key for a request."""
        # Create a deterministic string representation of the request
        parts = [method, url]
        if params:
            # Sort params for consistent hashing
            sorted_params = sorted(params.items())
            parts.append(json.dumps(sorted_params, sort_keys=True))
        if json_data is not None:
            parts.append(json.dumps(json_data, sort_keys=True))
        
        # Hash to create a filename-safe key
        cache_string = "|".join(str(p) for p in parts)
        return hashlib.sha256(cache_string.encode()).hexdigest()

    def _get_from_cache(self, cache_key: str) -> Optional[Any]:
        """Retrieve response from cache if it exists."""
        if not self.use_cache:
            return None
        
        cache_file = self.cache_dir / f"{cache_key}.json"
        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                # If cache is corrupted, ignore it
                return None
        return None

    def _save_to_cache(self, cache_key: str, data: Any) -> None:
        """Save response to cache."""
        if not self.use_cache:
            return
        
        cache_file = self.cache_dir / f"{cache_key}.json"
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f)
        except (IOError, TypeError):
            # If we can't cache it, just continue without caching
            pass

    def _request_with_cache(self, method: str, url: str, params: Optional[Mapping[str, Any]] = None,
                           json_data: Any = None, max_retries: int = 20) -> Any:
        """Common function for making requests with caching support and exponential backoff."""
        # Generate cache key
        cache_key = self._cache_key(method, url, params, json_data)
        
        # Try to get from cache first
        cached_response = self._get_from_cache(cache_key)
        if cached_response is not None:
            return cached_response
        
        # Not in cache, make the actual request with exponential backoff retry
        self._acquire_rate_limit(url)
        
        resp = None
        last_exception = None
        
        for attempt in range(max_retries):
            try:
                if method == "GET":
                    resp = self.session.get(url, params=params, headers=self._headers(), timeout=self.timeout)
                elif method == "POST":
                    resp = self.session.post(url, json=json_data, params=params, headers=self._headers(), timeout=self.timeout)
                else:
                    raise SemanticScholarError(f"Unsupported HTTP method: {method}")
                
                # Handle rate limiting with exponential backoff
                if resp.status_code == 429:
                    if attempt < max_retries - 1:  # Don't sleep on last attempt
                        # Check for Retry-After header
                        retry_after = resp.headers.get("retry-after")
                        if retry_after:
                            try:
                                wait_time = float(retry_after)
                            except ValueError:
                                # If Retry-After is not a number, use exponential backoff
                                wait_time = (2 ** attempt) + (time.time() % 1)  # Add jitter
                        else:
                            # Exponential backoff: 1s, 2s, 4s, 8s, 16s
                            wait_time = (2 ** attempt) + (time.time() % 1)  # Add jitter
                        
                        print(f"Rate limited (429). Waiting {wait_time:.1f}s before retry {attempt + 1}/{max_retries}...")
                        time.sleep(wait_time)
                        continue  # Retry
                    else:
                        # Last attempt failed
                        raise SemanticScholarError(f"HTTP 429: Rate limit exceeded after {max_retries} attempts. {resp.text}")
                
                # Success or other error
                break
                
            except requests.RequestException as e:
                last_exception = e
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) + (time.time() % 1)
                    print(f"Network error: {e}. Retrying in {wait_time:.1f}s... (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait_time)
                    continue
                else:
                    raise SemanticScholarError(f"Network error after {max_retries} attempts: {e}") from e
        
        if resp is None:
            raise SemanticScholarError(f"Request failed after {max_retries} attempts") from last_exception
        
        if not resp.ok:
            raise SemanticScholarError(f"HTTP {resp.status_code}: {resp.text}")
        
        try:
            result = resp.json()
        except ValueError as e:
            raise SemanticScholarError("Invalid JSON response") from e
        
        # Save to cache
        self._save_to_cache(cache_key, result)
        
        return result

    def _get(self, url: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self._request_with_cache("GET", url, params=params)

    def _post(self, url: str, json: Any, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self._request_with_cache("POST", url, params=params, json_data=json)

    def _fields(self, fields: Optional[Sequence[str]], default: Optional[Sequence[str]] = None) -> Optional[str]:
        if not fields:
            if not default:
                return None
            return ",".join(default)
        return ",".join(fields)

    # --------------------- Papers ---------------------
    def search_papers(
        self,
        query: str,
        *,
        fields: Optional[Sequence[str]] = None,
        publication_types: Optional[Sequence[str]] = None,
        open_access_pdf: bool = False,
        min_citation_count: Optional[int] = None,
        year: Optional[str] = None,
        venue: Optional[Sequence[str]] = None,
        fields_of_study: Optional[Sequence[str]] = None,
        offset: int = 0,
        limit: int = 10,
    ) -> Dict[str, Any]:
        if not query or not query.strip():
            raise SemanticScholarError("query cannot be empty")
        params: Dict[str, Any] = {
            "query": query,
            "offset": offset,
            "limit": min(max(limit, 1), 100),
        }
        f = self._fields(fields, self.DEFAULT_PAPER_FIELDS)
        if f:
            params["fields"] = f
        if publication_types:
            params["publicationTypes"] = ",".join(publication_types)
        if open_access_pdf:
            params["openAccessPdf"] = "true"
        if min_citation_count is not None:
            params["minCitationCount"] = int(min_citation_count)
        if year:
            params["year"] = year
        if venue:
            params["venue"] = ",".join(venue)
        if fields_of_study:
            params["fieldsOfStudy"] = ",".join(fields_of_study)
        url = f"{self.base_url}/paper/search"
        return self._get(url, params)

    def bulk_search(
        self,
        *,
        query: Optional[str] = None,
        token: Optional[str] = None,
        fields: Optional[Sequence[str]] = None,
        sort: Optional[str] = None,
        publication_types: Optional[Sequence[str]] = None,
        open_access_pdf: bool = False,
        min_citation_count: Optional[int] = None,
        publication_date_or_year: Optional[str] = None,
        year: Optional[str] = None,
        venue: Optional[Sequence[str]] = None,
        fields_of_study: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if query:
            params["query"] = query
        if token:
            params["token"] = token
        f = self._fields(fields, self.DEFAULT_AUTHOR_FIELDS)
        if f:
            params["fields"] = f
        if sort:
            params["sort"] = sort
        if publication_types:
            params["publicationTypes"] = ",".join(publication_types)
        if open_access_pdf:
            params["openAccessPdf"] = "true"
        if min_citation_count is not None:
            params["minCitationCount"] = int(min_citation_count)
        if publication_date_or_year:
            params["publicationDateOrYear"] = publication_date_or_year
        elif year:
            params["year"] = year
        if venue:
            params["venue"] = ",".join(venue)
        if fields_of_study:
            params["fieldsOfStudy"] = ",".join(fields_of_study)
        url = f"{self.base_url}/paper/search/bulk"
        return self._get(url, params)

    def title_search(
        self,
        query: str,
        *,
        fields: Optional[Sequence[str]] = None,
        publication_types: Optional[Sequence[str]] = None,
        open_access_pdf: bool = False,
        min_citation_count: Optional[int] = None,
        year: Optional[str] = None,
        venue: Optional[Sequence[str]] = None,
        fields_of_study: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        if not query or not query.strip():
            raise SemanticScholarError("query cannot be empty")
        params: Dict[str, Any] = {"query": query}
        f = self._fields(fields, self.DEFAULT_PAPER_FIELDS)
        if f:
            params["fields"] = f
        if publication_types:
            params["publicationTypes"] = ",".join(publication_types)
        if open_access_pdf:
            params["openAccessPdf"] = "true"
        if min_citation_count is not None:
            params["minCitationCount"] = int(min_citation_count)
        if year:
            params["year"] = year
        if venue:
            params["venue"] = ",".join(venue)
        if fields_of_study:
            params["fieldsOfStudy"] = ",".join(fields_of_study)
        url = f"{self.base_url}/paper/search/match"
        return self._get(url, params)

    def get_paper(self, paper_id: str, *, fields: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        if not paper_id or not paper_id.strip():
            raise SemanticScholarError("paper_id cannot be empty")
        params: Dict[str, Any] = {}
        f = self._fields(fields, self.DEFAULT_PAPER_FIELDS)
        if f:
            params["fields"] = f
        url = f"{self.base_url}/paper/{paper_id}"
        return self._get(url, params)

    def get_papers_batch(self, paper_ids: Sequence[str], *, fields: Optional[Sequence[str]] = None) -> Any:
        if not paper_ids:
            raise SemanticScholarError("paper_ids cannot be empty")
        if len(paper_ids) > 500:
            raise SemanticScholarError("cannot request more than 500 paper_ids in a batch")
        params: Dict[str, Any] = {}
        f = self._fields(fields, self.DEFAULT_PAPER_FIELDS)
        if f:
            params["fields"] = f
        url = f"{self.base_url}/paper/batch"
        return self._post(url, json={"ids": list(paper_ids)}, params=params)

    def paper_authors(
        self,
        paper_id: str,
        *,
        fields: Optional[Sequence[str]] = None,
        offset: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        if not paper_id or not paper_id.strip():
            raise SemanticScholarError("paper_id cannot be empty")
        params: Dict[str, Any] = {"offset": offset, "limit": min(max(limit, 1), 1000)}
        f = self._fields(fields, self.DEFAULT_PAPER_FIELDS)
        if f:
            params["fields"] = f
        url = f"{self.base_url}/paper/{paper_id}/authors"
        return self._get(url, params)

    def paper_citations(
        self,
        paper_id: str,
        *,
        fields: Optional[Sequence[str]] = None,
        offset: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        if not paper_id or not paper_id.strip():
            raise SemanticScholarError("paper_id cannot be empty")
        params: Dict[str, Any] = {"offset": offset, "limit": min(max(limit, 1), 1000)}
        f = self._fields(fields, self.DEFAULT_PAPER_FIELDS)
        if f:
            params["fields"] = f
        url = f"{self.base_url}/paper/{paper_id}/citations"
        return self._get(url, params)

    def paper_references(
        self,
        paper_id: str,
        *,
        fields: Optional[Sequence[str]] = None,
        offset: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        if not paper_id or not paper_id.strip():
            raise SemanticScholarError("paper_id cannot be empty")
        params: Dict[str, Any] = {"offset": offset, "limit": min(max(limit, 1), 1000)}
        f = self._fields(fields, self.DEFAULT_PAPER_FIELDS)
        if f:
            params["fields"] = f
        url = f"{self.base_url}/paper/{paper_id}/references"
        return self._get(url, params)

    # --------------------- Authors ---------------------
    def search_authors(
        self,
        query: str,
        *,
        fields: Optional[Sequence[str]] = None,
        offset: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        if not query or not query.strip():
            raise SemanticScholarError("query cannot be empty")
        params: Dict[str, Any] = {"query": query, "offset": offset, "limit": min(max(limit, 1), 1000)}
        f = self._fields(fields, self.DEFAULT_AUTHOR_FIELDS)
        if f:
            params["fields"] = f
        url = f"{self.base_url}/author/search"
        return self._get(url, params)

    def get_author(self, author_id: str, *, fields: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        if not author_id or not author_id.strip():
            raise SemanticScholarError("author_id cannot be empty")
        params: Dict[str, Any] = {}
        f = self._fields(fields, self.DEFAULT_AUTHOR_FIELDS)
        if f:
            params["fields"] = f
        url = f"{self.base_url}/author/{author_id}"
        return self._get(url, params)

    def author_papers(
        self,
        author_id: str,
        *,
        fields: Optional[Sequence[str]] = None,
        offset: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        if not author_id or not author_id.strip():
            raise SemanticScholarError("author_id cannot be empty")
        params: Dict[str, Any] = {"offset": offset, "limit": min(max(limit, 1), 1000)}
        f = self._fields(fields, self.DEFAULT_PAPER_FIELDS)
        if f:
            params["fields"] = f
        url = f"{self.base_url}/author/{author_id}/papers"
        return self._get(url, params)

    def author_batch_details(self, author_ids: Sequence[str], *, fields: Optional[Sequence[str]] = None) -> Any:
        if not author_ids:
            raise SemanticScholarError("author_ids cannot be empty")
        if len(author_ids) > 1000:
            raise SemanticScholarError("cannot request more than 1000 author_ids in a batch")
        params: Dict[str, Any] = {}
        f = self._fields(fields, self.DEFAULT_AUTHOR_FIELDS)
        if f:
            params["fields"] = f
        url = f"{self.base_url}/author/batch"
        return self._post(url, json={"ids": list(author_ids)}, params=params)

    # --------------------- Recommendations ---------------------
    def recommend_for_paper(
        self,
        paper_id: str,
        *,
        fields: Optional[Union[str, Sequence[str]]] = None,
        limit: int = 100,
        from_pool: str = "recent",
    ) -> Dict[str, Any]:
        if not paper_id or not paper_id.strip():
            raise SemanticScholarError("paper_id cannot be empty")
        if limit > 500:
            raise SemanticScholarError("limit cannot exceed 500 for recommendations")
        params: Dict[str, Any] = {"limit": limit, "from": from_pool}
        if fields is None:
            params["fields"] = ",".join(self.DEFAULT_PAPER_FIELDS)
        else:
            params["fields"] = ",".join(fields) if isinstance(fields, (list, tuple)) else str(fields)
        url = f"{self.recs_base_url}/papers/forpaper/{paper_id}"
        return self._get(url, params)

    def recommend_for_papers(
        self,
        positive_paper_ids: Sequence[str],
        *,
        negative_paper_ids: Optional[Sequence[str]] = None,
        fields: Optional[Union[str, Sequence[str]]] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        if not positive_paper_ids:
            raise SemanticScholarError("must provide at least one positive paper id")
        if limit > 500:
            raise SemanticScholarError("limit cannot exceed 500 for recommendations")
        params: Dict[str, Any] = {"limit": limit}
        if fields is None:
            params["fields"] = ",".join(self.DEFAULT_PAPER_FIELDS)
        else:
            params["fields"] = ",".join(fields) if isinstance(fields, (list, tuple)) else str(fields)
        body = {
            "positivePaperIds": list(positive_paper_ids),
            "negativePaperIds": list(negative_paper_ids or []),
        }
        url = f"{self.recs_base_url}/papers"
        return self._post(url, json=body, params=params)


__all__ = [
    "SemanticScholarClient",
    "SemanticScholarError",
]

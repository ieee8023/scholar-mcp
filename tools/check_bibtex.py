#!/usr/bin/env python3
"""Verify and enrich BibTeX entries with Semantic Scholar metadata."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from .semanticscholar import SemanticScholarClient, SemanticScholarError
except ImportError:  # pragma: no cover - fallback for script usage
    from semanticscholar import SemanticScholarClient, SemanticScholarError


VERIFY_FIELDS: Sequence[str] = (
    "paperId",
    "title",
    "year",
    "authors",
    "externalIds",
    "url",
)
INVALID_STATUSES = {"mismatch", "not_found", "error"}


@dataclass
class BibEntry:
    entry_type: str
    key: str
    fields: Dict[str, str]


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_title(value: str) -> str:
    cleaned = normalize_whitespace(value)
    cleaned = re.sub(r"[{}]", "", cleaned)
    cleaned = re.sub(r"[^A-Za-z0-9 ]+", " ", cleaned)
    return normalize_whitespace(cleaned).lower()


def normalize_doi(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    doi = value.strip().strip("{}\"")
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    doi = doi.rstrip("/ ")
    return doi or None


def title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_title(left), normalize_title(right)).ratio()


def split_top_level(source: str, delimiter: str = ',') -> List[str]:
    parts: List[str] = []
    current: List[str] = []
    brace_depth = 0
    quote_open = False
    escape = False
    for char in source:
        if escape:
            current.append(char)
            escape = False
            continue
        if char == "\\":
            current.append(char)
            escape = True
            continue
        if char == '"' and brace_depth == 0:
            quote_open = not quote_open
            current.append(char)
            continue
        if not quote_open:
            if char == '{':
                brace_depth += 1
            elif char == '}':
                brace_depth = max(0, brace_depth - 1)
            elif char == delimiter and brace_depth == 0:
                piece = "".join(current).strip()
                if piece:
                    parts.append(piece)
                current = []
                continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def parse_field_value(raw_value: str) -> str:
    value = raw_value.strip().rstrip(',').strip()
    if not value:
        return ""
    if value.startswith('{') and value.endswith('}'):
        return value[1:-1].strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].strip()
    return value


def parse_bibtex(text: str) -> List[BibEntry]:
    entries: List[BibEntry] = []
    position = 0
    while True:
        start = text.find('@', position)
        if start == -1:
            break
        open_brace = text.find('{', start)
        if open_brace == -1:
            break
        entry_type = text[start + 1:open_brace].strip()
        depth = 1
        index = open_brace + 1
        while index < len(text) and depth > 0:
            if text[index] == '{':
                depth += 1
            elif text[index] == '}':
                depth -= 1
            index += 1
        block = text[open_brace + 1:index - 1]
        position = index
        if entry_type.lower() in {"comment", "preamble", "string"}:
            continue
        parts = split_top_level(block)
        if not parts:
            continue
        key = parts[0].strip()
        fields: Dict[str, str] = {}
        for field in parts[1:]:
            if '=' not in field:
                continue
            name, raw_value = field.split('=', 1)
            fields[name.strip().lower()] = parse_field_value(raw_value)
        entries.append(BibEntry(entry_type=entry_type.strip(), key=key, fields=fields))
    return entries


def render_bibtex(entries: Iterable[BibEntry]) -> str:
    rendered: List[str] = []
    for entry in entries:
        rendered.append(f"@{entry.entry_type}{{{entry.key},")
        for field_name in sorted(entry.fields):
            value = entry.fields[field_name].replace("\n", " ").strip()
            rendered.append(f"  {field_name} = {{{value}}},")
        rendered.append("}")
        rendered.append("")
    return "\n".join(rendered).rstrip() + "\n"


def extract_doi_from_paper(paper: Dict[str, Any]) -> Optional[str]:
    external_ids = paper.get("externalIds") or {}
    if isinstance(external_ids, dict):
        return normalize_doi(external_ids.get("DOI") or external_ids.get("doi"))
    return None


def result_for(entry: BibEntry, source: str, status: str, **extra: Any) -> Dict[str, Any]:
    return {
        "key": entry.key,
        "source": source,
        "status": status,
        "title_similarity": 0.0,
        "year_matches": False,
        "updates": {},
        **extra,
    }


def fetch_paper_match(client: SemanticScholarClient, entry: BibEntry) -> Tuple[Optional[Dict[str, Any]], str]:
    doi = normalize_doi(entry.fields.get("doi"))
    if doi:
        return client.get_paper(f"DOI:{doi}", fields=list(VERIFY_FIELDS)), "doi"

    url = (entry.fields.get("url") or "").strip()
    if url:
        try:
            return client.get_paper(f"URL:{url}", fields=list(VERIFY_FIELDS)), "url"
        except SemanticScholarError:
            pass

    title = (entry.fields.get("title") or "").strip()
    if not title:
        return None, "missing-title"

    result = client.title_search(title, fields=list(VERIFY_FIELDS))
    if not result or not result.get("paperId"):
        return None, "title-not-found"
    return result, "title"


def build_updates(entry: BibEntry, paper: Dict[str, Any]) -> Dict[str, str]:
    updates: Dict[str, str] = {}
    paper_id = paper.get("paperId")
    if paper_id:
        updates["semanticscholarid"] = str(paper_id)
    doi = extract_doi_from_paper(paper)
    if doi:
        updates["doi"] = doi
    url = paper.get("url")
    if url:
        updates["url"] = str(url)
    return updates


def compare_entry(entry: BibEntry, paper: Optional[Dict[str, Any]], source: str, min_title_similarity: float) -> Dict[str, Any]:
    if paper is None:
        return result_for(entry, source, "not_found")

    entry_title = entry.fields.get("title") or ""
    paper_title = str(paper.get("title") or "")
    similarity = title_similarity(entry_title, paper_title) if entry_title and paper_title else 0.0

    entry_year = normalize_whitespace(entry.fields.get("year") or "")
    paper_year = normalize_whitespace(str(paper.get("year") or ""))
    year_matches = not entry_year or not paper_year or entry_year == paper_year

    doi_matches = True
    entry_doi = normalize_doi(entry.fields.get("doi"))
    paper_doi = extract_doi_from_paper(paper)
    if entry_doi and paper_doi:
        doi_matches = entry_doi.lower() == paper_doi.lower()

    is_match = (source == "doi" and doi_matches and year_matches) or (
        source != "doi" and similarity >= min_title_similarity and year_matches and doi_matches
    )
    return result_for(
        entry,
        source,
        "matched" if is_match else "mismatch",
        title_similarity=round(similarity, 3),
        year_matches=year_matches,
        updates=build_updates(entry, paper),
        paper_title=paper_title,
    )


def apply_updates(entry: BibEntry, updates: Dict[str, str]) -> None:
    for field_name, value in updates.items():
        if value:
            entry.fields[field_name] = value


def check_bibtex_file(
    bibtex_path: str,
    *,
    output_path: Optional[str] = None,
    write: bool = False,
    api_key: Optional[str] = None,
    min_title_similarity: float = 0.9,
) -> List[Dict[str, Any]]:
    path = Path(bibtex_path)
    text = path.read_text(encoding="utf-8")
    entries = parse_bibtex(text)
    client = SemanticScholarClient(api_key=api_key)
    results: List[Dict[str, Any]] = []
    for entry in entries:
        try:
            paper, source = fetch_paper_match(client, entry)
            result = compare_entry(entry, paper, source, min_title_similarity)
            if result["updates"]:
                apply_updates(entry, result["updates"])
        except SemanticScholarError as exc:
            result = result_for(entry, "error", "error", error=str(exc))
        results.append(result)

    if write or output_path:
        destination = Path(output_path) if output_path else path
        destination.write_text(render_bibtex(entries), encoding="utf-8")
    return results


def summarize_results(results: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    summary = {"matched": 0, "mismatch": 0, "not_found": 0, "error": 0}
    for result in results:
        status = result.get("status", "error")
        summary[status] = summary.get(status, 0) + 1
    return summary


def invalid_results(results: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [result for result in results if result.get("status") in INVALID_STATUSES]


def print_results(results: Sequence[Dict[str, Any]]) -> None:
    summary = summarize_results(results)
    print(
        "Summary: "
        f"matched={summary['matched']} "
        f"mismatch={summary['mismatch']} "
        f"not_found={summary['not_found']} "
        f"error={summary['error']}"
    )
    for result in results:
        parts = [f"[{result['status']}] {result['key']} via {result['source']}"]
        if result.get("title_similarity"):
            parts.append(f"title_similarity={result['title_similarity']:.3f}")
        if result.get("updates"):
            parts.append(f"updates={','.join(sorted(result['updates']))}")
        if result.get("error"):
            parts.append(f"error={result['error']}")
        print(" ".join(parts))

    invalid = invalid_results(results)
    if invalid:
        print("Invalid entries: " + ", ".join(result["key"] for result in invalid))
    else:
        print("Invalid entries: none")


def main() -> None:
    parser = argparse.ArgumentParser(prog="scholar-mcp-bibtex-check")
    parser.add_argument("bibtex_file", help="Path to the BibTeX file to verify")
    parser.add_argument("--output", help="Write the enriched BibTeX to a different file")
    parser.add_argument("--write", action="store_true", help="Rewrite the input BibTeX file in place")
    parser.add_argument("--semantic-scholar-api-key", help="Semantic Scholar API key to use")
    parser.add_argument(
        "--min-title-similarity",
        type=float,
        default=0.9,
        help="Minimum title similarity for title-based matches",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable results")
    args = parser.parse_args()

    results = check_bibtex_file(
        args.bibtex_file,
        output_path=args.output,
        write=args.write,
        api_key=args.semantic_scholar_api_key,
        min_title_similarity=args.min_title_similarity,
    )

    if args.json:
        print(json.dumps({"summary": summarize_results(results), "invalid": invalid_results(results), "entries": results}, indent=2))
        return

    print_results(results)


if __name__ == "__main__":
    main()
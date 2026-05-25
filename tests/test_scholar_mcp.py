from pathlib import Path
import json

import pytest

from tools import check_bibtex as cb
from tools.semanticscholar import SemanticScholarClient
from tools import download_paper_text as dpt


def _make_sample_pdf(path: Path, text: str) -> None:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


def test_default_fields_include_doi_for_papers(monkeypatch):
    client = SemanticScholarClient(use_cache=False)
    captured = {}

    def fake_get(url, params=None):
        captured["params"] = params or {}
        return {"ok": True}

    monkeypatch.setattr(client, "_get", fake_get)
    client.search_papers("test query")

    fields = (captured["params"].get("fields") or "").split(",")
    assert "paperId" in fields


def test_default_fields_include_author_fields(monkeypatch):
    client = SemanticScholarClient(use_cache=False)
    captured = {}

    def fake_get(url, params=None):
        captured["params"] = params or {}
        return {"ok": True}

    monkeypatch.setattr(client, "_get", fake_get)
    client.get_author("123")

    fields = (captured["params"].get("fields") or "").split(",")
    assert "authorId" in fields
    assert "name" in fields


def test_default_fields_used_for_recommendations(monkeypatch):
    client = SemanticScholarClient(use_cache=False)
    captured = {}

    def fake_get(url, params=None):
        captured["params"] = params or {}
        return {"ok": True}

    monkeypatch.setattr(client, "_get", fake_get)
    client.recommend_for_paper("abc")

    fields = (captured["params"].get("fields") or "").split(",")
    assert "title" in fields


def test_extract_text_from_existing_pdf(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_sample_pdf(pdf_path, "Hello PDF extraction works.")

    result = dpt.extract_text_from_existing_pdf(str(pdf_path), str(tmp_path))
    text_path = Path(result["text_path"])

    assert text_path.exists()
    content = text_path.read_text(encoding="utf-8")
    assert "Hello PDF extraction works" in content


def test_download_paper_and_extract_with_mocked_download(tmp_path, monkeypatch):
    source_pdf = tmp_path / "source.pdf"
    _make_sample_pdf(source_pdf, "Downloaded PDF extraction works.")

    def fake_get_pdf_from_semantic_scholar(_id_or_doi):
        return None, "https://example.com/fake.pdf", "paper-id-1", None, None

    def fake_download_stream(_url, dest_path, headers=None, timeout=60, session=None):
        dest = Path(dest_path)
        dest.write_bytes(source_pdf.read_bytes())
        return None

    def fake_get_paper(_paper_id, fields=None):
        return {"title": "Sample"}

    monkeypatch.setattr(dpt, "get_pdf_from_semantic_scholar", fake_get_pdf_from_semantic_scholar)
    monkeypatch.setattr(dpt, "download_stream", fake_download_stream)
    monkeypatch.setattr(dpt, "search_local_pdf", lambda *args, **kwargs: None)
    monkeypatch.setattr(dpt.SemanticScholarClient, "get_paper", fake_get_paper)

    result = dpt.download_paper_and_extract("dummy", str(tmp_path))
    pdf_path = Path(result["pdf_path"])
    text_path = Path(result["text_path"])

    assert pdf_path.exists()
    assert text_path.exists()
    content = text_path.read_text(encoding="utf-8")
    assert "Downloaded PDF extraction works" in content


def test_check_bibtex_file_enriches_entry_from_title_match(tmp_path, monkeypatch):
    bib_path = tmp_path / "sample.bib"
    bib_path.write_text(
        """@article{lime,
  title = {Why Should I Trust You? Explaining the Predictions of Any Classifier},
  year = {2016},
  author = {Ribeiro, Marco Tulio},
}
""",
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, api_key=None):
            self.api_key = api_key

        def get_paper(self, paper_id, fields=None):
            raise AssertionError("DOI lookup should not run for entries without a DOI")

        def title_search(self, title, fields=None):
            assert "Why Should I Trust You" in title
            return {
                "paperId": "s2-paper-123",
                "title": "Why Should I Trust You? Explaining the Predictions of Any Classifier",
                "year": 2016,
                "url": "https://www.semanticscholar.org/paper/s2-paper-123",
                "externalIds": {"DOI": "10.1145/2939672.2939778"},
                "authors": [{"name": "Marco Tulio Ribeiro"}],
            }

    monkeypatch.setattr(cb, "SemanticScholarClient", FakeClient)

    results = cb.check_bibtex_file(str(bib_path), write=True)

    assert results[0]["status"] == "matched"
    rewritten = bib_path.read_text(encoding="utf-8")
    assert "semanticscholarid = {s2-paper-123}" in rewritten
    assert "doi = {10.1145/2939672.2939778}" in rewritten
    assert "url = {https://www.semanticscholar.org/paper/s2-paper-123}" in rewritten


def test_check_bibtex_file_uses_doi_lookup_when_available(tmp_path, monkeypatch):
    bib_path = tmp_path / "sample.bib"
    bib_path.write_text(
        """@article{attention,
  title = {Attention Is All You Need},
  year = {2017},
  doi = {https://doi.org/10.5555/3295222.3295349},
}
""",
        encoding="utf-8",
    )

    calls = {"get_paper": 0, "title_search": 0}

    class FakeClient:
        def __init__(self, api_key=None):
            self.api_key = api_key

        def get_paper(self, paper_id, fields=None):
            calls["get_paper"] += 1
            assert paper_id == "DOI:10.5555/3295222.3295349"
            return {
                "paperId": "s2-attention",
                "title": "Attention Is All You Need",
                "year": 2017,
                "url": "https://www.semanticscholar.org/paper/s2-attention",
                "externalIds": {"DOI": "10.5555/3295222.3295349"},
            }

        def title_search(self, title, fields=None):
            calls["title_search"] += 1
            raise AssertionError("Title lookup should not run when DOI is present")

    monkeypatch.setattr(cb, "SemanticScholarClient", FakeClient)

    results = cb.check_bibtex_file(str(bib_path))

    assert results[0]["source"] == "doi"
    assert results[0]["status"] == "matched"
    assert calls == {"get_paper": 1, "title_search": 0}


def test_invalid_results_collects_non_matching_entries():
    results = [
        {"key": "ok", "status": "matched"},
        {"key": "bad1", "status": "mismatch"},
        {"key": "bad2", "status": "not_found"},
        {"key": "bad3", "status": "error"},
    ]

    assert [result["key"] for result in cb.invalid_results(results)] == ["bad1", "bad2", "bad3"]


def test_print_results_shows_invalid_summary(capsys):
    cb.print_results(
        [
            {"key": "ok", "source": "doi", "status": "matched", "title_similarity": 0.0, "updates": {}, "year_matches": True},
            {"key": "bad", "source": "title", "status": "mismatch", "title_similarity": 0.82, "updates": {}, "year_matches": False},
        ]
    )

    output = capsys.readouterr().out
    assert "Summary: matched=1 mismatch=1 not_found=0 error=0" in output
    assert "Invalid entries: bad" in output

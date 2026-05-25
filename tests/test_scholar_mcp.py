from pathlib import Path

import pytest

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

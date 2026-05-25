#!/usr/bin/env python3
"""Download a paper's PDF (DOI or Semantic Scholar paperId) and extract text.

Usage:
    PYTHONPATH=. python tools/download_paper_text.py [--output-dir DIR] [--save-html] <paperId|DOI>
    PYTHONPATH=. python tools/download_paper_text.py --pdf /path/to/file.pdf [--output-dir DIR]

Download strategies (in order):
 1. Semantic Scholar openAccessPdf URL
 2. Semantic Scholar landing page
 3a. Direct arXiv PDF (if arXiv ID exists in Semantic Scholar metadata)
 3b. Direct arXiv PDF (if DOI is an arXiv DOI like 10.48550/arXiv.XXXX.XXXXX)
 4. Unpaywall API (if DOI available)
 5. PMC via PMCID (if DOI maps to PMCID)
 6. arXiv search by title (if no arXiv ID found yet but title available)

Extraction:
 - PyMuPDF (fitz) if available, otherwise pdfminer.six
 - Output: <output_dir>/<safe_id>.pdf and <output_dir>/<safe_id>.txt
"""
import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urljoin, quote, urlparse
from difflib import SequenceMatcher
from threading import Thread, Lock
import queue

import requests
from bs4 import BeautifulSoup
try:
    from pdfminer.high_level import extract_text as pdfminer_extract # type: ignore
except ImportError:
    # If pdfminer import fails, define a dummy function
    def pdfminer_extract(*args, **kwargs):
        raise ImportError("pdfminer.six not available")
from tqdm import tqdm

try:
    from .semanticscholar import SemanticScholarClient
except ImportError:  # pragma: no cover - fallback for script usage
    from semanticscholar import SemanticScholarClient

CONFIG_PATH = Path(__file__).parent / 'download_config.json'
PDF_FOLDERS = []
PDF_CACHE_FILE = str(Path(__file__).parent / '.pdf_cache.json')
TITLE_MATCH_THRESHOLD = 0.85
PDF_METADATA_TIMEOUT = 10  # seconds
HEADERS_CONFIG_PATH = Path(__file__).parent / 'http_headers.json'


def load_download_config():
    """Load download configuration from download_config.json with defaults."""
    defaults = {
        'download_domain': '',
        'unpaywall_email': 'test@test.com',
    }
    # 1) Load values from package config file (lowest precedence)
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, 'r') as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                defaults.update({k: v for k, v in cfg.items() if v is not None})
    except Exception:
        pass
    return defaults


_download_cfg = load_download_config()
DOWNLOAD_DOMAIN = _download_cfg.get('download_domain')
UNPAYWALL_EMAIL = _download_cfg.get('unpaywall_email')

# Global lock and flag for thread-safe scanning
_scan_lock = Lock()
_scan_in_progress = False
_scan_completed = False
_cached_results = None

def extract_doi_from_string(s):
    """Extract DOI from a string using regex."""
    if not s:
        return None
    m = re.search(r'(10\.\d{2,9}/[^\s"<>]+)', s)
    if m:
        return m.group(1).rstrip('.,;')
    return None


def extract_arxiv_id_from_doi(doi):
    """Extract arXiv ID from DOI if it's an arXiv DOI (format: 10.48550/arXiv.XXXX.XXXXX)."""
    if not doi:
        return None
    # Match arXiv DOI format: 10.48550/arXiv.XXXX.XXXXX
    m = re.search(r'10\.48550/arXiv\.(\d{4}\.\d{4,5})', doi, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def normalize_title(title):
    """Normalize title for comparison by removing special chars and lowercasing."""
    if not title:
        return ""
    # Filter to keep only alphanumeric and spaces
    return filter_alphanumeric(title.lower())


def filter_alphanumeric(text, keep_spaces=True):
    """Filter to keep only alphanumeric characters and optionally spaces."""
    if not text:
        return text
    if keep_spaces:
        # Keep alphanumeric and spaces, then collapse multiple spaces
        filtered = ''.join(c if (c.isalnum() or c.isspace()) else ' ' for c in text)
        # Collapse multiple whitespace to single space
        filtered = re.sub(r'\s+', ' ', filtered).strip()
        return filtered
    return ''.join(c for c in text if c.isalnum())


def title_similarity(title1, title2):
    """Calculate similarity ratio between two titles."""
    if not title1 or not title2:
        return 0.0
    norm1 = normalize_title(title1)
    norm2 = normalize_title(title2)
    return SequenceMatcher(None, norm1, norm2).ratio()


def extract_pdf_metadata(pdf_path):
    """Extract DOI, title, and text preview from PDF metadata and first few pages.
    Returns (doi, title, text) tuple. Includes timeout to prevent hanging."""
    
    def _extract_metadata_worker(pdf_path, result_queue):
        """Worker function that does the actual extraction."""
        doi = None
        title = None
        text = None
        
        try:
            # Try PyMuPDF first (better metadata extraction)
            try:
                import fitz
                doc = fitz.open(str(pdf_path)) # type: ignore
                
                # Check PDF metadata
                metadata = doc.metadata
                if metadata:
                    # Check for DOI in metadata
                    for key in ['doi', 'DOI', 'subject', 'keywords']:
                        if key in metadata and metadata[key]:
                            extracted_doi = extract_doi_from_string(metadata[key])
                            if extracted_doi:
                                doi = extracted_doi
                                break
                    
                    # Get title from metadata
                    if 'title' in metadata and metadata['title']:
                        title = metadata['title'].strip()
                        title = filter_alphanumeric(title)
                
                # Extract text from first page
                if len(doc) > 0:
                    first_page_text = doc[0].get_text()
                    
                    # If no DOI found in metadata, check first 3 pages of content
                    if not doi:
                        pages_to_check = min(3, len(doc))
                        text_sample = ""
                        for i in range(pages_to_check):
                            text_sample += doc[i].get_text() # type: ignore
                        
                        # Look for DOI in text
                        doi = extract_doi_from_string(text_sample)
                    
                    # If no title from metadata, try to extract from first page
                    if not title:
                        lines = [l.strip() for l in first_page_text.split('\n') if l.strip()] # type: ignore
                        # Title is often one of the first few non-empty lines
                        for line in lines[:10]:
                            if len(line) > 20 and len(line) < 200:  # Reasonable title length
                                title = filter_alphanumeric(line)
                                break
                    
                    # Store text preview (first 500 chars, filtered)
                    text = filter_alphanumeric(first_page_text)
                    text = text[:500] if text else None
                
                doc.close()
                
            except ImportError:
                # Fallback to pdfminer
                text_sample = pdfminer_extract(str(pdf_path), maxpages=3)
                doi = extract_doi_from_string(text_sample)
                
                # Try to extract title from first lines
                lines = [l.strip() for l in text_sample.split('\n') if l.strip()] # type: ignore
                for line in lines[:10]:
                    if len(line) > 20 and len(line) < 200:
                        title = filter_alphanumeric(line)
                        break
                
                # Store text preview
                text = filter_alphanumeric(text_sample)
                text = text[:500] if text else None
        
        except Exception as e:
            # Silently fail - PDF might be corrupted or unreadable
            pass
        
        result_queue.put((doi, title, text))
    
    # Create a queue to get the result
    result_queue = queue.Queue()
    
    # Create and start the worker thread
    worker = Thread(target=_extract_metadata_worker, args=(pdf_path, result_queue))
    worker.daemon = True
    worker.start()
    
    # Wait for the result with timeout
    worker.join(timeout=PDF_METADATA_TIMEOUT)
    
    if worker.is_alive():
        # Timeout occurred - return None values
        return None, None, None
    
    # Get the result from the queue
    try:
        return result_queue.get_nowait()
    except queue.Empty:
        return None, None, None


def validate_pdf_matches_expected(pdf_path, expected_doi=None, expected_title=None):
    """Validate downloaded PDF by checking DOI or title similarity.
    Returns True if validation passes, False otherwise."""
    doi, title, _ = extract_pdf_metadata(pdf_path)

    # If we have an expected DOI, enforce exact match when available
    if expected_doi:
        if doi and doi.lower().strip() == expected_doi.lower().strip():
            return True
        # If DOI missing in PDF, allow fallback to title matching

    # Title similarity fallback
    if expected_title and title:
        sim = title_similarity(expected_title, title)
        return sim >= TITLE_MATCH_THRESHOLD

    # If nothing to compare, be conservative
    return False


def load_pdf_cache(cache_path=None):
    """Load PDF cache from disk."""
    cache_path = Path(cache_path or PDF_CACHE_FILE)
    if cache_path.exists():
        try:
            with open(cache_path, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_pdf_cache(cache, cache_path=None):
    """Save PDF cache to disk."""
    cache_path = Path(cache_path or PDF_CACHE_FILE)
    cache_path.parent.mkdir(exist_ok=True)
    try:
        with open(cache_path, 'w') as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f'Warning: Could not save PDF cache: {e}')


def load_domain_headers(url):
    """Load optional headers/cookies for a given URL host from http_headers.json."""
    try:
        if not HEADERS_CONFIG_PATH.exists():
            return None
        with open(HEADERS_CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
        host = urlparse(url).netloc.split(':')[0]
        # Exact match first
        domain_cfg = cfg.get(host)
        # Fallback: longest-suffix match (e.g., apply pmc.ncbi.nlm.nih.gov to www.ncbi.nlm.nih.gov)
        if not domain_cfg:
            best_key = None
            for key in cfg.keys():
                if host.endswith(key):
                    if best_key is None or len(key) > len(best_key):
                        best_key = key
            if best_key:
                domain_cfg = cfg.get(best_key)
        if not domain_cfg:
            return None
        headers = domain_cfg.get('headers') or {}
        cookie = domain_cfg.get('cookie')
        if cookie:
            headers = dict(headers)
            headers['Cookie'] = cookie
        return headers if headers else None
    except Exception:
        return None


def scan_pdf_folders(pdf_folders, cache=None, cache_path=None):
    """Recursively scan PDF folders and build/update cache.
    Thread-safe - only scans once even if called from multiple threads.
    Returns dict mapping pdf_path -> {doi, title, mtime}"""
    global _scan_in_progress, _scan_completed, _cached_results
    
    # Quick check if already completed (no lock needed for read)
    if _scan_completed and _cached_results is not None:
        return _cached_results
    
    # Acquire lock to check/set state
    should_scan = False
    with _scan_lock:
        # Double-check if completed while waiting for lock
        if _scan_completed and _cached_results is not None:
            return _cached_results
        
        # Check if another thread is scanning
        if _scan_in_progress:
            # Another thread is scanning, we'll wait
            should_scan = False
        else:
            # We're the first thread, mark as in progress
            _scan_in_progress = True
            should_scan = True
    
    # If we're not the scanning thread, wait for completion
    if not should_scan:
        while True:
            with _scan_lock:
                if _scan_completed and _cached_results is not None:
                    return _cached_results
            import time
            time.sleep(0.1)
    
    # We're the scanning thread - do the work
    try:
        if cache is None:
            cache = load_pdf_cache(cache_path=cache_path)
        
        # Count total PDFs first for progress bar
        all_pdfs = []
        for folder in pdf_folders:
            folder_path = Path(folder)
            if not folder_path.exists():
                continue
            all_pdfs.extend(list(folder_path.rglob('*.pdf')))
        
        print(f'Scanning {len(all_pdfs)} PDFs in local folders...')
        
        # Scan with progress bar
        for pdf_path in tqdm(all_pdfs, desc='Building PDF cache', unit='pdf'):
            pdf_str = str(pdf_path)
            try:
                mtime = pdf_path.stat().st_mtime
                
                # Check if PDF is in cache and unchanged
                if pdf_str in cache and cache[pdf_str].get('mtime') == mtime:
                    continue
                
                # Extract metadata from PDF
                doi, title, text = extract_pdf_metadata(pdf_path)
                
                cache[pdf_str] = {
                    'doi': doi,
                    'title': title,
                    'text': text,
                    'mtime': mtime
                }
            except Exception as e:
                # Skip problematic PDFs
                continue
        
            # Save cache once at the end
            save_pdf_cache(cache, cache_path=cache_path)
        
        with _scan_lock:
            _cached_results = cache
            _scan_completed = True
            _scan_in_progress = False
        
        return cache
        
    except Exception as e:
        with _scan_lock:
            _scan_in_progress = False
        raise


def search_local_pdf(target_doi, target_title, pdf_folders, cache_path=None):
    """Search local PDF folders for a matching paper.
    Returns path to matching PDF if found, None otherwise."""
    
    # Load and update cache
    cache = scan_pdf_folders(pdf_folders, cache_path=cache_path)
    
    if not cache:
        return None
    
    # First pass: exact DOI match (highest confidence)
    if target_doi:
        for pdf_path, metadata in cache.items():
            if metadata.get('doi') and metadata['doi'].lower() == target_doi.lower():
                return pdf_path
    
    # Second pass: title similarity match (medium confidence)
    if target_title:
        best_match = None
        best_score = 0.0
        
        for pdf_path, metadata in cache.items():
            if metadata.get('title'):
                similarity = title_similarity(target_title, metadata['title'])
                if similarity > TITLE_MATCH_THRESHOLD and similarity > best_score:
                    best_score = similarity
                    best_match = pdf_path
        
        if best_match:
            return best_match
    
    return None


def download_stream(url, dest_path, headers=None, timeout=60, session=None):
    """Download URL to file. Returns HTML text if response is HTML, None otherwise.
    Returns 'SKIP' if response is 202 (processing) and should skip to next strategy."""
    # Merge default and domain-specific headers
    merged = {'User-Agent': 'Mozilla/5.0'}
    domain_headers = load_domain_headers(url)
    if headers:
        merged.update(headers)
    if domain_headers:
        merged.update(domain_headers)
    client = session or requests
    with client.get(url, stream=True, timeout=timeout, headers=merged, allow_redirects=True) as r:
        # Handle 202 (Accepted) as "not ready yet, try next strategy"
        if r.status_code == 202:
            return 'SKIP'
        r.raise_for_status()
        ct = r.headers.get('content-type', '')
        if 'text/html' in ct:
            return r.text
        with open(dest_path, 'wb') as fh:
            for chunk in r.iter_content(8192):
                if chunk:
                    fh.write(chunk)
    return None


def clean_extracted_text(text):
    """Clean up extracted text by removing short lines and strange characters."""
    lines = text.split('\n')
    cleaned_lines = []
    
    for line in lines:
        # Strip whitespace
        line = line.strip()
        
        # Skip very short lines (less than 3 characters) unless they're common punctuation
        if len(line) < 3 and line not in ['', '.', '!', '?', '-', '—']:
            continue
        
        # Skip lines that are mostly non-alphanumeric (except spaces and common punctuation)
        if line:
            alphanumeric_count = sum(c.isalnum() or c.isspace() for c in line)
            if alphanumeric_count / len(line) < 0.5:
                continue
        
        # Replace multiple spaces with single space
        line = re.sub(r'\s+', ' ', line)
        
        # Remove common PDF artifacts
        line = re.sub(r'^\d+$', '', line)  # Lines with only numbers (page numbers)
        
        if line:
            cleaned_lines.append(line)
    
    return '\n'.join(cleaned_lines)


def extract_text_from_pdf(pdf_path, txt_path):
    """Extract text from PDF using PyMuPDF if available, otherwise pdfminer.six."""
    # Try PyMuPDF first
    try:
        import fitz
        doc = fitz.open(str(pdf_path)) # type: ignore
        texts = [p.get_text() for p in doc]
        raw_text = '\n\n'.join(texts) # type: ignore
    except ImportError:
        # Fallback to pdfminer.six
        raw_text = pdfminer_extract(str(pdf_path))
    
    # Clean up the extracted text
    cleaned_text = clean_extracted_text(raw_text)
    
    # Write cleaned text to file
    with open(txt_path, 'w', encoding='utf-8') as fh:
        fh.write(cleaned_text)

def extract_text_from_existing_pdf(pdf_path, output_dir=None):
    """Extract text from an existing PDF without downloading.

    Returns a dict with paths to the source PDF and extracted text.
    """
    if not pdf_path or not str(pdf_path).strip():
        raise ValueError("pdf_path is required")

    pdf_path = Path(pdf_path).expanduser().resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    out_dir = Path(output_dir).expanduser().resolve() if output_dir else pdf_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', pdf_path.stem)
    txt_path = out_dir / f'{safe_name}.txt'

    print(f'Using existing PDF: {pdf_path}')
    print('Extracting text from PDF...')
    extract_text_from_pdf(pdf_path, txt_path)
    print(f'✓ Extracted text saved to {txt_path}')
    print('Done!')

    return {"pdf_path": str(pdf_path), "text_path": str(txt_path)}


def find_pdf_link(html, base_url=None):
    """Parse HTML to find a PDF link using multiple heuristics."""
    soup = BeautifulSoup(html, 'html.parser')

    # Strategy 1: Meta tags (citation_pdf_url, pdf_url)
    for m in soup.find_all('meta'):
        for key in ('citation_pdf_url', 'pdf_url'):
            if (m.get('name') or '').lower() == key or (m.get('property') or '').lower() == key:
                url = m.get('content') or m.get('value')
                if url and '{pdf}' not in url:  # Skip placeholder URLs
                    return urljoin(base_url, url) if base_url else url

    # Strategy 2: Link tags with PDF type
    for link in soup.find_all('link', href=True):
        t = (link.get('type') or '').lower()
        if 'pdf' in t or 'application/pdf' in t:
            return urljoin(base_url, link['href']) if base_url else link['href']

    # Strategy 3: Embed/iframe/object tags
    for tag in soup.find_all(['embed', 'iframe', 'object']):
        src = tag.get('src') or tag.get('data')
        if not src:
            continue
        if src.startswith('//'):
            return 'https:' + src
        if '.pdf' in src.lower():
            return urljoin(base_url, src) if base_url else src

    # Strategy 4: Anchor tags with PDF in href
    candidates = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        hl = href.lower()
        if hl.endswith('.pdf') or '/content/pdf/' in hl or ('.pdf' in hl and '/article/' in hl):
            candidates.append(href)
    if candidates:
        return urljoin(base_url, candidates[0]) if base_url else candidates[0]

    # Strategy 5: Regex search for PDF URLs
    m = re.search(r'https?://[^\"\'>]+\.pdf[^\"\'>]*', html)
    if m:
        return m.group(0)

    # Strategy 6: Relative PDF links
    m2 = re.search(r'href=[\"\']([^\"\']+\.pdf[^\"\']*)', html)
    if m2:
        return urljoin(base_url, m2.group(1)) if base_url else m2.group(1)

    # Strategy 7: Sci-Hub storage URLs (e.g., /storage/2024/8083/e3fbafa0b747b57e09a30a0f86891ba3/ouyang2020.pdf)
    m3 = re.search(r'/storage/\d{4}/\d{4}/[a-f0-9]+/\w+\.pdf', html)
    if m3:
        return urljoin(base_url, m3.group(0)) if base_url else m3.group(0)

    return None


def get_pmcid_for_doi(doi):
    """Resolve PMCID for a DOI using NCBI idconv. Returns PMCID or None."""
    try:
        conv_url = 'https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/'
        params = {'ids': doi, 'format': 'json'}
        r = requests.get(conv_url, params=params, timeout=30, headers={'User-Agent': 'python-requests'})
        if not r.ok:
            return None
        data = r.json()
        recs = data.get('records') or []
        if not recs:
            return None
        pmcid = recs[0].get('pmcid')
        return pmcid
    except Exception:
        return None


def get_pdf_from_semantic_scholar(id_or_doi):
    """Query Semantic Scholar for paper metadata and return (doi, pdf_url, paper_id, s2_url, arxiv_id)."""
    
    try:
        s2 = SemanticScholarClient()
        paper = s2.get_paper(id_or_doi, fields=['paperId', 'title', 'openAccessPdf', 'url', 'externalIds'])
        
        # Extract DOI and arXiv ID
        doi = None
        arxiv_id = None
        ext = paper.get('externalIds') or {}
        if isinstance(ext, dict):
            doi = ext.get('DOI') or ext.get('doi')
            arxiv_id = ext.get('ArXiv')
        
        # Get PDF URL from openAccessPdf
        pdf_url = None
        if paper.get('openAccessPdf'):
            pdf_url = paper['openAccessPdf'].get('url')
        
        # Get Semantic Scholar URL as fallback
        s2_url = paper.get('url')
        
        # Use Semantic Scholar paper ID for filename
        paper_id = paper.get('paperId', id_or_doi)
        
        return doi, pdf_url, paper_id, s2_url, arxiv_id
    except Exception as e:
        print(f'Error querying Semantic Scholar: {e}')
        return None, None, None, None, None


def get_pdf_from_unpaywall(doi):
    """Query Unpaywall API for open access PDF URL."""
    try:
        url = f'https://api.unpaywall.org/v2/{quote(doi, safe="/")}'
        params = {'email': UNPAYWALL_EMAIL} if UNPAYWALL_EMAIL else {}
        r = requests.get(url, params=params, timeout=30, headers={'User-Agent': 'python-requests'})
        if r.ok:
            data = r.json()
            best = data.get('best_oa_location') or (data.get('oa_locations') or [])[:1]
            if isinstance(best, dict):
                return best.get('url_for_pdf') or best.get('url')
            elif isinstance(best, list) and best:
                return best[0].get('url_for_pdf') or best[0].get('url')
    except Exception as e:
        print(f'Unpaywall query failed: {e}')
    
    return None


def search_arxiv_by_title(title):
    """Search arXiv for a paper by title and return arXiv ID and PDF URL if found."""
    if not title:
        return None, None
    
    try:
        # Clean up title for search
        search_title = title.strip()[:100]  # Limit length
        url = f'https://export.arxiv.org/api/query?search_query=ti:{quote(search_title)}&max_results=3'
        
        r = requests.get(url, timeout=30, headers={'User-Agent': 'python-requests'})
        if not r.ok:
            return None, None
        
        # Parse XML response
        from xml.etree import ElementTree as ET
        root = ET.fromstring(r.text)
        
        # Define namespace
        ns = {'atom': 'http://www.w3.org/2005/Atom'}
        
        # Get first entry
        entries = root.findall('atom:entry', ns)
        if not entries:
            return None, None
        
        entry = entries[0]
        
        # Get arXiv ID from the entry ID (format: http://arxiv.org/abs/2102.09475v2)
        entry_id = entry.find('atom:id', ns)
        if entry_id is not None and entry_id.text:
            arxiv_id = entry_id.text.split('/abs/')[-1].replace('v1', '').replace('v2', '').replace('v3', '')
            pdf_url = f'https://export.arxiv.org/pdf/{arxiv_id}'
            return arxiv_id, pdf_url
        
    except Exception as e:
        print(f'arXiv search failed: {e}')
    
    return None, None


def download_paper_and_extract(id_or_doi, output_dir, save_html=False):
    """Download a paper PDF (by S2 paperId or DOI) and extract text.

    Returns a dict with paths to the downloaded PDF and extracted text.
    """
    if not id_or_doi or not str(id_or_doi).strip():
        raise ValueError("id_or_doi is required")
    if not output_dir or not str(output_dir).strip():
        raise ValueError("output_dir is required")

    id_or_doi = str(id_or_doi).strip()
    save_html = bool(save_html)

    # Create output directory
    out = Path(output_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    cache_path = out / '.pdf_cache.json'

    # Try to extract DOI from input
    doi = extract_doi_from_string(id_or_doi)
    paper_id = None
    s2_pdf_url = None
    s2_url = None
    arxiv_id_from_metadata = None

    # Strategy 1: Query Semantic Scholar (if not a DOI or to get metadata)
    paper_title = None
    if not doi or True:  # Always query S2 for metadata
        s2_doi, s2_pdf_url, s2_paper_id, s2_url, arxiv_id_from_metadata = get_pdf_from_semantic_scholar(id_or_doi)
        if s2_doi:
            doi = s2_doi
        if s2_paper_id:
            paper_id = s2_paper_id
        
        # Also get paper title for arXiv search fallback
        try:
            s2 = SemanticScholarClient()
            paper_data = s2.get_paper(id_or_doi, fields=['title'])
            paper_title = paper_data.get('title')
        except Exception:
            pass

    # Use paper_id for filename (preferred), fallback to sanitized DOI or input
    if paper_id:
        safe_name = paper_id
    elif doi:
        safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', doi)
    else:
        safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', id_or_doi)

    # Define output paths
    pdf_path = out / f'{safe_name}.pdf'
    txt_path = out / f'{safe_name}.txt'
    html_path = out / f'{safe_name}.html'

    # Check if already downloaded; validate correctness
    if pdf_path.exists() and txt_path.exists():
        if validate_pdf_matches_expected(pdf_path, expected_doi=doi, expected_title=paper_title):
            print(f'✓ Paper already exists: {pdf_path}')
            print(f'✓ Text already exists: {txt_path}')
            return {"pdf_path": str(pdf_path), "text_path": str(txt_path)}
        else:
            print('⚠ Existing files do not match expected paper, re-downloading')
            try:
                pdf_path.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                txt_path.unlink(missing_ok=True)
            except Exception:
                pass

    # NEW: Search local PDF folders before attempting download
    print('Searching local PDF folders...')
    local_pdf = search_local_pdf(doi, paper_title, PDF_FOLDERS, cache_path=cache_path)
    
    if local_pdf:
        print(f'✓ Found matching PDF in local library: {local_pdf}')
        try:
            # Copy the PDF to our papers directory
            shutil.copy2(local_pdf, pdf_path)
            print(f'✓ Copied to {pdf_path}')
            
            # Extract text from the copied PDF
            print('Extracting text from PDF...')
            extract_text_from_pdf(pdf_path, txt_path)
            print(f'✓ Extracted text saved to {txt_path}')
            print('Done!')
            return {"pdf_path": str(pdf_path), "text_path": str(txt_path)}
        except Exception as e:
            print(f'✗ Error copying/extracting local PDF: {e}')
            print('→ Will attempt download instead...')
            # Continue to download if local copy fails

    # Collect all potential PDF URLs to try
    pdf_urls_to_try = []
    
    # Strategy 1: Semantic Scholar openAccessPdf
    if s2_pdf_url:
        pdf_urls_to_try.append(('Semantic Scholar OA PDF', s2_pdf_url))
        print(f'✓ Found PDF via Semantic Scholar: {s2_pdf_url}')
    
    # Strategy 2: Semantic Scholar landing page
    if s2_url and s2_url not in [s2_pdf_url]:
        pdf_urls_to_try.append(('Semantic Scholar page', s2_url))
        print(f'→ Will try Semantic Scholar page: {s2_url}')
    
    # Strategy 3a: Direct arXiv PDF (if arXiv ID exists in metadata)
    if arxiv_id_from_metadata:
        arxiv_pdf = f'https://export.arxiv.org/pdf/{arxiv_id_from_metadata}'
        pdf_urls_to_try.append(('arXiv direct (from metadata)', arxiv_pdf))
        print(f'✓ Found arXiv ID in metadata, direct PDF URL: {arxiv_pdf}')
    
    # Strategy 3b: Direct arXiv PDF (if DOI is an arXiv DOI)
    arxiv_id_from_doi = extract_arxiv_id_from_doi(doi) if doi else None
    if arxiv_id_from_doi and not arxiv_id_from_metadata:  # Only if not already added from metadata
        arxiv_pdf = f'https://export.arxiv.org/pdf/{arxiv_id_from_doi}'
        pdf_urls_to_try.append(('arXiv direct (from DOI)', arxiv_pdf))
        print(f'✓ Detected arXiv DOI, direct PDF URL: {arxiv_pdf}')
    
    # Strategy 4: Unpaywall
    if doi:
        unpaywall_url = get_pdf_from_unpaywall(doi)
        if unpaywall_url:
            pdf_urls_to_try.append(('Unpaywall', unpaywall_url))
            print(f'✓ Found PDF via Unpaywall: {unpaywall_url}')
    
    # Strategy 5: PMC via PMCID (if available)
    if doi:
        pmcid = get_pmcid_for_doi(doi)
        if pmcid:
            pmc_pdf = f'https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/'
            pdf_urls_to_try.append(('PMC (via PMCID)', pmc_pdf))
            print(f'✓ Found PMCID {pmcid}, will try PMC PDF: {pmc_pdf}')

    # Strategy 6: (disabled) arXiv search by title to avoid misrouting

    # Strategy 7: DOI landing page (optional, disabled by default)
    if DOWNLOAD_DOMAIN and doi:
        doi_path = quote(doi, safe='/')
        domain = DOWNLOAD_DOMAIN.rstrip('/')
        # Allow config to include scheme; otherwise try https then http
        if domain.startswith('http://') or domain.startswith('https://'):
            doi_landing_urls = [f'{domain}/{doi_path}']
        else:
            doi_landing_urls = [f'https://{domain}/{doi_path}', f'http://{domain}/{doi_path}']

        for doi_landing in doi_landing_urls:
            pdf_urls_to_try.append(('DOI landing page', doi_landing))
            print(f'→ Will try DOI landing page: {doi_landing}')

    if not pdf_urls_to_try:
        raise RuntimeError('No PDF or DOI available')

    # Try each URL strategy until one succeeds
    pdf_downloaded = False
    for strategy_name, pdf_url in pdf_urls_to_try:
        print(f'\nTrying {strategy_name}: {pdf_url}')
        try:
            session = requests.Session()
            html = download_stream(pdf_url, pdf_path, session=session)
            
            # Check if we should skip this strategy (e.g., 202 response)
            if html == 'SKIP':
                print('→ Resource not ready (202), skipping to next strategy')
                continue
            
            # If we got HTML instead of PDF, parse it for PDF links
            if html:
                print('→ Got HTML landing page, parsing for PDF link...')
                pdf_link = find_pdf_link(html, base_url=pdf_url)
                if pdf_link:
                    print(f'✓ Found PDF link: {pdf_link}')
                    # Save HTML for debugging DOI landing pages
                    if 'DOI landing page' in strategy_name and save_html:
                        html_path = Path(output_dir) / f'{safe_name}_doi.html'
                        html_path.write_text(html, encoding='utf-8')
                        print(f'→ Saved DOI landing page to {html_path}')
                    try:
                        html2 = download_stream(
                            pdf_link,
                            pdf_path,
                            headers={'Referer': pdf_url},
                            session=session,
                        )
                        if html2 == 'SKIP':
                            print('→ PDF link returned 202, skipping')
                            continue
                        if html2:
                            # Still HTML, try next strategy
                            print('✗ PDF link still returned HTML')
                            continue
                        # Success - downloaded PDF
                        pdf_downloaded = True
                        break
                    except Exception as e:
                        print(f'✗ Failed to download PDF from link: {e}')
                        continue
                else:
                    print('✗ No PDF link found on landing page')
                    if save_html:
                        html_path.write_text(html, encoding='utf-8')
                        print(f'→ Saved landing page to {html_path}')
                    else:
                        print('→ Not saving landing page (use --save-html to save)')
                    continue
            else:
                # Direct PDF download succeeded
                pdf_downloaded = True
                break
                
        except Exception as e:
            print(f'✗ Failed with {strategy_name}: {e}')
            continue

    # Verify PDF was downloaded
    if not pdf_downloaded or not pdf_path.exists():
        raise RuntimeError('All download strategies failed')

    print(f'✓ Saved PDF to {pdf_path}')

    # Extract text from PDF
    print('Extracting text from PDF...')
    try:
        extract_text_from_pdf(pdf_path, txt_path)
    except Exception as e:
        raise RuntimeError(f'Text extraction failed: {e}')

    print(f'✓ Extracted text saved to {txt_path}')
    print('Done!')

    return {"pdf_path": str(pdf_path), "text_path": str(txt_path)}


def main():
    p = argparse.ArgumentParser(description='Download paper PDF and extract text')
    p.add_argument('id_or_doi', nargs='?', help='Semantic Scholar paperId or DOI (required unless --pdf is set)')
    p.add_argument('--pdf', help='Path to an existing PDF to extract text from')
    p.add_argument('--output-dir', default='papers',
                   help='Output directory for PDF and text (default: papers)')
    p.add_argument('--save-html', action='store_true',
                   help='Save landing HTML when a PDF link is not found (default: false)')
    args = p.parse_args()

    if args.pdf:
        extract_text_from_existing_pdf(args.pdf, args.output_dir)
        return
    if not args.id_or_doi:
        p.error('id_or_doi is required unless --pdf is provided')

    download_paper_and_extract(args.id_or_doi, args.output_dir, save_html=bool(args.save_html))


if __name__ == '__main__':
    main()

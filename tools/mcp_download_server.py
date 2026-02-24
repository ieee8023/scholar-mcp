import contextlib
import logging
import sys
from typing import Any, Dict

from mcp.server.fastmcp import FastMCP

from .download_paper_text import download_paper_and_extract


_LOGGER = logging.getLogger("scholar-mcp.download")
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
_LOGGER.addHandler(_handler)
_LOGGER.setLevel(logging.INFO)


@contextlib.contextmanager
def _redirect_stdout_to_stderr():
    with contextlib.redirect_stdout(sys.stderr):
        yield


mcp = FastMCP("scholar-mcp-download")


@mcp.tool(name="scholar-mcp.download.extract")
def download_extract(paper_id_or_doi: str, output_dir: str) -> Dict[str, Any]:
    """Download a paper PDF and extract text.

    Args:
        paper_id_or_doi:
            A paper identifier usable with Semantic Scholar. Recommended forms:
            - Semantic Scholar `paperId` (e.g., "649def34f8be52c8b66281af98ae884c09aef38b")
            - DOI with prefix (e.g., "DOI:10.1038/s41746-023-00919-1")
            Also accepted:
            - Bare DOI (e.g., "10.1038/s41746-023-00919-1")
            - DOI URL (e.g., "https://doi.org/10.1038/s41746-023-00919-1")
        output_dir:
            Directory where artifacts are written.

    The MCP client should pass output_dir as a folder named "papers" inside
    the workspace (for example: <workspace>/papers). Returns file paths to the
    PDF and extracted text.
    """
    with _redirect_stdout_to_stderr():
        return download_paper_and_extract(paper_id_or_doi, output_dir, save_html=False)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

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

import argparse
import contextlib
import json
import logging
import os
import sys
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

# Defer importing the download helper until runtime so CLI args set at process
# startup (in `main`) are honored by `download_paper_text.load_download_config`.


_LOGGER = logging.getLogger("scholar-mcp.download")
# Write logs to stdout so MCP hosts that mark stderr as warnings don't
# display ordinary INFO logs as warnings.
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
_LOGGER.addHandler(_handler)
_LOGGER.setLevel(logging.INFO)


@contextlib.contextmanager
def _redirect_stdout_to_stderr():
    # Historically we redirected stdout -> stderr; that makes MCP hosts treat
    # normal output as warnings. Keep an explicit redirect to stdout (noop)
    # so any prints from helpers go to the same stream as logs.
    with contextlib.redirect_stdout(sys.stdout):
        yield


mcp = FastMCP("scholar-mcp-download")


@mcp.tool(name="scholar-mcp-download-extract")
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
        # Import here so the download module reads `MCP_DOWNLOAD_CONFIG` set at
        # process startup (in `main`) instead of relying on MCP_SERVER_ARGS.
        from .download_paper_text import download_paper_and_extract
        return download_paper_and_extract(paper_id_or_doi, output_dir, save_html=False)


def main() -> None:
    parser = argparse.ArgumentParser(prog="scholar-mcp-download")
    parser.add_argument("--download-domain", help="Mirror/download domain to use")
    parser.add_argument("--unpaywall-email", help="Email for Unpaywall API")
    parser.add_argument("--mcp-args-json", help="Raw JSON to set as MCP_SERVER_ARGS")
    args = parser.parse_args()

    # Build a download-specific config from CLI args and set MCP_DOWNLOAD_CONFIG.
    cli_cfg: Dict[str, str] = {}
    if args.download_domain:
        cli_cfg["download_domain"] = args.download_domain
    if args.unpaywall_email:
        cli_cfg["unpaywall_email"] = args.unpaywall_email
    if args.mcp_args_json:
        try:
            cli_cfg = {**cli_cfg, **json.loads(args.mcp_args_json)}
        except Exception:
            pass

    if cli_cfg:
        try:
            os.environ["MCP_DOWNLOAD_CONFIG"] = json.dumps(cli_cfg)
        except Exception:
            os.environ["MCP_DOWNLOAD_CONFIG"] = json.dumps(cli_cfg)

    # Echo effective MCP args so host logs show what configuration is in use.
    effective = os.environ.get("MCP_SERVER_ARGS") or os.environ.get("MCP_DOWNLOAD_CONFIG")
    if effective:
        try:
            parsed = json.loads(effective)
            # Log compact single-line JSON to avoid hosts parsing multiline logs
            _LOGGER.info("Effective MCP args: %s", json.dumps(parsed, separators=(",", ":")))
        except Exception:
            _LOGGER.info("Effective MCP args (raw): %s", effective)
    else:
        _LOGGER.info("No MCP args supplied; using package defaults and env vars.")

    mcp.run()


if __name__ == "__main__":
    main()

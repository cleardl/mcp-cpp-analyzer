"""Native FastMCP filesystem entrypoint."""
import sys

from mcp_cpp_analyzer.server import create_mcp

sys.stderr.reconfigure(encoding="utf-8")

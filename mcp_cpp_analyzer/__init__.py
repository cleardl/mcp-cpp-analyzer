"""C++ semantic analysis for MCP clients."""

__version__ = "0.3.1"


def build_info():
    import json
    from pathlib import Path
    import sys

    if not getattr(sys, "frozen", False):
        return {"kind": "source", "version": __version__, "revision": None}
    path = Path(__file__).with_name("_build_info.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value["version"] != __version__:
        raise RuntimeError("Packaged build metadata does not match the service version")
    return {"kind": "packaged", **value}

"""Self-contained stdio executable entrypoint."""
import os
import sys
from pathlib import Path

def main():
    from mcp_cpp_analyzer.stdio import reserve_protocol_stdout, serve_stdio
    protocol = reserve_protocol_stdout()
    import anyio
    from mcp_cpp_analyzer.config import Config
    from mcp_cpp_analyzer.server import create_server

    if getattr(sys, "frozen", False):
        sandbox = Path(__file__).resolve().with_name("monty.exe")
        if not sandbox.is_file():
            raise FileNotFoundError(f"Bundled Code Mode runtime is missing: {sandbox}")
        os.environ["MONTY_BIN"] = str(sandbox)
    backend = Path(sys.argv[0]).resolve().with_name("clangd.exe")
    config = Config.from_environment(clangd_path=backend)
    anyio.run(serve_stdio, create_server(config), protocol)


if __name__ == "__main__":
    main()

import argparse
import shutil
import sys
from pathlib import Path


RULES_DIR = Path(__file__).parent.parent / "rules"


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-cpp-analyzer",
        description="MCP-CPP-Analyzer: A C++ Intelligence Server powered by clangd"
    )
    subparsers = parser.add_subparsers(dest="command")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start the MCP server")
    serve_parser.add_argument(
        "--transport", type=str, choices=["stdio", "sse"],
        default="sse", help="Transport protocol (default: sse)"
    )
    serve_parser.add_argument(
        "--compile-db-path", type=Path, required=True,
        help="Directory containing compile_commands.json"
    )
    serve_parser.add_argument("--clangd-path", default="clangd", help="Path to clangd executable")
    serve_parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    serve_parser.add_argument("--init-timeout", type=float, default=30.0, help="LSP initialization timeout")
    serve_parser.add_argument("--hover-timeout", type=float, default=10.0, help="Hover request timeout")
    serve_parser.add_argument("--definition-timeout", type=float, default=20.0, help="Definition request timeout")
    serve_parser.add_argument("--references-timeout", type=float, default=30.0, help="References request timeout")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    serve_parser.add_argument("--port", type=int, default=8000, help="Port to bind to")

    # install-rule
    rule_parser = subparsers.add_parser(
        "install-rule",
        help="Install Cursor rule files into a project's .cursor/rules/ directory"
    )
    rule_parser.add_argument(
        "target", nargs="?", default=".",
        help="Target project directory (default: current directory)"
    )

    return parser


def cmd_install_rule(args: argparse.Namespace) -> None:
    target_dir = Path(args.target).resolve()
    if not target_dir.is_dir():
        print(f"Error: target directory does not exist: {target_dir}", file=sys.stderr)
        sys.exit(1)

    rules_dest = target_dir / ".cursor" / "rules"
    rules_dest.mkdir(parents=True, exist_ok=True)

    if not RULES_DIR.is_dir():
        print(f"Error: rules source directory not found: {RULES_DIR}", file=sys.stderr)
        sys.exit(1)

    installed = []
    for rule_file in RULES_DIR.glob("*.mdc"):
        dest_file = rules_dest / rule_file.name
        if dest_file.exists():
            print(f"  Skipped (already exists): {dest_file}")
            continue
        shutil.copy2(rule_file, dest_file)
        installed.append(rule_file.name)
        print(f"  Installed: {dest_file}")

    if installed:
        print(f"\nDone! Installed {len(installed)} rule(s) to {rules_dest}")
    else:
        print(f"\nNo new rules to install. All rules already exist in {rules_dest}")


def cmd_serve(args: argparse.Namespace) -> None:
    from .config import Config, set_global_config
    from .server import main as server_main

    config = Config(
        compile_db_dir=args.compile_db_path.resolve(),
        clangd_path=args.clangd_path,
        lsp_init_timeout=args.init_timeout,
        lsp_hover_timeout=args.hover_timeout,
        lsp_definition_timeout=args.definition_timeout,
        lsp_references_timeout=args.references_timeout,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        transport=args.transport,
    )
    set_global_config(config)
    server_main()


def main():
    parser = create_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "serve":
        cmd_serve(args)
    elif args.command == "install-rule":
        cmd_install_rule(args)


if __name__ == "__main__":
    main()

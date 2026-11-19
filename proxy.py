"""Lightweight stdio entrypoint. The shared backend owns the analysis runtime."""


def main():
    from mcp_cpp_semantic_search.stdio import reserve_protocol_stdout
    protocol = reserve_protocol_stdout()
    import argparse
    from pathlib import Path
    from mcp_cpp_semantic_search.shared_runtime import load_profile, new_profile
    from mcp_cpp_semantic_search.proxy import Proxy

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, help="Explicit profile for source-development validation")
    arguments = parser.parse_args()
    profile = load_profile(arguments.profile) if arguments.profile else new_profile()
    Proxy(profile, protocol).run()


if __name__ == "__main__":
    main()

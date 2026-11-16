"""Build the self-contained stdio program from a clean, committed source tree."""
import argparse
from importlib.metadata import PathDistribution
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig

from mcp_cpp_analyzer import __version__

ROOT = Path(__file__).resolve().parent


def git(*arguments):
    return subprocess.check_output(["git", "-C", str(ROOT), *arguments], encoding="utf-8").strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if git("status", "--porcelain"):
        raise RuntimeError("Commit source changes before packaging so the executable maps to one revision")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if sys.platform == "win32" else ""
    worker = Path(sysconfig.get_path("scripts")) / ("monty" + suffix)
    if not worker.is_file():
        raise FileNotFoundError(f"Code Mode worker is missing: {worker}")
    metadata = {"version": __version__, "revision": git("rev-parse", "HEAD"), "dirty": False}
    metadata_path = output / "_build_info.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    command = [sys.executable, "-X", "utf8", "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--console", "--noupx", "--name=mcp-cpp-analyzer",
        f"--distpath={output}", f"--workpath={output / 'work'}", f"--specpath={output}",
        f"--paths={ROOT}", "--hidden-import=fastmcp.server.server", "--hidden-import=fastmcp.server.context",
        "--hidden-import=fastmcp.experimental.transforms.code_mode", "--hidden-import=pydantic_monty._monty",
        "--hidden-import=pydantic_monty._binary", f"--add-binary={worker}{os.pathsep}.",
        f"--add-data={metadata_path}{os.pathsep}mcp_cpp_analyzer",
        "--collect-data=fastmcp", "--copy-metadata=fastmcp-slim", "--copy-metadata=fastmcp",
        "--copy-metadata=mcp", str(ROOT / "server.py")]
    with (output / "pyinstaller-build.log").open("w", encoding="utf-8") as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    executable = output / ("mcp-cpp-analyzer" + suffix)
    if not executable.is_file():
        raise FileNotFoundError(executable)
    sections = ["Python runtime\n\n" + (Path(sys.base_prefix) / "LICENSE.txt").read_text(encoding="utf-8")]
    for folder in sorted(Path(sysconfig.get_path("purelib")).glob("*.dist-info")):
        distribution = PathDistribution(folder)
        for file in sorted(folder.rglob("*")):
            if file.is_file() and file.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                title = f"{distribution.metadata['Name']} {distribution.version} / {file.relative_to(folder)}"
                sections.append(title + "\n\n" + file.read_text(encoding="utf-8"))
    (output / "mcp-cpp-analyzer-licenses.txt").write_text(
        ("\n\n" + "=" * 72 + "\n\n").join(sections), encoding="utf-8")
    print(json.dumps({**metadata, "executable": str(executable), "bytes": executable.stat().st_size}))


if __name__ == "__main__":
    main()

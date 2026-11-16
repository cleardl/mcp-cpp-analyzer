"""Build the stdlib-only stdio proxy and self-contained shared backend."""
import argparse
from importlib.metadata import PathDistribution
import json
import os
from pathlib import Path
import subprocess
import shutil
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
        "--noconfirm", "--onefile", "--windowed", "--disable-windowed-traceback", "--noupx",
        "--name=mcp-cpp-analyzer-backend",
        f"--distpath={output}", f"--workpath={output / 'work-backend'}", f"--specpath={output}",
        f"--paths={ROOT}", "--hidden-import=fastmcp.server.server", "--hidden-import=fastmcp.server.context",
        "--hidden-import=fastmcp.experimental.transforms.code_mode", "--hidden-import=pydantic_monty._monty",
        "--hidden-import=pydantic_monty._binary", f"--add-binary={worker}{os.pathsep}.",
        f"--add-data={metadata_path}{os.pathsep}mcp_cpp_analyzer",
        "--collect-data=fastmcp", "--copy-metadata=fastmcp-slim", "--copy-metadata=fastmcp",
        "--copy-metadata=mcp", str(ROOT / "server.py")]
    with (output / "pyinstaller-build.log").open("w", encoding="utf-8") as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
        proxy_command = [sys.executable, "-X", "utf8", "-m", "PyInstaller", "--noconfirm", "--onedir",
            "--console", "--noupx", "--name=mcp-cpp-analyzer-proxy", "--contents-directory=_proxy",
            f"--distpath={output / 'proxy-stage'}", f"--workpath={output / 'work-proxy'}", f"--specpath={output}",
            f"--paths={ROOT}", f"--add-data={metadata_path}{os.pathsep}mcp_cpp_analyzer",
            "--exclude-module=fastmcp", "--exclude-module=mcp", "--exclude-module=pydantic",
            "--exclude-module=uvicorn", "--exclude-module=pydantic_monty", "--exclude-module=anyio",
            str(ROOT / "proxy.py")]
        subprocess.run(proxy_command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    staged = output / "proxy-stage/mcp-cpp-analyzer-proxy"
    runtime = output / "_proxy"
    if runtime.resolve().parent != output or runtime.is_symlink():
        raise RuntimeError("Proxy runtime directory escaped the requested build output")
    if runtime.exists():
        shutil.rmtree(runtime)
    shutil.copytree(staged / "_proxy", runtime)
    shutil.copy2(staged / ("mcp-cpp-analyzer-proxy" + suffix), output)
    artifacts = [output / (name + suffix) for name in ("mcp-cpp-analyzer-proxy", "mcp-cpp-analyzer-backend")]
    for artifact in artifacts:
        if not artifact.is_file():
            raise FileNotFoundError(artifact)
    sections = ["Python runtime\n\n" + (Path(sys.base_prefix) / "LICENSE.txt").read_text(encoding="utf-8")]
    for folder in sorted(Path(sysconfig.get_path("purelib")).glob("*.dist-info")):
        distribution = PathDistribution(folder)
        for file in sorted(folder.rglob("*")):
            if file.is_file() and file.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                title = f"{distribution.metadata['Name']} {distribution.version} / {file.relative_to(folder)}"
                sections.append(title + "\n\n" + file.read_text(encoding="utf-8"))
    (output / "mcp-cpp-analyzer-licenses.txt").write_text(
        ("\n\n" + "=" * 72 + "\n\n").join(sections), encoding="utf-8")
    print(json.dumps({**metadata, "artifacts": {str(file): file.stat().st_size for file in artifacts},
                      "proxy_runtime_bytes": sum(file.stat().st_size for file in runtime.rglob("*") if file.is_file())}))


if __name__ == "__main__":
    main()

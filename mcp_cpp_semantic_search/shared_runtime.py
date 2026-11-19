"""Locate/start a backend without importing FastMCP, clangd, or the sandbox."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time

from . import build_info
from .shared_protocol import connect
from .windows_runtime import launch_task, private_directory, process_alive, read_shared_text, task_command, task_xml, user_sid, workspace_lock

IDLE_SECONDS = 30
STARTUP_SECONDS = 80


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    try:
        return json.loads(read_shared_text(path))
    except FileNotFoundError:
        return None


def identity(value):
    fields = {key: value[key] for key in ("workspace", "compile_db", "tool_mode", "build", "user",
                                         "backend_command", "backend_arguments", "clangd")}
    for name in ("backend_command", "clangd"):
        fields[name] = os.path.normcase(fields[name])
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def new_profile(*, workspace=None, compile_db=None, clangd=None, backend_command=None, backend_arguments=None):
    root = Path(workspace or Path.cwd()).resolve()
    database = compile_db if compile_db is not None else os.environ.get("CPP_SEMANTIC_SEARCH_COMPILE_DB_PATH")
    if not database or not str(database).strip():
        raise ValueError("Required environment variable CPP_SEMANTIC_SEARCH_COMPILE_DB_PATH is missing")
    database = (root / database).resolve()
    if not root.is_dir() or not (database / "compile_commands.json").is_file():
        raise ValueError(f"Invalid workspace or missing compilation database: {root}; {database}")
    mode = os.environ.get("CPP_SEMANTIC_SEARCH_TOOL_MODE", "code")
    if mode not in ("code", "direct", "search"):
        raise ValueError(f"Unknown tool mode: {mode}")
    if backend_command is None:
        if not getattr(sys, "frozen", False):
            raise ValueError("Source execution requires an explicit backend command and clangd path")
        backend_command = Path(sys.executable).with_name("mcp-cpp-semantic-search-backend.exe")
        backend_arguments = []
    command = Path(backend_command).resolve()
    clangd = Path(clangd).resolve() if clangd else command.with_name("clangd.exe")
    if not command.is_file() or not clangd.is_file():
        raise FileNotFoundError(f"Bundled backend or clangd is missing: {command}; {clangd}")
    value = {"workspace": os.path.normcase(str(root)), "compile_db": os.path.normcase(str(database)),
             "tool_mode": mode, "build": build_info(), "user": user_sid(),
             "backend_command": str(command), "backend_arguments": [str(item) for item in backend_arguments or []],
             "clangd": str(clangd), "idle_seconds": IDLE_SECONDS}
    value["key"] = identity(value)
    value["task_name"] = "McpCppSemanticSearch-" + value["key"]
    value["mutex"] = "Local\\McpCppSemanticSearch-" + value["key"]
    directory = private_directory(Path(os.environ["LOCALAPPDATA"]) / "mcp-cpp-semantic-search" / value["key"])
    value["profile_path"] = str(directory / "profile.json")
    return value


def load_profile(path):
    value = read_json(Path(path))
    if value is None or value["key"] != identity(value) or value["user"] != user_sid():
        raise ValueError("Invalid shared-backend workspace profile")
    if value["build"] != build_info() or value["idle_seconds"] != IDLE_SECONDS:
        raise ValueError("Workspace profile belongs to a different program build")
    if Path(value["profile_path"]).resolve() != Path(path).resolve():
        raise ValueError("Workspace profile path does not match its contents")
    if value["task_name"] != "McpCppSemanticSearch-" + value["key"] or value["mutex"] != "Local\\McpCppSemanticSearch-" + value["key"]:
        raise ValueError("Workspace profile has invalid ownership identifiers")
    return value


def _endpoint(directory, profile):
    value = read_json(directory / "instance.json")
    if value is not None and value["key"] != profile["key"]:
        raise ValueError("Shared-backend endpoint belongs to another workspace")
    return value


def ensure_connection(profile):
    directory = Path(profile["profile_path"]).parent
    with workspace_lock(profile["mutex"], timeout=STARTUP_SECONDS + 10):
        endpoint = _endpoint(directory, profile)
        if endpoint is not None and process_alive(endpoint["pid"], endpoint["created"]):
            try:
                connection, stream = connect(endpoint)
                return endpoint, connection, stream
            except (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError):
                deadline = time.monotonic() + 3
                while process_alive(endpoint["pid"], endpoint["created"]) and time.monotonic() < deadline:
                    time.sleep(0.02)
                if process_alive(endpoint["pid"], endpoint["created"]):
                    raise ConnectionError(f"Backend process {endpoint['pid']} is alive but its endpoint failed; "
                                          f"inspect {directory / 'backend.log'}")
        launch_id = secrets.token_hex(16)
        profile = {**profile, "launch_id": launch_id}
        atomic_json(Path(profile["profile_path"]), profile)
        definition = directory / "task.xml"
        definition.write_text(task_xml(profile["backend_command"],
            [*profile["backend_arguments"], "--profile", profile["profile_path"]],
            Path(profile["backend_command"]).parent),
            encoding="utf-16")
        launch_task(profile["task_name"], definition)
        deadline, last_run = time.monotonic() + STARTUP_SECONDS, time.monotonic()
        while time.monotonic() < deadline:
            endpoint = _endpoint(directory, profile)
            if endpoint is not None and endpoint.get("launch_id") == launch_id and process_alive(endpoint["pid"], endpoint["created"]):
                connection, stream = connect(endpoint)
                return endpoint, connection, stream
            failure = read_json(directory / "failure.json")
            if failure is not None and failure.get("launch_id") == launch_id:
                raise RuntimeError("Shared backend failed to start: " + failure["error"])
            started = read_json(directory / "starting.json")
            if time.monotonic() - last_run > 2 and (started is None or not process_alive(started["pid"], started["created"])):
                # A previous idle instance can still be leaving Task Scheduler
                # just after releasing the launch mutex. Run again once it has left.
                task_command("/Run", "/TN", profile["task_name"])
                last_run = time.monotonic()
            time.sleep(0.05)
        raise TimeoutError(f"Shared backend did not become ready; inspect {directory / 'backend.log'}")

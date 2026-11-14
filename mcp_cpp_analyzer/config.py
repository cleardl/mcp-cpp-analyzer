"""Validated, workspace-local runtime settings."""
from dataclasses import dataclass
from pathlib import Path
import os
import shutil

@dataclass(frozen=True)
class Config:
    compile_db_dir: Path
    workspace_root: Path
    clangd_path: str
    request_timeout: float = 30.0
    init_timeout: float = 60.0
    concurrency: int = 8
    max_open_documents: int = 32
    background_index: bool = True
    tool_mode: str = "code"

    def __post_init__(self):
        for name in ("compile_db_dir", "workspace_root"):
            value = Path(getattr(self, name)).resolve()
            if not value.is_dir():
                raise ValueError(f"{name} is not a directory: {value}")
            object.__setattr__(self, name, value)
        if not self.find_compile_commands().is_file():
            raise ValueError(f"Compilation database is missing: {self.find_compile_commands()}")
        executable = shutil.which(self.clangd_path)
        if executable is None:
            raise ValueError(f"clangd executable is unavailable: {self.clangd_path}")
        object.__setattr__(self, "clangd_path", str(Path(executable).resolve()))
        if not 1 <= self.concurrency <= 32 or not 1 <= self.max_open_documents <= 256:
            raise ValueError("concurrency must be 1..32 and max_open_documents 1..256")
        if self.request_timeout <= 0 or self.init_timeout <= 0:
            raise ValueError("Request and initialization timeouts must be positive")
        if self.tool_mode not in ("direct", "search", "code"):
            raise ValueError(f"Unknown tool mode: {self.tool_mode}")

    def find_compile_commands(self) -> Path:
        return self.compile_db_dir / "compile_commands.json"

    @classmethod
    def from_environment(cls):
        """Read backend paths from the MCP client's process environment."""
        values = {}
        for field, name in (("compile_db_dir", "CPP_ANALYZER_COMPILE_DB_PATH"),
                            ("clangd_path", "CPP_ANALYZER_CLANGD_PATH")):
            value = os.environ.get(name)
            if not value or not value.strip():
                raise ValueError(f"Required environment variable {name} is missing")
            values[field] = value
        return cls(workspace_root=Path.cwd(), **values,
                   tool_mode=os.environ.get("CPP_ANALYZER_TOOL_MODE", "code"))

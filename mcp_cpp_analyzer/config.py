import os
import shutil
import argparse
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    
    compile_db_dir: Path
    clangd_path: str = "clangd"
    lsp_init_timeout: float = 5.0
    lsp_hover_timeout: float = 5.0
    lsp_definition_timeout: float = 10.0
    lsp_references_timeout: float = 10.0
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"

    def __post_init__(self):
        
        self.compile_db_dir = Path(self.compile_db_dir).resolve()
        if not self.compile_db_dir.exists():
            raise ValueError(f"Compile database directory does not exist: {self.compile_db_dir}")
        if not self.compile_db_dir.is_dir():
            raise ValueError(f"Compile database directory is not a directory: {self.compile_db_dir}")
            
        self._validate_clangd()

    def find_compile_commands(self) -> Path:
        return self.compile_db_dir / "compile_commands.json"
        
    def _validate_clangd(self) -> None:
        """Validate clangd executable"""
        
        if self.clangd_path == "clangd":
            if not shutil.which("clangd"):
                raise ValueError(
                    "clangd not found in PATH. Please install clangd or specify path with --clangd-path"
                )
        else:
            clangd_path = Path(self.clangd_path)
            if not clangd_path.exists():
                raise ValueError(f"Specified clangd path does not exist: {self.clangd_path}")
            if not clangd_path.is_file():
                raise ValueError(f"Specified clangd path is not a file: {self.clangd_path}")

_global_config: Optional[Config] = None

def set_global_config(config: Config) -> None:
    global _global_config
    _global_config = config

def get_global_config() -> Config:
    if _global_config is None:
        raise RuntimeError("Config not initialized. Call set_global_config() first.")
    return _global_config
def set_config_from_cli() -> None: 
    """Create config from command line arguments and set as global config"""
    parser = argparse.ArgumentParser(description="MCP-CPP-Analyzer, A C++ Intelligence Server")
    parser.add_argument("--compile-db-path", type=Path, required=True, help="Directory containing compile_commands.json")
    parser.add_argument("--clangd-path", default="clangd", help="Path to clangd executable")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    parser.add_argument("--init-timeout", type=float, default=30.0, help="LSP initialization timeout")
    parser.add_argument("--hover-timeout", type=float, default=10.0, help="Hover request timeout")
    parser.add_argument("--definition-timeout", type=float, default=20.0, help="Definition request timeout")
    parser.add_argument("--references-timeout", type=float, default=30.0, help="References request timeout")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to")
    args = parser.parse_args()
    
    compile_db_dir = args.compile_db_path.resolve()
    if not compile_db_dir.exists():
        raise ValueError(f"Compile database directory does not exist: {compile_db_dir}")
    
    config = Config(
        compile_db_dir=compile_db_dir,
        clangd_path=args.clangd_path,
        lsp_init_timeout=args.init_timeout,
        lsp_hover_timeout=args.hover_timeout,
        lsp_definition_timeout=args.definition_timeout,
        lsp_references_timeout=args.references_timeout,
        host=args.host,
        port=args.port,
        log_level=args.log_level
    )
    set_global_config(config)
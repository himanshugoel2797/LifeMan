from __future__ import annotations

import secrets
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "LIFEMAN_"}

    # Directories
    data_dir: Path = Path.home() / ".lifeman"
    db_path: Path | None = None  # defaults to data_dir/data.db

    # Auth
    token: str = secrets.token_urlsafe(32)

    # Server
    host: str = "127.0.0.1"
    port: int = 8390

    # Sandbox
    sandbox_enabled: bool = True
    bwrap_path: str = "bwrap"

    # LLM
    llama_server_url: str = "http://127.0.0.1:8080"

    def get_db_path(self) -> Path:
        return self.db_path or (self.data_dir / "data.db")

    def get_tools_dir(self) -> Path:
        return self.data_dir / "tools"


settings = Settings()

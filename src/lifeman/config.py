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

    # LLM (Ollama, OpenAI-compatible at /v1/chat/completions)
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen3.5:latest"
    llm_system_prompt: str = (
        "You are lifeman, a personal companion AI running locally on the user's hardware. "
        "You have access to tools that let you invoke registered tools, schedule reminders, "
        "request new tools to be built, send notifications, and check the current time. "
        "Be concise. Use tools when appropriate. Do not guess the time — call the now tool."
    )

    # Output router LLM fallback. When the rule table matches nothing, the
    # router can ask the local LLM to pick channels instead of silently
    # defaulting to the digest. Off by default in tests; on in normal runs.
    output_router_llm_fallback: bool = True
    output_router_llm_timeout: float = 5.0

    # Ollama supervisor
    ollama_bin: str = "ollama"
    ollama_autostart: bool = True
    ollama_startup_timeout: float = 30.0

    # Build chat (Claude Code wrapper)
    claude_cli: str = "claude"
    build_workspace_dir: Path | None = None  # defaults to data_dir/build_workspaces

    # Backups. The SQLite file is snapshotted with VACUUM INTO and encrypted
    # with the master key; the master key itself must be backed up separately.
    backup_dir: Path | None = None              # defaults to data_dir/backups
    backup_enabled: bool = True
    backup_interval_hours: float = 24.0         # 0 disables the scheduled task
    backup_retention_count: int = 14            # keep the N most-recent files

    def get_db_path(self) -> Path:
        return self.db_path or (self.data_dir / "data.db")

    def get_tools_dir(self) -> Path:
        return self.data_dir / "tools"

    def get_build_workspace_dir(self) -> Path:
        return self.build_workspace_dir or (self.data_dir / "build_workspaces")

    def get_backup_dir(self) -> Path:
        return self.backup_dir or (self.data_dir / "backups")


settings = Settings()

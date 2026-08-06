from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="GIT_PG_",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = "postgresql+asyncpg://gitpg:gitpg@localhost:54329/gitpg"
    sessions_root: str = "/tmp/git-pg/sessions"
    warm_cache_root: str = "/tmp/git-pg/warm"
    warm_cache_enabled: bool = True
    agent_docker_image: str = "git-pg-agent:local"
    compose_project_name: str = "git-pg"
    compose_project_dir: str = ""
    demo_repo: str = "demo"
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3010",
            "http://127.0.0.1:3010",
        ]
    )
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    anthropic_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "GIT_PG_ANTHROPIC_API_KEY"),
    )
    task_gen_model: str = "claude-haiku-4-5-20251001"

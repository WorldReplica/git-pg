from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GIT_PG_")

    database_url: str = "postgresql+asyncpg://gitpg:gitpg@localhost:54329/gitpg"
    sessions_root: str = "/tmp/git-pg/sessions"
    warm_cache_root: str = "/tmp/git-pg/warm"
    warm_cache_enabled: bool = True
    migration_author_name: str = "git-pg migration"
    migration_author_email: str = "migration@git-pg.local"


def get_settings() -> Settings:
    return Settings()

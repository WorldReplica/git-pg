from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from git_pg.db.base import Base
from git_pg.db.orm import models as _orm_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return os.environ.get(
        "GIT_PG_DATABASE_URL",
        config.get_main_option("sqlalchemy.url"),
    )


def run_migrations_offline() -> None:
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is None:
        configuration = config.get_section(config.config_ini_section) or {}
        configuration["sqlalchemy.url"] = _database_url().replace(
            "+asyncpg", "+psycopg"
        )
        connectable = engine_from_config(
            configuration,
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

    external_transaction = config.attributes.get("external_transaction", False)

    def do_run_migrations(connection) -> None:  # noqa: ANN001
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            transaction_per_migration=False,
        )
        if external_transaction:
            context.run_migrations()
        else:
            with context.begin_transaction():
                context.run_migrations()

    if config.attributes.get("connection") is not None:
        do_run_migrations(connectable)
    else:
        with connectable.connect() as connection:
            do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

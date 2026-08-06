from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from git_pg.config import Settings, get_settings
from git_pg.db.orm.models import SpecialRule
from git_pg.models.repo import GitOid, MigrateApplyResult, RefName
from git_pg.special.registry import HandlerRegistry, default_registry
from git_pg.store.postgres import PostgresGitStore, _parse_commit_links


def alembic_config(alembic_ini: Path, database_url: str | None = None) -> Config:
    root = alembic_ini.resolve().parent
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(root / "alembic"))
    if database_url is not None:
        cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _current_revision(sync_session: Session) -> str | None:
    connection = sync_session.connection()
    context = MigrationContext.configure(connection=connection)
    return context.get_current_revision()


def _upgrade_one_step(sync_session: Session, cfg: Config) -> None:
    connection = sync_session.connection()
    cfg.attributes["connection"] = connection
    cfg.attributes["external_transaction"] = True
    command.upgrade(cfg, "+1")


def _downgrade_one_step(sync_session: Session, cfg: Config) -> None:
    connection = sync_session.connection()
    cfg.attributes["connection"] = connection
    cfg.attributes["external_transaction"] = True
    command.downgrade(cfg, "-1")


async def rematerialize_repo(
    session: AsyncSession,
    repo_name: str,
    ref: RefName,
    migration_revision: str,
    migration_description: str,
    registry: HandlerRegistry | None = None,
    settings: Settings | None = None,
    *,
    message_prefix: str = "migrate",
) -> MigrateApplyResult:
    cfg = settings or get_settings()
    reg = registry or default_registry()
    store = PostgresGitStore(session)
    repo = await store.get_repo(repo_name)
    if repo is None:
        msg = f"repo {repo_name!r} not found"
        raise LookupError(msg)

    previous_oid = await store.get_ref_oid(repo.id, ref)
    if previous_oid is None:
        msg = f"ref {ref.heads_name} not found"
        raise LookupError(msg)

    commit_content = await store.get_object(repo.id, previous_oid)
    if commit_content is None:
        msg = "HEAD commit missing"
        raise LookupError(msg)

    _, root_tree_oid = _parse_commit_links(commit_content)

    rules_result = await session.execute(
        select(SpecialRule).where(SpecialRule.repo_id == repo.id)
    )
    rules = list(rules_result.scalars().all())
    blobs = await reg.materialize_paths(session, repo.id, rules)

    new_tree_oid = root_tree_oid
    for path, blob in blobs.items():
        new_tree_oid = await store.replace_tree_blob(repo.id, new_tree_oid, path, blob)

    message = f"{message_prefix}: {migration_description} ({migration_revision})"
    new_commit_oid = await store.write_commit_from_tree(
        repo_id=repo.id,
        parent_oid=previous_oid,
        tree_oid=new_tree_oid,
        author_name=cfg.migration_author_name,
        author_email=cfg.migration_author_email,
        message=message,
    )
    await store.set_ref(repo.id, ref.heads_name, new_commit_oid)

    return MigrateApplyResult(
        repo=repo_name,
        migration_revision=migration_revision,
        migration_message=message,
        new_commit=GitOid(hex=new_commit_oid.hex()),
        previous_commit=GitOid(hex=previous_oid.hex()),
    )


async def apply_migrations(
    session: AsyncSession,
    repo_name: str,
    ref: RefName,
    alembic_ini: Path,
    target_revision: str | None = None,
    registry: HandlerRegistry | None = None,
    settings: Settings | None = None,
) -> MigrateApplyResult | None:
    """Apply pending Alembic revisions, rematerializing git blobs after each."""
    cfg_settings = settings or get_settings()
    cfg = alembic_config(alembic_ini, database_url=cfg_settings.database_url)
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    last: MigrateApplyResult | None = None

    while True:
        current = await session.run_sync(_current_revision)
        if current == head:
            break
        if target_revision is not None and current == target_revision:
            break

        await session.run_sync(_upgrade_one_step, cfg)

        new_current = await session.run_sync(_current_revision)
        if new_current is None:
            msg = "alembic upgrade did not advance revision"
            raise RuntimeError(msg)

        rev = script.get_revision(new_current)
        description = rev.doc if rev and rev.doc else new_current
        last = await rematerialize_repo(
            session,
            repo_name,
            ref,
            new_current,
            description,
            registry=registry,
            settings=settings,
        )

        if target_revision is not None and new_current == target_revision:
            break

    return last


async def downgrade_migrations(
    session: AsyncSession,
    repo_name: str,
    ref: RefName,
    alembic_ini: Path,
    target_revision: str,
    registry: HandlerRegistry | None = None,
    settings: Settings | None = None,
) -> MigrateApplyResult | None:
    """Downgrade Alembic revisions one step at a time, rematerializing after each.

    Git history stays append-only: each downgrade writes a *new* rematerialize
    commit on top of HEAD (never resets refs to an older commit).
    """
    cfg_settings = settings or get_settings()
    cfg = alembic_config(alembic_ini, database_url=cfg_settings.database_url)
    script = ScriptDirectory.from_config(cfg)
    last: MigrateApplyResult | None = None

    while True:
        current = await session.run_sync(_current_revision)
        if current is None or current == target_revision:
            break

        leaving = script.get_revision(current)
        leaving_doc = leaving.doc if leaving and leaving.doc else current

        await session.run_sync(_downgrade_one_step, cfg)

        new_current = await session.run_sync(_current_revision)
        if new_current == current:
            msg = "alembic downgrade did not move revision"
            raise RuntimeError(msg)

        revision_label = new_current if new_current is not None else "base"
        last = await rematerialize_repo(
            session,
            repo_name,
            ref,
            revision_label,
            f"downgrade {leaving_doc}",
            registry=registry,
            settings=settings,
            message_prefix="migrate",
        )

        if new_current == target_revision or new_current is None:
            break

    return last

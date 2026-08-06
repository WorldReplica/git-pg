from __future__ import annotations

from typing import ClassVar

import yaml
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from git_pg.db.orm.models import AppConfig


class AppConfigDocument(BaseModel):
    name: str
    port: int


class YamlAppConfigHandler:
    handler_id: ClassVar[str] = "yaml:app_config"
    table_names: ClassVar[tuple[str, ...]] = ("app_config",)

    async def ingest(
        self,
        session: AsyncSession,
        repo_id: int,
        path: str,
        blob: bytes,
    ) -> None:
        del path
        data = yaml.safe_load(blob.decode())
        if not isinstance(data, dict):
            msg = "config.yaml must be a mapping"
            raise ValueError(msg)
        doc = AppConfigDocument.model_validate(data)
        existing = await session.get(AppConfig, repo_id)
        if existing is None:
            session.add(
                AppConfig(
                    repo_id=repo_id,
                    name=doc.name,
                    port=doc.port,
                    raw=data,
                )
            )
        else:
            existing.name = doc.name
            existing.port = doc.port
            existing.raw = data
        await session.flush()

    async def materialize(
        self,
        session: AsyncSession,
        repo_id: int,
        path: str,
    ) -> bytes:
        del path
        row = await session.get(AppConfig, repo_id)
        if row is None:
            msg = "app_config row missing"
            raise LookupError(msg)
        doc = AppConfigDocument(name=row.name or "", port=row.port or 0)
        return yaml.safe_dump(doc.model_dump(), sort_keys=False).encode()

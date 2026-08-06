from __future__ import annotations

from typing import ClassVar, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from git_pg.db.orm.models import SpecialRule


class SpecialHandler(Protocol):
    handler_id: ClassVar[str]
    table_names: ClassVar[tuple[str, ...]]

    async def ingest(
        self,
        session: AsyncSession,
        repo_id: int,
        path: str,
        blob: bytes,
    ) -> None: ...

    async def materialize(
        self,
        session: AsyncSession,
        repo_id: int,
        path: str,
    ) -> bytes: ...


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, SpecialHandler] = {}

    def register(self, handler: SpecialHandler) -> None:
        self._handlers[handler.handler_id] = handler

    def get(self, handler_id: str) -> SpecialHandler:
        if handler_id not in self._handlers:
            msg = f"unknown handler: {handler_id}"
            raise KeyError(msg)
        return self._handlers[handler_id]

    async def ingest_path(
        self,
        session: AsyncSession,
        repo_id: int,
        rule: SpecialRule,
        blob: bytes,
    ) -> None:
        handler = self.get(rule.handler)
        await handler.ingest(session, repo_id, rule.path, blob)

    async def materialize_paths(
        self,
        session: AsyncSession,
        repo_id: int,
        rules: list[SpecialRule],
    ) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for rule in rules:
            handler = self.get(rule.handler)
            result[rule.path] = await handler.materialize(session, repo_id, rule.path)
        return result


def default_registry() -> HandlerRegistry:
    from git_pg.special.csv_rates import CsvRatesHandler
    from git_pg.special.yaml_app_config import YamlAppConfigHandler

    registry = HandlerRegistry()
    registry.register(CsvRatesHandler())
    registry.register(YamlAppConfigHandler())
    return registry

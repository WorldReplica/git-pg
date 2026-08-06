from __future__ import annotations

import csv
import io
from typing import ClassVar

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from git_pg.db.orm.models import Rate


class CsvRatesHandler:
    handler_id: ClassVar[str] = "csv:rates"
    table_names: ClassVar[tuple[str, ...]] = ("rates",)

    async def _rate_column_is_float(self, session: AsyncSession) -> bool:
        result = await session.execute(
            text(
                "SELECT udt_name FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "AND table_name = 'rates' AND column_name = 'rate'"
            )
        )
        return bool(result.scalar_one() == "float8")

    async def ingest(
        self,
        session: AsyncSession,
        repo_id: int,
        path: str,
        blob: bytes,
    ) -> None:
        del path
        reader = csv.DictReader(io.StringIO(blob.decode()))
        is_float = await self._rate_column_is_float(session)
        await session.execute(delete(Rate).where(Rate.repo_id == repo_id))
        for row in reader:
            name = row.get("name")
            rate_raw = row.get("rate")
            if name is None or rate_raw is None:
                continue
            if is_float:
                await session.execute(
                    text(
                        "INSERT INTO rates (repo_id, name, rate) "
                        "VALUES (:repo_id, :name, :rate)"
                    ),
                    {
                        "repo_id": repo_id,
                        "name": name,
                        "rate": _parse_rate_float(rate_raw),
                    },
                )
            else:
                session.add(Rate(repo_id=repo_id, name=name, rate=rate_raw))
        await session.flush()

    async def materialize(
        self,
        session: AsyncSession,
        repo_id: int,
        path: str,
    ) -> bytes:
        del path
        from sqlalchemy import text

        result = await session.execute(
            text(
                "SELECT name, rate::text FROM rates "
                "WHERE repo_id = :repo_id ORDER BY name"
            ),
            {"repo_id": repo_id},
        )
        rows = result.all()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["name", "rate"])
        for name, rate in rows:
            writer.writerow([name, rate])
        return buf.getvalue().encode()


def _parse_rate_float(value: str) -> float:
    if value.endswith("%"):
        return float(value[:-1]) / 100.0
    return float(value)

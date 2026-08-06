from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import BYTEA, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from git_pg.db.base import Base


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    objects: Mapped[list["GitObject"]] = relationship(back_populates="repository")
    refs: Mapped[list["GitRef"]] = relationship(back_populates="repository")
    special_rules: Mapped[list["SpecialRule"]] = relationship(
        back_populates="repository"
    )


class GitObject(Base):
    __tablename__ = "objects"

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    oid: Mapped[bytes] = mapped_column(BYTEA, primary_key=True)
    type: Mapped[int] = mapped_column(Integer, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[bytes] = mapped_column(BYTEA, nullable=False)

    repository: Mapped[Repository] = relationship(back_populates="objects")


class GitRef(Base):
    __tablename__ = "refs"

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    oid: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    symbolic: Mapped[str | None] = mapped_column(Text, nullable=True)

    repository: Mapped[Repository] = relationship(back_populates="refs")


class SpecialRule(Base):
    __tablename__ = "special_rules"
    __table_args__ = (UniqueConstraint("repo_id", "path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    handler: Mapped[str] = mapped_column(Text, nullable=False)

    repository: Mapped[Repository] = relationship(back_populates="special_rules")


class Rate(Base):
    __tablename__ = "rates"

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    rate: Mapped[str] = mapped_column(Text, nullable=False)


class AppConfig(Base):
    __tablename__ = "app_config"

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)

from pydantic import BaseModel, Field


class GitOid(BaseModel):
    model_config = {"frozen": True}

    hex: str = Field(min_length=40, max_length=40)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "GitOid":
        return cls(hex=raw.hex())


class RefName(BaseModel):
    model_config = {"frozen": True}

    value: str

    @property
    def heads_name(self) -> str:
        if self.value.startswith("refs/"):
            return self.value
        return f"refs/heads/{self.value}"


class RepoName(BaseModel):
    model_config = {"frozen": True}

    value: str


class SessionHandle(BaseModel):
    model_config = {"frozen": True}

    session_id: str
    repo: RepoName
    ref: RefName
    cwd: str
    spin_up_ms: float


class SessionStartRequest(BaseModel):
    repo: str
    ref: str = "main"
    session_id: str | None = None


class MigrateApplyRequest(BaseModel):
    repo: str
    ref: str = "main"
    alembic_ini: str
    migration_revision: str | None = None


class MigrateApplyResult(BaseModel):
    repo: str
    migration_revision: str
    migration_message: str
    new_commit: GitOid
    previous_commit: GitOid

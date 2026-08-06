-- Git-PG schema (gitgres-compatible core + special tables)

CREATE TABLE repositories (
    id   serial PRIMARY KEY,
    name text NOT NULL UNIQUE
);

CREATE TABLE objects (
    repo_id integer NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    oid     bytea NOT NULL,
    type    smallint NOT NULL,
    size    integer NOT NULL,
    content bytea NOT NULL,
    PRIMARY KEY (repo_id, oid)
);

CREATE INDEX objects_oid_idx ON objects (repo_id, oid);

CREATE TABLE refs (
    repo_id  integer NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    name     text NOT NULL,
    oid      bytea,
    symbolic text,
    PRIMARY KEY (repo_id, name)
);

CREATE TABLE special_rules (
    id      serial PRIMARY KEY,
    repo_id integer NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    path    text NOT NULL,
    handler text NOT NULL,
    UNIQUE (repo_id, path)
);

CREATE TABLE rates (
    repo_id integer NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    name    text NOT NULL,
    rate    text NOT NULL,
    PRIMARY KEY (repo_id, name)
);

CREATE TABLE app_config (
    repo_id integer NOT NULL PRIMARY KEY REFERENCES repositories(id) ON DELETE CASCADE,
    name    text,
    port    integer,
    raw     jsonb
);

-- Alembic revision tracking (managed by Alembic; stamped to baseline on fresh init)
CREATE TABLE IF NOT EXISTS alembic_version (
    version_num character varying(32) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

INSERT INTO alembic_version (version_num) VALUES ('20260806120000')
ON CONFLICT DO NOTHING;

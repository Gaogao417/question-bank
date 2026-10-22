"""Every test uses isolated schemas on a real, explicitly selected PostgreSQL DB."""
import hashlib
import uuid
import pytest
import psycopg
from psycopg import sql
from question_bank import db


@pytest.fixture(autouse=True)
def block_real_oss_clients(monkeypatch):
    """Unit/regression suites use fake OSS; live probes run outside pytest.

    A mistakenly inherited bucket must fail here, before any real network write.
    Existing fake fixtures replace the public storage functions directly.
    """
    from question_bank import oss_store
    def forbidden(*args, **kwargs):
        raise AssertionError("Real OSS clients are forbidden in pytest; use a fake or an isolated standalone probe")
    monkeypatch.setattr(oss_store, "_bucket", forbidden)
    monkeypatch.setattr(oss_store, "_public_bucket", forbidden)

@pytest.fixture(autouse=True)
def isolated_bank(monkeypatch):
    name='qb_test_'+uuid.uuid4().hex
    monkeypatch.setenv('QB_DATABASE_SCHEMA',name)
    original=db.connect
    roots={}
    schemas=set()
    def connect(root, *, database_schema=None):
        if database_schema:
            schema=database_schema
        else:
            key=str(root)
            if key not in roots: roots[key]=name if not roots else name+'_'+hashlib.sha256(key.encode()).hexdigest()[:8]
            schema=roots[key]
        result = original(root,database_schema=schema)
        schemas.add(schema)
        return result
    monkeypatch.setattr(db,'connect',connect)
    yield
    if schemas:
        with psycopg.connect(db.database_url(),autocommit=True) as conn:
            for schema in schemas:
                conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))

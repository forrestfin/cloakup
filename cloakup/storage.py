"""Durable document aggregates, immutable revisions and append-only run events.

PostgreSQL in Compose; SQLite is the same contract for local development/tests.
Artifacts and Chromium profiles are files, never stored in JSON columns.
"""
from __future__ import annotations
import copy
import os
import time
import uuid
from pathlib import Path
from sqlalchemy import (JSON, Column, Float, Integer, MetaData, String, Table,
                        create_engine, event, select, update)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
json_type = JSON().with_variant(JSONB(), 'postgresql')
documents = Table('documents', metadata,
    Column('kind', String(32), primary_key=True), Column('id', String(96), primary_key=True),
    Column('revision', Integer, nullable=False), Column('updated_at', Float, nullable=False),
    Column('payload', json_type, nullable=False))
versions = Table('document_versions', metadata,
    Column('kind', String(32), primary_key=True), Column('id', String(96), primary_key=True),
    Column('revision', Integer, primary_key=True), Column('at', Float, nullable=False),
    Column('payload', json_type, nullable=False))
events = Table('process_events', metadata,
    Column('id', String(32), primary_key=True), Column('run_id', String(32), nullable=False, index=True),
    Column('at', Float, nullable=False), Column('payload', json_type, nullable=False))

operations = Table('action_operations', metadata,
    Column('id',String(64),primary_key=True),Column('task_id',String(128),nullable=False,index=True),
    Column('resource',String(256),nullable=False,index=True),Column('capability',String(100),nullable=False),
    Column('run_id',String(128)),Column('state',String(24),nullable=False),
    Column('payload',json_type,nullable=False),Column('updated_at',Float,nullable=False))

action_leases=Table('action_leases',metadata,Column('id',String(64),primary_key=True),Column('operation_id',String(64),nullable=False))

class Database:
    def __init__(self, root: Path, url: str | None = None):
        data = root / 'data'
        data.mkdir(parents=True, exist_ok=True)
        url = url or os.environ.get('DATABASE_URL') or f'sqlite:///{data / "cloakup.db"}'
        kw = {'connect_args': {'check_same_thread': False, 'timeout': 20}} if url.startswith('sqlite:') else {'pool_pre_ping': True, 'connect_args': {'connect_timeout': 5}}
        self.engine = create_engine(url, **kw)
        self.backend = self.engine.dialect.name
        if self.backend == 'sqlite':
            @event.listens_for(self.engine, 'connect')
            def configure(connection, _):
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('PRAGMA foreign_keys=ON')
        metadata.create_all(self.engine)

    def get(self, kind, key):
        with self.engine.connect() as c:
            row = c.execute(select(documents.c.payload).where(documents.c.kind == kind, documents.c.id == key)).first()
        if row is None:
            raise FileNotFoundError(f'未找到 {kind} 记录')
        return copy.deepcopy(row[0])

    def list(self, kind):
        with self.engine.connect() as c:
            return [copy.deepcopy(r[0]) for r in c.execute(select(documents.c.payload).where(documents.c.kind == kind).order_by(documents.c.updated_at.desc()))]

    def put(self, kind, key, payload, *, expected=None, history=False):
        """CAS makes editable configuration revisions safe across HTTP clients."""
        with self.engine.begin() as c:
            q = select(documents.c.revision).where(documents.c.kind == kind, documents.c.id == key)
            if self.backend == 'postgresql':
                q = q.with_for_update()
            old = c.execute(q).scalar_one_or_none()
            if expected is not None and (old or 0) != expected:
                raise ValueError('配置已被其他操作修改，请刷新后再保存')
            revision = (old or 0) + 1
            values = dict(payload=copy.deepcopy(payload), revision=revision, updated_at=time.time())
            if old is None:
                c.execute(documents.insert().values(kind=kind, id=key, **values))
            else:
                result = c.execute(update(documents).where(documents.c.kind == kind, documents.c.id == key, documents.c.revision == old).values(**values))
                if result.rowcount != 1:
                    raise ValueError('记录发生并发更新，请刷新重试')
            if history:
                c.execute(versions.insert().values(kind=kind, id=key, revision=revision, at=time.time(), payload=copy.deepcopy(payload)))
        return revision

    def append(self, run_id, payload):
        with self.engine.begin() as c:
            c.execute(events.insert().values(id=uuid.uuid4().hex, run_id=run_id, at=time.time(), payload=copy.deepcopy(payload)))

    def put_batch(self, items, *, checks=()):
        """Atomic release: all CAS checks and immutable snapshots commit together.

        Each item is (kind, id, payload, expected_document_revision, history).
        checks are (kind, id, expected_document_revision) read dependencies.
        """
        with self.engine.begin() as c:
            for kind,key,expected in sorted(checks):
                q=select(documents.c.revision).where(documents.c.kind==kind,documents.c.id==key)
                if self.backend=='postgresql':q=q.with_for_update()
                actual=c.execute(q).scalar_one_or_none() or 0
                if actual!=expected:raise ValueError('发布依据已变化，请重新建立草案并验证')
            for kind,key,payload,expected,history in items:
                q=select(documents.c.revision).where(documents.c.kind==kind,documents.c.id==key)
                if self.backend=='postgresql':q=q.with_for_update()
                old=c.execute(q).scalar_one_or_none()
                if expected is not None and (old or 0)!=expected:raise ValueError('配置发生并发变化，未发布任何更改')
                revision=(old or 0)+1
                values=dict(payload=copy.deepcopy(payload),revision=revision,updated_at=time.time())
                if old is None:c.execute(documents.insert().values(kind=kind,id=key,**values))
                else:
                    result=c.execute(update(documents).where(documents.c.kind==kind,documents.c.id==key,documents.c.revision==old).values(**values))
                    if result.rowcount!=1:raise ValueError('记录发生并发更新，未发布任何更改')
                if history:c.execute(versions.insert().values(kind=kind,id=key,revision=revision,at=time.time(),payload=copy.deepcopy(payload)))

    def revision(self,kind,key):
        with self.engine.connect() as c:
            return c.execute(select(documents.c.revision).where(documents.c.kind==kind,documents.c.id==key)).scalar_one_or_none() or 0

    def revisions(self,kind):
        """某类记录的 (id, 修订号) 列表，不读内容；供缓存判断是否有记录新增或变化。"""
        with self.engine.connect() as c:
            return sorted((r.id,r.revision) for r in c.execute(select(documents.c.id,documents.c.revision).where(documents.c.kind==kind)))

    def trace(self, run_id):
        with self.engine.connect() as c:
            return [r[0] for r in c.execute(select(events.c.payload).where(events.c.run_id == run_id).order_by(events.c.at))]

    def history(self, kind, key):
        with self.engine.connect() as c:
            return [{'revision': r.revision, 'at': r.at, 'config': r.payload} for r in c.execute(select(versions).where(versions.c.kind == kind, versions.c.id == key).order_by(versions.c.revision.desc()))]

    def probe(self):
        key = uuid.uuid4().hex
        with self.engine.begin() as c:
            c.execute(documents.insert().values(kind='health_probe', id=key, revision=1, updated_at=time.time(), payload={'value': key}))
            assert c.execute(select(documents.c.payload).where(documents.c.kind == 'health_probe', documents.c.id == key)).scalar_one()['value'] == key
            c.execute(documents.delete().where(documents.c.kind == 'health_probe', documents.c.id == key))
        return self.backend

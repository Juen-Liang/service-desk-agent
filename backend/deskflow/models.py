from datetime import datetime
from sqlalchemy import String, Text, DateTime, Integer, Boolean, ForeignKey, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector
from .db import Base, now, uid


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    password: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default='employee')
    department: Mapped[str] = mapped_column(String(80), default='研发部')
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Refresh(Base):
    __tablename__ = 'refresh_tokens'
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Ticket(Base):
    __tablename__ = 'tickets'
    __table_args__ = (UniqueConstraint('owner_id', 'request_key'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    department: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(30), default='auto')
    status: Mapped[str] = mapped_column(String(30), default='pending')
    version: Mapped[int] = mapped_column(Integer, default=1)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'))
    request_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    fields: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Comment(Base):
    __tablename__ = 'comments'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    ticket_id: Mapped[str] = mapped_column(ForeignKey('tickets.id'), index=True)
    author_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'))
    author_name: Mapped[str] = mapped_column(String(80), default='DeskFlow')
    body: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Plan(Base):
    __tablename__ = 'plans'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    ticket_id: Mapped[str] = mapped_column(ForeignKey('tickets.id'), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(40))
    params: Mapped[dict] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSONB, default=list)
    digest: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[int] = mapped_column(Integer)
    approval_required: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Approval(Base):
    __tablename__ = 'approvals'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    plan_id: Mapped[str] = mapped_column(ForeignKey('plans.id'), unique=True)
    status: Mapped[str] = mapped_column(String(20), default='pending')
    approver_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'))
    note: Mapped[str] = mapped_column(Text, default='')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Run(Base):
    __tablename__ = 'runs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    ticket_id: Mapped[str] = mapped_column(ForeignKey('tickets.id'), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(20), default='mock')
    status: Mapped[str] = mapped_column(String(30), default='queued', index=True)
    phase: Mapped[str] = mapped_column(String(50), default='queued')
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rounds: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    model_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(Base):
    __tablename__ = 'events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[str | None] = mapped_column(String(36), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Operation(Base):
    __tablename__ = 'operations'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey('plans.id'), unique=True)
    status: Mapped[str] = mapped_column(String(30), default='pending')
    receipt: Mapped[dict] = mapped_column(JSONB, default=dict)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)


class Config(Base):
    __tablename__ = 'config'
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Document(Base):
    __tablename__ = 'documents'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    active_version_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DocumentVersion(Base):
    __tablename__ = 'document_versions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(ForeignKey('documents.id'), index=True)
    number: Mapped[int] = mapped_column(Integer)
    file_hash: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default='queued')
    error: Mapped[str] = mapped_column(Text, default='')
    embedding_identity: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Chunk(Base):
    __tablename__ = 'chunks'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    version_id: Mapped[str] = mapped_column(ForeignKey('document_versions.id'), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    location: Mapped[dict] = mapped_column(JSONB)
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR)
    embedding: Mapped[list | None] = mapped_column(Vector(1024))
    __table_args__ = (
        Index('ix_chunks_fts', 'search_vector', postgresql_using='gin'),
        Index('ix_chunks_vector', 'embedding', postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'}),
    )


class Evaluation(Base):
    __tablename__ = 'evaluations'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    mode: Mapped[str] = mapped_column(String(20))
    label: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default='queued')
    results: Mapped[list] = mapped_column(JSONB, default=list)
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

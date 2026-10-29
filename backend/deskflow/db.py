from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings


def now():
    return datetime.now(timezone.utc)


def uid():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


engine = create_engine(settings().app_dsn, pool_pre_ping=True, pool_size=8, max_overflow=8)
Session = sessionmaker(engine, expire_on_commit=False)

from alembic import context
from sqlalchemy import create_engine
from deskflow.config import settings
from deskflow.db import Base
from deskflow import models
from deskflow.demo import DemoBase
target=context.config.attributes.get('target','agent')
dsn=settings().demo_dsn if target=='demo' else settings().app_dsn
metadata=DemoBase.metadata if target=='demo' else Base.metadata
with create_engine(dsn).connect() as connection:
    context.configure(connection=connection,target_metadata=metadata)
    with context.begin_transaction(): context.run_migrations()

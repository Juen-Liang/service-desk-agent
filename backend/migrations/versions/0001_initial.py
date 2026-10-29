from alembic import op, context
from deskflow.db import Base
from deskflow import models
from deskflow.demo import DemoBase
revision='0001'
down_revision=None
branch_labels=None
depends_on=None
def upgrade():
    metadata=DemoBase.metadata if context.config.attributes.get('target')=='demo' else Base.metadata
    metadata.create_all(op.get_bind())
def downgrade():
    raise RuntimeError('DeskFlow 首版不提供自动删除数据的降级；请通过备份恢复')

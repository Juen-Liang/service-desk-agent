"""Create only DeskFlow-owned databases/roles. Never change existing foreign databases."""
import os, secrets, getpass, json, hashlib
from pathlib import Path
from urllib.parse import quote
import psycopg
from psycopg import sql
from cryptography.fernet import Fernet
ROOT=Path(__file__).resolve().parent.parent

def main():
    env=ROOT/'.env'
    if not env.exists():
        app_pw,demo_pw=secrets.token_urlsafe(30),secrets.token_urlsafe(30)
        env.write_text('\n'.join([
            f'APP_DSN=postgresql+psycopg://deskflow_app:{quote(app_pw)}@127.0.0.1:5432/service_desk_agent',
            f'DEMO_DSN=postgresql+psycopg://deskflow_demo:{quote(demo_pw)}@127.0.0.1:5432/service_desk_demo',
            f'JWT_SECRET={secrets.token_urlsafe(48)}',f'DEMO_SERVICE_SECRET={secrets.token_urlsafe(48)}',
            f'ENCRYPTION_KEY={Fernet.generate_key().decode()}', 'DEMO_URL=http://127.0.0.1:8012',
            'FRONTEND_ORIGIN=http://127.0.0.1:5178','WORKER_CONCURRENCY=2','']),encoding='utf-8')
    from deskflow.config import settings
    from sqlalchemy.engine import make_url
    cfg=settings()
    password=os.environ.get('DESKFLOW_ADMIN_PASSWORD') or getpass.getpass('PostgreSQL 管理员密码: ')
    admin_host=os.environ.get('DESKFLOW_ADMIN_HOST','127.0.0.1')
    with psycopg.connect(host=admin_host,port=5432,user='postgres',password=password,dbname='postgres',autocommit=True) as db:
        for dsn in [cfg.app_dsn,cfg.demo_dsn]:
            url=make_url(dsn)
            if url.database not in ('service_desk_agent','service_desk_demo') or url.username not in ('deskflow_app','deskflow_demo'): raise RuntimeError('初始化目标不属于 DeskFlow')
            role=db.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(url.username,)).fetchone()
            if not role:
                db.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(url.username),sql.Literal(url.password)))
            owner=db.execute('SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s',(url.database,)).fetchone()
            if owner and owner[0]!=url.username: raise RuntimeError('数据库已存在且所有者不同，停止初始化')
            if not owner: db.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(url.database),sql.Identifier(url.username)))
        with psycopg.connect(host=admin_host,user='postgres',password=password,dbname='service_desk_agent',autocommit=True) as app:
            app.execute('CREATE EXTENSION IF NOT EXISTS vector')
    initialize()
    print('DeskFlow 独立数据库初始化完成；演示登录凭据保存在 data/demo-credentials.json（不提交 Git）。')

def initialize():
    from alembic.config import Config
    from alembic import command
    for target in ('agent','demo'):
        cfg=Config(str(ROOT/'backend/alembic.ini')); cfg.set_main_option('script_location',str(ROOT/'backend/migrations')); cfg.attributes['target']=target
        command.upgrade(cfg,'head')
    from deskflow.db import Session, uid
    from deskflow.models import User, Config as AppConfig, Document, DocumentVersion
    from deskflow.security import passwords
    from deskflow.policy import DEFAULT_POLICY
    from deskflow.demo import seed, SeedEmployee
    from sqlalchemy import select
    (ROOT/'data/uploads').mkdir(parents=True,exist_ok=True)
    credentials_path=ROOT/'data/demo-credentials.json'
    credentials=json.loads(credentials_path.read_text(encoding='utf-8')) if credentials_path.exists() else {}
    with Session.begin() as db:
        for username,name,role,department in [('employee','林小雨','employee','研发部'),('employee2','陈子安','employee','市场部'),('support','周远 · IT 支持','support','IT 部'),('approver','许宁 · 部门审批','approver','研发部'),('admin','系统管理员','admin','IT 部')]:
            u=db.scalar(select(User).where(User.username==username))
            if not u:
                password=secrets.token_urlsafe(12)
                u=User(id=uid(),username=username,name=name,role=role,department=department,password=passwords.hash(password)); db.add(u)
                credentials[username]={'password':password,'name':name,'role':role}
                seed(SeedEmployee(id=u.id,name=name,department=department))
        if not db.get(AppConfig,'policy'): db.add(AppConfig(key='policy',value=DEFAULT_POLICY))
        for file in (ROOT/'examples/knowledge').glob('*.md'):
            if db.scalar(select(Document).where(Document.name==file.name)): continue
            data=file.read_bytes(); d=Document(id=uid(),name=file.name); db.add(d); db.flush()
            v=DocumentVersion(id=uid(),document_id=d.id,number=1,filename=file.name,file_hash=hashlib.sha256(data).hexdigest()); db.add(v)
            (ROOT/'data/uploads'/v.id).write_bytes(data)
    credentials_path.write_text(json.dumps(credentials,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__': main()

"""Independent business API. Only this process connects to service_desk_demo."""
from datetime import datetime, timedelta
import secrets
import jwt
from fastapi import FastAPI, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, String, Integer, DateTime, ForeignKey, UniqueConstraint, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .config import settings
from .db import now
from .security import digest


class DemoBase(DeclarativeBase):
    pass


class Employee(DemoBase):
    __tablename__ = 'employees'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB)
    scope: Mapped[str] = mapped_column(String(100), default='demo')


class Software(DemoBase):
    __tablename__ = 'software'
    id: Mapped[str] = mapped_column(String(150), primary_key=True)
    sku: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(100))
    capacity: Mapped[int] = mapped_column(Integer, default=3)
    scope: Mapped[str] = mapped_column(String(100), default='demo')


class License(DemoBase):
    __tablename__ = 'licenses'
    __table_args__ = (UniqueConstraint('employee_id', 'software_id'),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey('employees.id'))
    software_id: Mapped[str] = mapped_column(ForeignKey('software.id'))


class Grant(DemoBase):
    __tablename__ = 'grants'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey('employees.id'))
    resource: Mapped[str] = mapped_column(String(40))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Receipt(DemoBase):
    __tablename__ = 'receipts'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


demo_engine = create_engine(settings().demo_dsn, pool_pre_ping=True)
DemoSession = sessionmaker(demo_engine, expire_on_commit=False)


def service_auth(authorization: str = Header(default='')):
    expected = settings().demo_service_secret
    if not expected or not secrets.compare_digest(authorization, f'Bearer {expected}'):
        raise HTTPException(401, '业务服务认证失败')


app = FastAPI(title='DeskFlow 演示业务系统', dependencies=[Depends(service_auth)])


def employee_view(db, employee):
    data = dict(employee.data)
    grants = db.scalars(select(Grant).where(Grant.employee_id == employee.id)).all()
    data['permissions'] = [{'resource': g.resource, 'expires_at': g.expires_at.isoformat(),
                            'active': g.expires_at > now() and g.revoked_at is None} for g in grants]
    licenses = db.scalars(select(License).where(License.employee_id == employee.id)).all()
    data['licenses'] = [db.get(Software, item.software_id).sku for item in licenses]
    return {'id': employee.id, **data}


@app.get('/health')
def health():
    with DemoSession() as db:
        db.execute(text('SELECT 1'))
    return {'status': 'ok', 'service': 'demo-business'}


@app.get('/employees/{employee_id}')
def employee(employee_id: str):
    with DemoSession() as db:
        item = db.get(Employee, employee_id)
        if not item:
            raise HTTPException(404, '演示员工不存在')
        return employee_view(db, item)


@app.get('/software/{employee_id}')
def software(employee_id: str):
    with DemoSession() as db:
        employee = db.get(Employee, employee_id)
        if not employee:
            raise HTTPException(404, '演示员工不存在')
        items = db.scalars(select(Software).where(Software.scope == employee.scope)).all()
        return [{'sku': s.sku, 'name': s.name, 'capacity': s.capacity,
                 'available': s.capacity - len(db.scalars(select(License).where(License.software_id == s.id)).all())} for s in items]


@app.get('/operations/{operation_id}')
def operation(operation_id: str):
    with DemoSession() as db:
        item = db.get(Receipt, operation_id)
        if not item:
            raise HTTPException(404, '操作尚未完成')
        return item.result


class Execute(BaseModel):
    operation_id: str = Field(max_length=80)
    action: str
    params: dict
    permit: str


@app.post('/operations')
def execute(payload: Execute):
    request_hash = digest({'action': payload.action, 'params': payload.params})
    try:
        claims = jwt.decode(payload.permit, settings().demo_service_secret, algorithms=['HS256'], audience='deskflow-business')
        if (claims['op'], claims['action'], claims['hash']) != (payload.operation_id, payload.action, digest(payload.params)):
            raise ValueError('permit mismatch')
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(403, '执行凭据无效或已过期')
    with DemoSession.begin() as db:
        # A transaction-scoped lock serializes duplicate deliveries, even before the receipt exists.
        db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': payload.operation_id})
        receipt = db.get(Receipt, payload.operation_id)
        if receipt:
            if receipt.request_hash != request_hash:
                raise HTTPException(409, '操作 ID 已用于不同参数')
            return receipt.result
        params = payload.params
        employee = db.scalar(select(Employee).where(Employee.id == params.get('employee_id')).with_for_update())
        if not employee or not employee.data.get('active'):
            raise HTTPException(409, '员工不存在或已停用')
        data = dict(employee.data)
        if data.get('privileged'):
            raise HTTPException(403, '特权账号必须人工处理')
        result = {'operation_id': payload.operation_id, 'action': payload.action, 'employee_id': employee.id, 'success': True}
        if payload.action == 'unlock_account':
            if data.get('locked') and data.get('lock_reason') != 'failed_login':
                raise HTTPException(409, '当前锁定原因不允许自动解锁')
            data.update(locked=False, lock_reason=None)
            employee.data = data
            result['locked'] = False
        elif payload.action == 'grant_permission':
            hours, resource = params.get('hours', 24), params.get('resource')
            if type(hours) is not int or not 1 <= hours <= 168 or resource not in {'VPN_ACCESS', 'DEV_READONLY'}:
                raise HTTPException(422, '权限或有效期不符合策略')
            expiry = now() + timedelta(hours=hours)
            db.add(Grant(id=payload.operation_id, employee_id=employee.id, resource=resource, expires_at=expiry))
            result.update(resource=resource, expires_at=expiry.isoformat())
        elif payload.action == 'assign_software':
            item = db.scalar(select(Software).where(Software.scope == employee.scope, Software.sku == params.get('sku')).with_for_update())
            if not item:
                raise HTTPException(422, '软件不在授权目录')
            existing = db.scalar(select(License).where(License.employee_id == employee.id, License.software_id == item.id))
            if not existing:
                used = len(db.scalars(select(License).where(License.software_id == item.id)).all())
                if used >= item.capacity:
                    raise HTTPException(409, '软件授权名额已用完')
                db.add(License(id=payload.operation_id, employee_id=employee.id, software_id=item.id))
            result['sku'] = item.sku
        else:
            raise HTTPException(422, '不支持的业务操作')
        db.add(Receipt(id=payload.operation_id, request_hash=request_hash, result=result))
        return result


class SeedEmployee(BaseModel):
    id: str = Field(max_length=80)
    name: str
    department: str = '研发部'
    scenario: str = 'normal'
    scope: str = 'demo'


SCENARIOS = {'normal', 'security_lock', 'inactive', 'privileged', 'unlocked', 'no_seats'}


@app.post('/seed')
def seed(payload: SeedEmployee):
    if payload.scenario not in SCENARIOS or not (payload.scope == 'demo' or payload.scope.startswith(('eval-', 'test-'))):
        raise HTTPException(422, '仅允许预置演示场景')
    with DemoSession.begin() as db:
        item = db.scalar(select(Employee).where(Employee.id == payload.id).with_for_update())
        if item and item.scope != payload.scope:
            raise HTTPException(409, '不能覆盖其他场景的数据')
        data = {'name': payload.name, 'department': payload.department,
                'active': payload.scenario != 'inactive', 'privileged': payload.scenario == 'privileged',
                'locked': payload.scenario != 'unlocked', 'lock_reason': 'security' if payload.scenario == 'security_lock' else 'failed_login',
                'devices': [{'id': f'DEV-{payload.id[:8]}', 'name': '办公笔记本', 'status': 'in_use'}]}
        if item:
            item.data = data
        else:
            db.add(Employee(id=payload.id, data=data, scope=payload.scope))
        for sku, name in [('IDE_PRO', '开发工具专业版'), ('DIAGRAM_TOOL', '流程图工具')]:
            software_id = f'{payload.scope}:{sku}'
            software = db.get(Software, software_id)
            if not software:
                db.add(Software(id=software_id, sku=sku, name=name, scope=payload.scope, capacity=0 if payload.scenario == 'no_seats' else 3))
        return {'id': payload.id, 'scope': payload.scope, 'scenario': payload.scenario}


@app.post('/maintenance/expire')
def expire():
    with DemoSession.begin() as db:
        rows = db.scalars(select(Grant).where(Grant.expires_at <= now(), Grant.revoked_at.is_(None)).with_for_update(skip_locked=True)).all()
        for row in rows:
            row.revoked_at = now()
        return {'expired': len(rows)}

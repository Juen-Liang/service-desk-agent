from sqlalchemy import select
from fastapi import HTTPException
from .db import uid, now
from .models import Ticket, Run, Config, Event, Plan, Approval
from .workflow import SYSTEM
from .providers import validate_profile

def as_dict(row, omit=()):
    return {c.name: (v.isoformat() if hasattr(v, 'isoformat') else v) for c in row.__table__.columns if c.name not in omit for v in [getattr(row, c.name)]}

def public_run(row):
    return as_dict(row, ('model_snapshot', 'lease_token')) | {'model': row.model_snapshot.get('model', '模拟流程')}

def visible(user, ticket):
    return ticket and (user.role in ('admin', 'support') or ticket.owner_id == user.id or user.role == 'approver' and user.department == ticket.department)

def get_ticket(db, ticket_id, user, lock=False):
    query = select(Ticket).where(Ticket.id == ticket_id)
    row = db.scalar(query.with_for_update() if lock else query)
    if not visible(user, row):
        raise HTTPException(404, '工单不存在或无权访问')
    return row

def invalidate(db, ticket, reason):
    ticket.generation += 1
    for run in db.scalars(select(Run).where(Run.ticket_id == ticket.id, Run.status.in_(['queued','running','paused','failed','waiting_approval']))).all():
        run.status, run.error = 'cancelled', reason
    for a in db.scalars(select(Approval).join(Plan, Plan.id == Approval.plan_id).where(Plan.ticket_id == ticket.id, Approval.status == 'pending')).all():
        a.status = 'superseded'

def start_run(db, ticket, mode, profile_override=None):
    if mode not in ('mock','real'):
        raise HTTPException(422, '无效运行模式')
    existing = db.scalar(select(Run).where(Run.ticket_id == ticket.id, Run.status.in_(['queued','running'])))
    if existing:
        return existing
    if ticket.status in ('closed','cancelled'):
        raise HTTPException(409, '请先重新打开工单')
    profile = profile_override or (db.get(Config, 'chat').value if db.get(Config, 'chat') else {})
    if mode == 'real':
        try:
            validate_profile(profile)
        except ValueError as e:
            raise HTTPException(422, str(e))
    invalidate(db, ticket, '开始新一轮分析')
    ticket.status, ticket.version, ticket.updated_at = 'processing', ticket.version + 1, now()
    snapshot = dict(profile) if mode == 'real' else {k:v for k,v in profile.items() if k=='knowledge_version_ids'}
    snapshot.setdefault('prompt', SYSTEM)
    run = Run(id=uid(), ticket_id=ticket.id, generation=ticket.generation, mode=mode, model_snapshot=snapshot)
    db.add(run)
    db.add(Event(ticket_id=ticket.id, run_id=run.id, kind='queued', data={'mode':mode}))
    return run
